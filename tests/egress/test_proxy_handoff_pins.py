"""同期 handoff と INET_DIAG の検査のうち、既存の試験が観測していなかった防御を固定する。

- 応答の検査: 長さ・rtattr・NLMSG_ERROR の短い応答は、どれも LookupFailed (malformed) に
  なり、struct の例外や受理にならない。cookie は 64 bit で取り出す。
- lookup: 取り残しの応答が複数あっても全部捨てる。要求ごとに seq が変わる。
- probe: state・client inode・server inode のどれが食い違っても起動失敗にする。
- 判定: client / server の state、再 lookup の inode / cookie / 失敗、cookie でなく inode に
  よる同一性、判定中の例外、履歴の上限。
- proxy: 拒否した接続の要求を 1 byte も読まない。
- supervisor との結合: ack の待ちは PROXY_ACK_DEADLINE_SEC で切れ、不成立なら tracee の
  socket は使えなくなる。
"""
from __future__ import annotations

import dataclasses
import errno
import os
import socket
import struct
import threading
import time

import pytest

from agentic_fx.core import connect_supervisor as CS
from agentic_fx.egress import handoff as H
from agentic_fx.egress import inet_diag as D
from agentic_fx.egress.proxy import ProxyStartFailed
from tests.egress._handoff_peers import dup_from
from tests.egress.test_inet_diag import _mutate, _real_reply, _require_diag, diag, pair  # noqa: F401
from tests.egress.test_proxy_handoff import LO, rigs, tracee_handoff  # noqa: F401
from tests.egress.test_proxy_handoff_supervisor import factory, mission  # noqa: F401

_HDR = 16
_COOKIE_HI_OFF = _HDR + 4 + 44  # inet_diag_msg の idiag_sockid.idiag_cookie[1]


def _with_len(data: bytes) -> bytes:
    return _mutate(data, 0, "=I", len(data))


# --- 応答の検査 -----------------------------------------------------------------

def _parse(data: bytes, peer, server, seq: int = 77) -> D.DiagSocket:
    return D.parse_response(data, seq=seq, src=peer, dst=server)


def test_header_length_shorter_than_datagram_is_malformed(diag, pair):
    _c, _a, peer, server = pair
    data = _real_reply(diag, peer, server)
    with pytest.raises(D.LookupFailed) as ei:
        _parse(_mutate(data, 0, "=I", len(data) - 4), peer, server)
    assert ei.value.reason == D.R_MALFORMED


def test_unpadded_last_attribute_is_malformed(diag, pair):
    _c, _a, peer, server = pair
    data = _real_reply(diag, peer, server)
    # 長さ 5 の属性で終わり、4 byte 境界までの詰めが無い
    data = _with_len(data + struct.pack("=HH", 5, 1) + b"\x01")
    with pytest.raises(D.LookupFailed) as ei:
        _parse(data, peer, server)
    assert ei.value.reason == D.R_MALFORMED


def test_one_trailing_byte_is_malformed_not_struct_error(diag, pair):
    _c, _a, peer, server = pair
    data = _with_len(_real_reply(diag, peer, server) + b"\x08")
    with pytest.raises(D.LookupFailed) as ei:
        _parse(data, peer, server)
    assert ei.value.reason == D.R_MALFORMED


def test_short_nlmsg_error_is_malformed_not_struct_error():
    msg = struct.pack("=IHHII", _HDR, D.NLMSG_ERROR, 0, 5, 0)
    with pytest.raises(D.LookupFailed) as ei:
        D.parse_response(msg, seq=5, src=(LO, 1), dst=(LO, 2))
    assert ei.value.reason == D.R_MALFORMED


def test_cookie_keeps_upper_32_bits(diag, pair):
    _c, _a, peer, server = pair
    data = _real_reply(diag, peer, server)
    lo = _parse(data, peer, server).cookie
    got = _parse(_mutate(data, _COOKIE_HI_OFF, "=I", 3), peer, server)
    assert got.cookie == (3 << 32) | (lo & 0xFFFFFFFF)


# --- lookup ------------------------------------------------------------------------

def test_every_stale_reply_is_drained(diag, pair):
    _c, _a, peer, server = pair
    for seq in (0xABC001, 0xABC002, 0xABC003):
        diag.sock.send(D.build_request(peer, server, seq=seq))
    time.sleep(0.05)
    assert diag.lookup(peer, server).state == D.TCP_ESTABLISHED


class _Recorder:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self.sent: list[bytes] = []

    def send(self, data: bytes, *a) -> int:
        self.sent.append(bytes(data))
        return self._sock.send(data, *a)

    def __getattr__(self, name):
        return getattr(self._sock, name)


def test_each_request_has_a_new_seq(diag, pair):
    _c, _a, peer, server = pair
    rec = _Recorder(diag.sock)
    d = D.InetDiag(rec)  # type: ignore[arg-type]
    d.lookup(peer, server)
    d.lookup(peer, server)
    seqs = [struct.unpack_from("=I", m, 8)[0] for m in rec.sent]
    assert len(seqs) == 2 and seqs[0] != seqs[1]


# --- probe -------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["client_state", "client_inode", "server_inode"])
def test_probe_rejects_any_mismatch(monkeypatch, case):
    _require_diag()
    real = D.InetDiag.lookup
    calls = {"n": 0}

    def lookup(self, src, dst):
        calls["n"] += 1
        got = real(self, src, dst)
        if case == "client_state" and calls["n"] == 1:
            return dataclasses.replace(got, state=D.TCP_ESTABLISHED + 1)
        if case == "client_inode" and calls["n"] == 1:
            return dataclasses.replace(got, inode=got.inode + 1)
        if case == "server_inode" and calls["n"] == 2:
            return dataclasses.replace(got, inode=got.inode + 1)
        return got

    monkeypatch.setattr(D.InetDiag, "lookup", lookup)
    with pytest.raises(ProxyStartFailed):
        D.probe_inet_diag()
    assert calls["n"] == 2


# --- 判定 -----------------------------------------------------------------------------

def _tamper(monkeypatch, plan: dict) -> None:
    """admit の n 回目の lookup (1 = client、2 = server、3 = client の再 lookup) を差し替える。"""
    real = D.InetDiag.lookup
    calls = {"n": 0}

    def lookup(self, src, dst):
        calls["n"] += 1
        fn = plan.get(calls["n"])
        if fn is None:
            return real(self, src, dst)
        return fn(real(self, src, dst))

    monkeypatch.setattr(D.InetDiag, "lookup", lookup)


def _fail(_got):
    raise D.LookupFailed(D.R_TIMEOUT)


def _bump_inode(got):
    return dataclasses.replace(got, inode=got.inode + 1)


def _bump_cookie(got):
    return dataclasses.replace(got, cookie=got.cookie + 1)


def _syn_sent(got):
    return dataclasses.replace(got, state=2)


@pytest.mark.parametrize("plan,want", [
    ({1: _syn_sent}, H.V_STATE),
    ({2: _syn_sent}, H.V_STATE),
    ({3: _bump_inode}, H.V_CHANGED),
    ({3: _bump_cookie}, H.V_CHANGED),
    ({3: _fail}, H.V_CHANGED),
    # cookie (と tuple) は一致し、inode だけが違う client: cookie では同一性を決めない
    ({1: _bump_inode, 3: _bump_inode}, H.V_NO_MATCH),
], ids=["client_state", "server_state", "relookup_inode", "relookup_cookie", "relookup_fail",
        "inode_not_cookie"])
def test_judge_rejects(rigs, monkeypatch, plan, want):  # noqa: F811
    rig = rigs()
    _tamper(monkeypatch, plan)
    r = tracee_handoff(rig)
    assert r["reply"] == "ECONNREFUSED", r
    assert rig.wait_verdicts(1) == [want]
    assert rig.proxy.active_tunnels() == 0


def test_judge_exception_is_rejected(rigs):  # noqa: F811
    def boom(_conn, _peer):
        raise RuntimeError("injected")

    rig = rigs(after_accept=boom)
    r = tracee_handoff(rig)
    assert r["reply"] == "ECONNREFUSED", r
    assert rig.wait_verdicts(1) == [H.V_ERROR]
    assert rig.h.history()[0]["error"] == "RuntimeError"
    assert rig.proxy.active_tunnels() == 0


def test_history_is_capped_at_256(rigs):  # noqa: F811
    rig = rigs(start=False)
    c = socket.socket()
    c.close()
    for _ in range(300):
        assert rig.h.admit(c) is False
    assert len(rig.h.history()) == 256
    assert rig.h.verdict_counts() == {H.V_NO_PEER: 300}


# --- proxy: 拒否した接続は読まない -----------------------------------------------------

def test_rejected_connection_is_closed_with_its_byte_unread(rigs):  # noqa: F811
    """要求が 1 byte だけ届いた状態で拒否する。読まずに閉じれば kernel は RST を返す
    (読んでから閉じると FIN になる)。"""
    sent = threading.Event()

    def wait_sent(_conn, _peer):
        assert sent.wait(5)
        time.sleep(0.05)

    rig = rigs(after_accept=wait_sent)
    c = socket.create_connection((LO, rig.port), timeout=5)
    try:
        c.sendall(b"C")
        sent.set()
        assert rig.wait_verdicts(1) == [H.V_NO_MATCH]
        with pytest.raises(ConnectionResetError):
            c.recv(1)
    finally:
        c.close()


# --- supervisor との結合 ----------------------------------------------------------------

def test_missing_ack_ends_at_the_ack_deadline_and_disables_the_socket(mission):  # noqa: F811
    m = mission(start_proxy=False)
    t0 = time.monotonic()
    r = m.connect(keep=True)
    took = time.monotonic() - t0
    assert r["rc"] == -errno.ECONNREFUSED, r
    assert CS.PROXY_ACK_DEADLINE_SEC - 0.1 <= took < CS.PROXY_ACK_DEADLINE_SEC + 0.4
    # 複製を shutdown したので、tracee の socket からはもう送れない
    s = socket.socket(fileno=dup_from(m.t.pid, r["fd"]))
    try:
        with pytest.raises(OSError) as ei:
            s.send(b"x", socket.MSG_NOSIGNAL | socket.MSG_DONTWAIT)
        assert ei.value.errno == errno.EPIPE
    finally:
        s.close()


def test_client_leaving_established_between_lookups_is_rejected(rigs):  # noqa: F811
    """1 回目の lookup の後、同じ socket (inode・cookie・tuple は同じ) が FIN を送って
    ESTABLISHED でなくなったら、再 lookup で拒否する。"""
    box: dict = {}

    def cut(_conn, _peer):
        if "dup" in box:
            from tests.egress.test_proxy_handoff import _shut
            _shut(box["dup"])

    rig = rigs(between_lookups=cut)
    t = rig.tracee()
    dup = t.dup()
    box["dup"] = dup
    try:
        from tests.egress.test_proxy_handoff import sv_handoff
        r = sv_handoff(rig.h, rig.port, dup)
    finally:
        os.close(dup)
    assert r["reply"] == "ECONNREFUSED", r
    assert rig.wait_verdicts(1) == [H.V_CHANGED]
    assert rig.h.history()[0]["client_state"] == D.TCP_ESTABLISHED
    assert rig.proxy.active_tunnels() == 0


class _RaisingDict(dict):
    def get(self, *a):
        raise RuntimeError("injected")

    def setdefault(self, *a):
        raise RuntimeError("injected")


def test_pending_operations_never_raise_and_leave_nothing_matchable(rigs):  # noqa: F811
    rig = rigs(start=False)
    s = socket.socket()
    try:
        real = rig.h._pending
        good = rig.h.add_pending(s.fileno())
        assert good.ino != 0
        rig.h._pending = _RaisingDict()
        bad = rig.h.add_pending(s.fileno())  # 表に足せない
        assert bad.ino == 0 and bad.dup_fd == -1
        assert rig.h.wait_ack(bad, (LO, 1), time.monotonic() + 0.02) is False
        rig.h.remove_pending(good)  # 除けない
        assert good.ino == 0 and good.dup_fd == -1
        rig.h._pending = real
        assert rig.h.faults() == ["add_pending", "remove_pending"]
        rig.h.remove_pending(good)  # 元の表からは除ける
        assert rig.h.pending_count() == 0
    finally:
        s.close()


def test_add_pending_without_any_entry_returns_unusable_token_that_never_waits(rigs, monkeypatch):  # noqa: F811
    """無効な entry すら作れないときも None を返さず、wait_ack は待たずに偽、除くのは何もしない。"""
    rig = rigs(start=False)

    def boom(*_a, **_k):
        raise MemoryError

    monkeypatch.setattr(H, "PendingEntry", boom)
    e = rig.h.add_pending(0)
    other = rig.h.add_pending(0)
    assert e is not None and e is not other and rig.h.pending_count() == 0
    assert isinstance(e, CS.PendingToken)
    assert rig.h.faults() == ["add_pending", "add_pending"]
    monkeypatch.undo()
    t0 = time.monotonic()
    assert rig.h.wait_ack(e, (LO, 1), time.monotonic() + 5) is False
    assert time.monotonic() - t0 < 0.5
    rig.h.remove_pending(e)


def test_add_pending_raises_when_not_even_the_unusable_token_can_be_made(rigs, monkeypatch):  # noqa: F811
    rig = rigs(start=False)

    def boom(*_a, **_k):
        raise MemoryError

    monkeypatch.setattr(H, "PendingEntry", boom)
    monkeypatch.setattr(H, "UnusableEntry", boom)
    with pytest.raises(MemoryError):
        rig.h.add_pending(0)


ADMISSION_BURST = 32


def test_legitimate_handoff_passes_behind_a_burst_of_external_connections(rigs):  # noqa: F811
    """外から proxy port へ ADMISSION_BURST 本が同時に来ても、その後ろに並んだ正規の
    handoff が ack 期限内に通る (accept thread が 1 本でも 1 接続は数 ms)。"""
    rig = rigs()
    outsiders = []
    try:
        for _ in range(ADMISSION_BURST):
            outsiders.append(socket.create_connection((LO, rig.port), timeout=5))
        r = tracee_handoff(rig)
        assert r["reply"] == 0, r
        assert r["ms"] < H.ACK_DEADLINE_SEC * 1000 * 0.5
        assert rig.wait_verdicts(ADMISSION_BURST + 1).count(H.V_ACCEPT) == 1
    finally:
        for c in outsiders:
            c.close()


class _DropFirstRequests:
    """最初の n 本の要求を出さない netlink socket (応答が来ない = lookup が timeout)。
    送った分の応答だけを実 kernel から受ける。"""

    def __init__(self, sock: socket.socket, n: int) -> None:
        self._sock = sock
        self.left = n

    def send(self, data: bytes, *a) -> int:
        if self.left > 0:
            self.left -= 1
            return len(data)
        return self._sock.send(data, *a)

    def __getattr__(self, name):
        return getattr(self._sock, name)


def _handoff_behind_silent_lookups(rig, n: int) -> dict:
    """lookup が timeout する外部接続 n 本の後ろに、正規の handoff を並べる。"""
    rig.h._diag.sock = _DropFirstRequests(rig.h._diag.sock, n)
    outsiders = []
    try:
        for _ in range(n):
            outsiders.append(socket.create_connection((LO, rig.port), timeout=5))
        return tracee_handoff(rig)
    finally:
        for c in outsiders:
            c.close()


SILENT_LOOKUP_BURST = 5


def test_legitimate_handoff_passes_behind_connections_whose_lookup_times_out(rigs):  # noqa: F811
    """外部接続の lookup が 1 本ごとに DIAG_RECV_TIMEOUT_SEC かかっても、その後ろに並んだ
    正規の handoff は SILENT_LOOKUP_BURST 本までなら ack 期限内に通る。"""
    rig = rigs()
    r = _handoff_behind_silent_lookups(rig, SILENT_LOOKUP_BURST)
    assert r["reply"] == 0, r
    assert r["ms"] < H.ACK_DEADLINE_SEC * 1000
    verdicts = rig.wait_verdicts(SILENT_LOOKUP_BURST + 1)
    assert verdicts.count(H.V_LOOKUP) == SILENT_LOOKUP_BURST
    assert verdicts.count(H.V_ACCEPT) == 1


def test_discarded_count_is_kept_in_the_handoff_history(rigs):  # noqa: F811
    """lookup が捨てた応答の件数を判定の履歴に残す (timeout が遅延か送りつけかの材料)。"""
    rig = rigs()

    class Stale:
        def __init__(self, diag) -> None:
            self._d = diag

        def lookup(self, src, dst):
            raise D.LookupFailed(D.R_TIMEOUT, discarded=3)

    rig.h._diag = Stale(rig.h._diag)
    c = socket.create_connection((LO, rig.port), timeout=5)
    try:
        assert rig.wait_verdicts(1) == [H.V_LOOKUP]
    finally:
        c.close()
    ev = rig.h.history()[0]
    assert ev["lookup"] == D.R_TIMEOUT and ev["discarded"] == 3


def test_stop_during_admission_never_acks(rigs):  # noqa: F811
    """判定の途中 (1 回目と 2 回目の lookup の間) で proxy の停止が始まったら、ack は立たず、
    tracee の connect は成功を返さない。"""
    box: dict = {}

    def stop_now(_conn, _peer):
        t = threading.Thread(target=lambda: box.update(stopped=box["rig"].proxy.stop(
            time.monotonic() + 5)))
        box["th"] = t
        t.start()
        end = time.monotonic() + 3
        while not box["rig"].proxy._is_stopping() and time.monotonic() < end:
            time.sleep(0.005)
        time.sleep(0.05)  # begin_stop の通知まで終わるのを待つ

    rig = rigs(between_lookups=stop_now)
    box["rig"] = rig
    t0 = time.monotonic()
    r = tracee_handoff(rig)
    box["th"].join(5)
    assert r["reply"] == "ECONNREFUSED", r
    assert time.monotonic() - t0 < H.ACK_DEADLINE_SEC  # 待ちは期限切れでなく停止で解放された
    # 解放された supervisor 側は判定の終わりを待たずに client を閉じるので、再 lookup で
    # 変化として落ちることもある。どちらでも ack は立たない
    assert rig.wait_verdicts(1)[0] in (H.V_STOPPING, H.V_CHANGED)
    assert box["stopped"] is True
    assert rig.closed == []


def test_admit_after_begin_stop_rejects_and_releases_waiters(rigs):  # noqa: F811
    rig = rigs()
    s = socket.socket()
    try:
        before = rig.h.add_pending(s.fileno())
        res: dict = {}
        th = threading.Thread(target=lambda: res.update(
            ok=rig.h.wait_ack(before, (LO, 1), time.monotonic() + 5)))
        t0 = time.monotonic()
        th.start()
        time.sleep(0.05)
        rig.h.begin_stop()
        th.join(2)
        assert res == {"ok": False} and time.monotonic() - t0 < 1.0
        after = rig.h.add_pending(s.fileno())  # 停止後の待ちも待たせない
        assert rig.h.wait_ack(after, (LO, 1), time.monotonic() + 5) is False
        # 停止後に来た正規の接続も、ack を立てる直前の検査で拒否される
        c = socket.socket()
        try:
            legit = rig.h.add_pending(c.fileno())
            c.connect((LO, rig.port))
            assert rig.wait_verdicts(1) == [H.V_STOPPING]
            assert rig.h.wait_ack(legit, c.getsockname(), time.monotonic() + 0.05) is False
            rig.h.remove_pending(legit)
        finally:
            c.close()
        rig.h.remove_pending(before)
        rig.h.remove_pending(after)
    finally:
        s.close()


def test_connection_accepted_while_stopping_is_closed_without_admission(rigs):  # noqa: F811
    rig = rigs(start=False)
    calls: list = []
    rig.proxy._admit = lambda conn: calls.append(conn) or True
    rig.proxy._stopping = True
    a, b = socket.socketpair()
    try:
        rig.proxy._on_accepted(a, time.monotonic())
        assert calls == [] and a.fileno() == -1
    finally:
        rig.proxy._stopping = False
        b.close()
