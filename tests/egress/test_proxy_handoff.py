"""proxy の同期 handoff: INET_DIAG の inode + tuple で、supervisor が代行した connect と
proxy が accept した socket が同じ接続の両端であることを確かめる。

ここでは supervisor の代わりに、同じ順序 (pending の追加 → 複製への connect →
getsockname → ack の待ち → 不成立なら shutdown → pending の削除) を踏む役 `sv_handoff`
を使う。複製は自分の子 process が作った socket を `pidfd_getfd` で取ったもの。proxy は
実 `ConnectProxy` で、判定の差し込み口に `SyncHandoff.admit` を渡す。netlink・TCP・
port の再利用・RST / FIN は全部実物 (127.0.0.1 の一時 port)。
"""
from __future__ import annotations

import errno
import os
import socket
import struct
import threading
import time
from contextlib import ExitStack

import pytest

from agentic_fx.egress import handoff as H
from agentic_fx.egress import inet_diag as D
from agentic_fx.egress import proxy as px
from agentic_fx.egress.proxy import ProxyStartFailed
from tests.egress._handoff_peers import ExternalRole, TraceeRole, free_port
from tests.egress.test_proxy import StubResolver, request

LO = "127.0.0.1"


def _require_diag() -> socket.socket:
    try:
        return D.open_diag_socket()
    except ProxyStartFailed as e:
        if os.environ.get("AFX_REQUIRE_SANDBOX_PROBES") == "1":
            pytest.fail(f"NETLINK_SOCK_DIAG を作れない: {e}")
        pytest.skip(f"NETLINK_SOCK_DIAG を作れない: {e}")


def fd_set() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


class Rig:
    """1 mission 分: netlink socket、handoff、proxy listener、proxy。"""

    def __init__(self, *, start: bool = True, **kw) -> None:
        self.nl = _require_diag()
        self.h = H.SyncHandoff(D.InetDiag(self.nl), **kw)
        self.resolver = StubResolver()
        self.closed: list = []
        self.proxy = px.ConnectProxy(px.create_listener(), self.resolver, admit=self.h.admit,
                                     on_tunnel_closed=self.closed.append,
                                     on_stopping=self.h.begin_stop)
        self.port = self.proxy.port
        self.peers: list = []
        if start:
            self.proxy.start()

    def tracee(self) -> TraceeRole:
        t = TraceeRole()
        self.peers.append(t)
        return t

    def external(self, local_port: int | None = None, family: str = "inet") -> ExternalRole:
        e = ExternalRole(free_port() if local_port is None else local_port, self.port, family)
        self.peers.append(e)
        return e

    def wait_verdicts(self, n: int, timeout: float = 5.0) -> list[str]:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            v = self.h.verdicts()
            if len(v) >= n:
                return v
            time.sleep(0.005)
        return self.h.verdicts()

    def close(self) -> None:
        try:
            assert self.proxy.stop(time.monotonic() + 5)
        finally:
            for p in self.peers:
                p.close()
            self.nl.close()


@pytest.fixture
def rigs():
    fds = fd_set()
    threads = set(threading.enumerate())
    made: list[Rig] = []

    def make(**kw) -> Rig:
        r = Rig(**kw)
        made.append(r)
        return r

    try:
        yield make
    finally:
        for r in made:
            r.close()
    for r in made:
        assert r.h.pending_count() == 0, "pending の表に entry が残った"
    end = time.monotonic() + 3
    while time.monotonic() < end and set(threading.enumerate()) - threads:
        time.sleep(0.01)
    assert not (set(threading.enumerate()) - threads), "thread が残った"
    assert fd_set() == fds, "fd が残った"


def _shut(fd: int) -> None:
    s = socket.socket(fileno=os.dup(fd))
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    finally:
        s.close()


def _is_shut_down(fd: int) -> bool:
    s = socket.socket(fileno=os.dup(fd))
    try:
        s.send(b"x", socket.MSG_NOSIGNAL | socket.MSG_DONTWAIT)
        return False
    except OSError as e:
        return e.errno == errno.EPIPE
    finally:
        s.close()


def sv_handoff(h: H.SyncHandoff, port: int, dup: int, *, bind_port: int | None = None,
               register_twice: bool = False, before_connect=None, between=None,
               ack_sec: float = H.ACK_DEADLINE_SEC) -> dict:
    """supervisor の段 10b〜12 の順序を踏む役。複製 fd は呼び出し側が閉じる。"""
    lock = threading.Lock()  # supervisor の lock の代役 (順序: supervisor → pending)
    with lock:
        entries = [h.add_pending(dup)]
        if register_twice:
            entries.append(h.add_pending(dup))
    out: dict = {}
    s = socket.socket(fileno=os.dup(dup))
    try:
        if bind_port is not None:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((LO, bind_port))
        if before_connect is not None:
            before_connect(entries[0])
        t0 = time.perf_counter()
        rc = s.connect_ex((LO, port))
        if rc not in (0, errno.EINPROGRESS):
            out["reply"] = errno.errorcode[rc]
            return out
        client = s.getsockname()
        out["client"] = client
        if between is not None:
            between(entries[0])
        ok = h.wait_ack(entries[0], client, time.monotonic() + ack_sec)
        out["ms"] = (time.perf_counter() - t0) * 1e3
        if not ok:
            _shut(dup)
            out["shut_down"] = _is_shut_down(dup)
            out["reply"] = "ECONNREFUSED"
        else:
            out["reply"] = 0
        return out
    finally:
        s.close()
        with lock:
            for e in entries:
                h.remove_pending(e)


def run_bg(fn) -> tuple[threading.Thread, dict]:
    box: dict = {}
    t = threading.Thread(target=lambda: box.update(fn()))
    t.start()
    return t, box


def tracee_handoff(rig: Rig, **kw) -> dict:
    t = rig.tracee()
    dup = t.dup()
    try:
        return sv_handoff(rig.h, rig.port, dup, **kw)
    finally:
        os.close(dup)


class Hold:
    """accept 直後の seam で、指定回目の接続だけ止める。"""

    def __init__(self, nth: int = 1) -> None:
        self.nth = nth
        self.calls = 0
        self.held = threading.Event()
        self.go = threading.Event()
        self.action = None

    def __call__(self, conn: socket.socket, peer: tuple) -> None:
        self.calls += 1
        if self.calls != self.nth:
            return
        self.held.set()
        if self.action is not None:
            self.action(conn, peer)
        else:
            assert self.go.wait(5)


# --- 受入 39: 逆順の port 再利用 ---------------------------------------------------

@pytest.mark.parametrize("mode", ["rst", "fin"])
@pytest.mark.parametrize("order", ["R1", "R2", "R5"])
def test_reverse_port_reuse_is_rejected(rigs, order, mode):
    p = free_port()
    if order == "R1":
        # 外部 E が先に同じ local port で接続して拒否され、閉じた後に正規の handoff
        rig = rigs()
        e = rig.external(p)
        assert e.info["connect"] == "ok"
        assert rig.wait_verdicts(1) == [H.V_NO_MATCH]
        e.close_conn(mode)
        r = tracee_handoff(rig, bind_port=p)
        assert r["reply"] == 0, r
        assert rig.wait_verdicts(2) == [H.V_NO_MATCH, H.V_ACCEPT]
    elif order == "R2":
        # proxy が accept する前に E が接続して閉じ、同じ port から正規が接続する
        rig = rigs(start=False)
        e = rig.external(p)
        assert e.info["connect"] == "ok"
        e.close_conn(mode)
        r = tracee_handoff(rig, bind_port=p, between=lambda _e: rig.proxy.start())
        assert r["reply"] == 0, r
        assert rig.wait_verdicts(2) == [H.V_NO_PEER, H.V_ACCEPT]
    else:
        # E を accept した直後で止め、E が閉じて正規が同じ port から接続した後に E を判定する。
        # このとき server 側の tuple は正規の未 accept の socket (inode 0) を指す
        hold = Hold()
        rig = rigs(after_accept=hold)
        e = rig.external(p)
        assert e.info["connect"] == "ok"
        assert hold.held.wait(5)
        e.close_conn(mode)
        r = tracee_handoff(rig, bind_port=p, between=lambda _e: hold.go.set())
        assert r["reply"] == 0, r
        assert rig.wait_verdicts(2) == [H.V_NO_MATCH, H.V_ACCEPT]
        first = rig.h.history()[0]
        assert first["server_inode"] == 0 and first["accepted_inode"] != 0
    # E は判定で 1 件も一致せず、要求行を読まれずに閉じられた。正規の 1 本だけが tunnel
    assert rig.h.pending_count() == 0
    assert rig.resolver.calls == []
    end = time.monotonic() + 2
    while rig.proxy.active_tunnels() != 1 and time.monotonic() < end:
        time.sleep(0.01)
    assert rig.proxy.active_tunnels() <= 1


def test_inode_not_cookie_decides_identity(rigs):
    """bucket の索引 (cookie) と tuple では外部 E と区別できない配置でも、inode が拒否する。

    索引関数を定数にして E の cookie の bucket に正規の entry を置き、その複製を E と
    同じ local port に bind しておく (getsockname が E の peer と一致する)。"""
    hold = Hold()
    rig = rigs(bucket_key=lambda _ck: 0, after_accept=hold)
    p = free_port()
    e = rig.external(p)
    assert e.info["connect"] == "ok"
    assert hold.held.wait(5)
    t = rig.tracee()
    dup = t.dup()
    try:
        s = socket.socket(fileno=os.dup(dup))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((LO, p))
        # 複製の tuple は E の peer と同じ (tuple では区別できない)
        assert s.getsockname() == (LO, p)
        s.close()
        entry = rig.h.add_pending(dup)
        hold.go.set()
        assert rig.wait_verdicts(1) == [H.V_NO_MATCH]
        ev = rig.h.history()[0]
        # cookie の bucket は空でなく、そこに居た entry を inode が落とした
        assert ev["bucket"] == 1 and ev["matches"] == 0
        assert ev["client_inode"] != entry.ino
        rig.h.remove_pending(entry)
    finally:
        os.close(dup)
    assert e.recv(1.0) in (0, errno.ECONNRESET)


def test_bucket_collision_two_legit(rigs):
    rig = rigs(bucket_key=lambda _ck: 0)
    both = threading.Barrier(2, timeout=5)
    ta, ra = run_bg(lambda: tracee_handoff(rig, before_connect=lambda _e: both.wait()))
    tb, rb = run_bg(lambda: tracee_handoff(rig, before_connect=lambda _e: both.wait()))
    ta.join(10)
    tb.join(10)
    assert ra["reply"] == 0 and rb["reply"] == 0, (ra, rb)
    assert rig.wait_verdicts(2) == [H.V_ACCEPT, H.V_ACCEPT]
    hist = rig.h.history()
    assert hist[0]["bucket"] == 2
    assert all(ev["matches"] == 1 for ev in hist)


def test_external_while_bucket_populated(rigs):
    rig = rigs(bucket_key=lambda _ck: 0)
    t = rig.tracee()
    dup = t.dup()
    try:
        entry = rig.h.add_pending(dup)
        e = rig.external()
        assert e.info["connect"] == "ok"
        assert rig.wait_verdicts(1) == [H.V_NO_MATCH]
        ev = rig.h.history()[0]
        # 正規の複製はまだ connect していないので、tuple も inode も一致しない
        assert ev["bucket"] == 1 and ev["matches"] == 0
        rig.h.remove_pending(entry)
    finally:
        os.close(dup)
    assert e.recv(1.0) in (0, errno.ECONNRESET)


def test_duplicate_entry_is_rejected(rigs):
    rig = rigs()
    r = tracee_handoff(rig, register_twice=True)
    assert r["reply"] == "ECONNREFUSED", r
    assert r["shut_down"] is True
    assert rig.wait_verdicts(1) == [H.V_MULTIPLE]
    assert rig.h.history()[0]["matches"] == 2
    assert 900 <= r["ms"] < 1500


# --- 受入 39: accept と lookup の間の切断 ----------------------------------------

@pytest.mark.parametrize("case", ["D1", "D2"])
def test_disconnect_between_accept_and_lookup(rigs, case):
    hold = Hold()
    rig = rigs(after_accept=hold)
    if case == "D1":
        made = threading.Event()
        box: dict = {}

        def cut(_conn, _peer):
            assert made.wait(5)
            box["e"].close_conn("rst")
            time.sleep(0.05)

        hold.action = cut
        box["e"] = rig.external()
        made.set()
        assert box["e"].info["connect"] == "ok"
        assert rig.wait_verdicts(1) == [H.V_LOOKUP]
        assert rig.h.history()[0]["lookup"] == D.R_ERROR
    else:
        box = {}
        ready = threading.Event()

        def cut(_conn, _peer):
            assert ready.wait(5)
            _shut(box["entry"].dup_fd)
            time.sleep(0.05)

        hold.action = cut

        def between(entry):
            box["entry"] = entry
            ready.set()

        r = tracee_handoff(rig, between=between)
        assert r["reply"] == "ECONNREFUSED", r
        assert rig.wait_verdicts(1) == [H.V_STATE]
        ev = rig.h.history()[0]
        assert ev["client_state"] != D.TCP_ESTABLISHED or ev["server_state"] != D.TCP_ESTABLISHED


def test_mapped_ipv6_external_is_rejected(rigs):
    rig = rigs()
    e = rig.external(family="inet6")
    assert e.info["connect"] == "ok"
    assert rig.wait_verdicts(1) == [H.V_NOT_INET]
    assert rig.h.history()[0]["family"] == socket.AF_INET6
    assert e.recv(1.0) in (0, errno.ECONNRESET)
    assert rig.resolver.calls == []


def _capeff() -> int:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("CapEff:"):
                return int(line.split()[1], 16)
    return -1


def test_handoff_works_with_capeff_zero(rigs):
    if _capeff() != 0:
        pytest.skip("試験 process の CapEff が 0 でない (CAP_NET_ADMIN 無しの確認にならない)")
    rig = rigs()
    r = tracee_handoff(rig)
    assert r["reply"] == 0, r
    ev = rig.h.history()[0]
    assert ev["verdict"] == H.V_ACCEPT and ev["client_inode"] != 0


def test_relookup_change_is_rejected(rigs):
    """1 回目と 2 回目の lookup の間に、同じ tuple の client 側を別の socket に差し替える。"""
    swapped: dict = {}
    p = free_port()
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((LO, p))

    def swap(_conn, _peer):
        if swapped:
            return
        # 正規の client を RST で消し、同じ local port から別の socket で繋ぎ直す
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
        n = socket.socket()
        n.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        n.bind((LO, p))
        n.connect((LO, rig.port))
        swapped["new"] = n

    rig = rigs(between_lookups=swap)
    try:
        entry = rig.h.add_pending(s.fileno())
        s.connect((LO, rig.port))
        ok = rig.h.wait_ack(entry, s.getsockname(), time.monotonic() + 1.0)
        rig.h.remove_pending(entry)
        assert ok is False
        assert rig.wait_verdicts(1)[0] == H.V_CHANGED
    finally:
        s.close()
        if "new" in swapped:
            swapped["new"].close()


# --- 受入 32: supervisor を経ない接続 ---------------------------------------------

def test_unsupervised_connection_closed_without_reading(rigs):
    rig = rigs()
    c = socket.create_connection((LO, rig.port), timeout=5)
    try:
        c.sendall(request())
        got = b""
        try:
            got = c.recv(65536)
        except ConnectionResetError:
            pass
        assert got == b""
    finally:
        c.close()
    assert rig.wait_verdicts(1) == [H.V_NO_MATCH]
    assert rig.proxy.active_tunnels() == 0
    assert rig.closed == []
    assert rig.resolver.calls == []


# --- 部品の契約 -------------------------------------------------------------------

def test_remove_pending_drops_every_copy(rigs):
    rig = rigs(bucket_key=lambda _ck: 7)
    s = socket.socket()
    try:
        a = rig.h.add_pending(s.fileno())
        b = rig.h.add_pending(s.fileno())  # 別 entry (同じ fd)
        rig.h.remove_pending(a)
        rig.h.remove_pending(a)  # 2 回目は何もしない
        assert rig.h.pending_count() == 1
        rig.h.remove_pending(b)
        assert rig.h.pending_count() == 0
    finally:
        s.close()


class _NoAdd(dict):
    """表への追加だけが失敗する pending の表。"""

    def setdefault(self, *_a):
        raise MemoryError


def test_remove_pending_releases_waiting_even_without_bucket(rigs):
    """表に載らなかった entry・二重に除いた entry も、待ちの一覧から外れる (begin_stop が
    無関係な event を set し続けない)。"""
    rig = rigs(start=False)
    s = socket.socket()
    try:
        rig.h._pending = _NoAdd()
        lost = rig.h.add_pending(s.fileno())
        assert lost.ino == 0
        rig.h.remove_pending(lost)
        assert rig.h._waiting == set()
        rig.h._pending = {}
        twice = rig.h.add_pending(s.fileno())
        rig.h.remove_pending(twice)
        rig.h.remove_pending(twice)
        assert rig.h._waiting == set()
    finally:
        s.close()


def test_entry_that_could_not_be_built_is_a_distinct_token_that_never_waits(rigs, monkeypatch):  # noqa: F811
    """entry の生成そのものが失敗しても、返る印は呼び出しごとに別物で、待ちの一覧には載らず、
    wait_ack は待たずに偽、除いても何も起きない。"""
    rig = rigs(start=False)

    def boom(*_a, **_k):
        raise MemoryError

    monkeypatch.setattr(H, "PendingEntry", boom)
    s = socket.socket()
    try:
        a = rig.h.add_pending(s.fileno())
        b = rig.h.add_pending(s.fileno())
        assert isinstance(a, H.UnusableEntry) and a is not b
        assert rig.h._waiting == set() and rig.h.pending_count() == 0
        monkeypatch.undo()
        t0 = time.monotonic()
        assert rig.h.wait_ack(a, (LO, 1), time.monotonic() + 5) is False
        assert time.monotonic() - t0 < 0.5
        rig.h.remove_pending(a)
        rig.h.remove_pending(b)
        assert rig.h.faults().count("add_pending") == 2
    finally:
        s.close()


def test_admission_in_progress_does_not_block_pending_operations(rigs):
    """判定が 1 回目と 2 回目の lookup の間で止まっていても、別の接続の add_pending・
    wait_ack・remove_pending は待たされない (判定は lookup の間 pending の lock を持たない)。"""
    hold = Hold()
    rig = rigs(between_lookups=hold)
    s = socket.socket()
    other = socket.socket()
    try:
        entry = rig.h.add_pending(s.fileno())
        s.connect((LO, rig.port))
        assert hold.held.wait(5)
        took: dict = {}

        def ops() -> None:
            t0 = time.monotonic()
            e2 = rig.h.add_pending(other.fileno())
            took["add"] = time.monotonic() - t0
            took["ack"] = rig.h.wait_ack(e2, (LO, 1), time.monotonic() + 0.05)
            took["wait"] = time.monotonic() - t0
            rig.h.remove_pending(e2)
            took["all"] = time.monotonic() - t0

        th = threading.Thread(target=ops)
        th.start()
        th.join(0.5)
        done = not th.is_alive()
        hold.go.set()
        th.join(10)
        assert done, took
        assert took["ack"] is False and took["all"] < 0.2, took
        assert rig.wait_verdicts(1) == [H.V_ACCEPT]
        assert rig.h.wait_ack(entry, s.getsockname(), time.monotonic() + 1) is True
        rig.h.remove_pending(entry)
    finally:
        hold.go.set()
        s.close()
        other.close()


def test_entry_removed_during_admission_is_not_acked(rigs):
    """lookup の間に supervisor が entry を除いたら、再 lookup が通っても ack は立てない。"""
    box: dict = {}

    def withdraw(_conn, _peer) -> None:
        rig.h.remove_pending(box["entry"])

    rig = rigs(between_lookups=withdraw)
    s = socket.socket()
    try:
        box["entry"] = entry = rig.h.add_pending(s.fileno())
        s.connect((LO, rig.port))
        assert rig.wait_verdicts(1) == [H.V_GONE]
        assert entry.acked is None and not entry.event.is_set()
        assert rig.h.wait_ack(entry, s.getsockname(), time.monotonic() + 0.05) is False
    finally:
        s.close()


def test_add_pending_on_broken_fd_never_matches_and_never_raises(rigs):
    rig = rigs()
    r, w = os.pipe()
    try:
        entry = rig.h.add_pending(r)
        assert entry.ino == 0 and entry.key is None
        assert rig.h.wait_ack(entry, (LO, 1), time.monotonic() + 0.05) is False
        rig.h.remove_pending(entry)
        entry2 = rig.h.add_pending(10_000)
        rig.h.remove_pending(entry2)
    finally:
        os.close(r)
        os.close(w)


def test_ack_tuple_mismatch_is_false(rigs):
    rig = rigs()
    s = socket.socket()
    try:
        entry = rig.h.add_pending(s.fileno())
        s.connect((LO, rig.port))
        assert rig.wait_verdicts(1) == [H.V_ACCEPT]
        assert rig.h.wait_ack(entry, (LO, 1), time.monotonic() + 0.05) is False
        assert rig.h.wait_ack(entry, s.getsockname(), time.monotonic() + 0.05) is True
        rig.h.remove_pending(entry)
    finally:
        s.close()


def test_admit_failure_is_recorded_as_reject(rigs):
    rig = rigs(start=False)
    c = socket.socket()
    c.close()
    assert rig.h.admit(c) is False
    assert rig.h.verdicts() == [H.V_NO_PEER]


def test_default_bucket_key_is_cookie_and_constants():
    assert H.ACK_DEADLINE_SEC == 1.0
    assert H.cookie_index(12345) == 12345


# --- §14: 所要 ---------------------------------------------------------------------

def test_handoff_latency(rigs, record_property):
    rig = rigs()
    ms: list[float] = []
    n = 200
    for _ in range(n):
        with ExitStack() as st:
            s = socket.socket()
            st.callback(s.close)
            r = sv_handoff(rig.h, rig.port, s.fileno())
            assert r["reply"] == 0, r
            ms.append(r["ms"])
    ms.sort()
    p50, p99, mx = ms[n // 2], ms[int(n * 0.99) - 1], ms[-1]
    record_property("handoff_ms", {"p50": p50, "p99": p99, "max": mx})
    print(f"\nhandoff n={n} p50={p50:.3f} ms p99={p99:.3f} ms max={mx:.3f} ms")
    assert rig.h.verdicts().count(H.V_ACCEPT) == n
