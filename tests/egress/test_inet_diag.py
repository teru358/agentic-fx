"""NETLINK_SOCK_DIAG の exact lookup と、その応答の検査、admission の probe。

lookup は実 netlink と 127.0.0.1 の実 socket で確かめる。応答の検査 (長さ・type・seq・
NLMSG_ERROR・tuple) は、kernel が実際に返した応答の byte 列を 1 箇所ずつ壊して確かめる。
"""
from __future__ import annotations

import errno
import fcntl
import os
import socket
import struct
import time

import pytest

from agentic_fx.egress import inet_diag as D
from agentic_fx.egress.proxy import PROXY_START_FAILED, ProxyStartFailed

LO = "127.0.0.1"
SO_COOKIE = 57


def _require_diag() -> None:
    try:
        s = D.open_diag_socket()
    except ProxyStartFailed as e:
        if os.environ.get("AFX_REQUIRE_SANDBOX_PROBES") == "1":
            pytest.fail(f"NETLINK_SOCK_DIAG を作れない: {e}")
        pytest.skip(f"NETLINK_SOCK_DIAG を作れない: {e}")
    s.close()


@pytest.fixture
def diag():
    _require_diag()
    sock = D.open_diag_socket()
    try:
        yield D.InetDiag(sock)
    finally:
        sock.close()


@pytest.fixture
def pair():
    ls = socket.socket()
    ls.bind((LO, 0))
    ls.listen(8)
    c = socket.socket()
    c.connect(ls.getsockname())
    a, peer = ls.accept()
    try:
        yield c, a, peer, ls.getsockname()
    finally:
        for s in (c, a, ls):
            s.close()


def so_cookie(s: socket.socket) -> int:
    return struct.unpack("Q", s.getsockopt(socket.SOL_SOCKET, SO_COOKIE, 8))[0]


def test_lookup_returns_inode_cookie_and_state(diag, pair):
    c, a, peer, server = pair
    cl = diag.lookup(peer, server)
    assert cl.family == socket.AF_INET
    assert cl.state == D.TCP_ESTABLISHED
    assert cl.inode == os.fstat(c.fileno()).st_ino
    assert cl.cookie == so_cookie(c)
    sv = diag.lookup(server, peer)
    assert sv.state == D.TCP_ESTABLISHED
    assert sv.inode == os.fstat(a.fileno()).st_ino
    assert sv.cookie == so_cookie(a)


def test_lookup_of_absent_tuple_fails_with_errno(diag, pair):
    _c, _a, peer, server = pair
    with pytest.raises(D.LookupFailed) as ei:
        diag.lookup((LO, 1), server)
    assert ei.value.reason == D.R_ERROR and ei.value.errno == errno.ENOENT
    # 失敗の後も同じ socket で引ける (応答の取り残しが次の lookup を壊さない)
    assert diag.lookup(peer, server).state == D.TCP_ESTABLISHED


def test_mapped_ipv6_client_is_not_inet(diag):
    """AF_INET6 socket から ::ffff:127.0.0.1 へ繋いだ接続は、AF_INET の exact lookup でも
    見つかるが、応答の family が AF_INET6 になる。これを lookup の失敗にする。"""
    ls = socket.socket()
    ls.bind((LO, 0))
    ls.listen(8)
    c6 = socket.socket(socket.AF_INET6)
    try:
        c6.connect(("::ffff:127.0.0.1", ls.getsockname()[1]))
        a, peer = ls.accept()
        with pytest.raises(D.LookupFailed) as ei:
            diag.lookup(peer, ls.getsockname())
        assert ei.value.reason == D.R_NOT_INET
        assert ei.value.family == socket.AF_INET6
        # server 側 (AF_INET の listener から accept した側) は AF_INET のまま
        assert diag.lookup(ls.getsockname(), peer).family == socket.AF_INET
        a.close()
    finally:
        c6.close()
        ls.close()


def test_stale_reply_is_drained_before_request(diag, pair):
    """期限切れで取り残された応答が次の lookup の seq 検査を壊さない。"""
    _c, _a, peer, server = pair
    diag.sock.send(D.build_request(peer, server, seq=0xABCDEF))
    time.sleep(0.05)
    got = diag.lookup(peer, server)
    assert got.state == D.TCP_ESTABLISHED


def test_lookup_times_out_without_reply():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        d = D.InetDiag(a, timeout=0.05)
        t0 = time.monotonic()
        with pytest.raises(D.LookupFailed) as ei:
            d.lookup((LO, 1), (LO, 2))
        took = time.monotonic() - t0
        assert ei.value.reason == D.R_TIMEOUT
        assert 0.04 <= took < 0.5
    finally:
        a.close()
        b.close()


def test_default_timeout_is_constant():
    assert D.DIAG_RECV_TIMEOUT_SEC == 0.1
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        assert D.InetDiag(a).timeout == D.DIAG_RECV_TIMEOUT_SEC
    finally:
        a.close()
        b.close()


# --- 応答の検査 -----------------------------------------------------------------

def _real_reply(diag, peer, server, seq=77) -> bytes:
    diag.sock.send(D.build_request(peer, server, seq=seq))
    return diag.sock.recv(65536)


def test_parse_accepts_real_reply(diag, pair):
    c, _a, peer, server = pair
    data = _real_reply(diag, peer, server)
    got = D.parse_response(data, seq=77, src=peer, dst=server)
    assert got.inode == os.fstat(c.fileno()).st_ino


def _mutate(data: bytes, off: int, fmt: str, value) -> bytes:
    b = bytearray(data)
    struct.pack_into(fmt, b, off, value)
    return bytes(b)


@pytest.mark.parametrize("case", [
    "short", "len_mismatch", "type", "seq", "sport", "dport", "src_addr", "dst_tail",
    "error_zero", "truncated_flag", "trailing", "bad_attr",
])
def test_parse_rejects_malformed(diag, pair, case):
    _c, _a, peer, server = pair
    data = _real_reply(diag, peer, server)
    kw: dict = {"seq": 77, "src": peer, "dst": server}
    want = D.R_MALFORMED
    if case == "short":
        data = data[:40]
        data = _mutate(data, 0, "=I", len(data))
    elif case == "len_mismatch":
        data = _mutate(data, 0, "=I", len(data) + 4)
    elif case == "type":
        data = _mutate(data, 4, "=H", 3)
    elif case == "seq":
        kw["seq"] = 78
        want = D.R_SEQ
    elif case == "sport":
        data = _mutate(data, 16 + 4, "!H", (peer[1] + 1) & 0xFFFF)
        want = D.R_TUPLE
    elif case == "dport":
        data = _mutate(data, 16 + 6, "!H", (server[1] + 1) & 0xFFFF)
        want = D.R_TUPLE
    elif case == "src_addr":
        data = _mutate(data, 16 + 8, "4s", socket.inet_aton("127.0.0.2"))
        want = D.R_TUPLE
    elif case == "dst_tail":
        data = _mutate(data, 16 + 24 + 4, "=I", 1)
        want = D.R_TUPLE
    elif case == "error_zero":
        data = struct.pack("=IHHII", 36, D.NLMSG_ERROR, 0, 77, 0) + struct.pack("=i", 0) + data[:16]
        want = D.R_ERROR
    elif case == "truncated_flag":
        kw["truncated"] = True
    elif case == "trailing":
        data = data + b"\0" * 20
    elif case == "bad_attr":
        data = data + struct.pack("=HH", 2, 1)
        data = _mutate(data, 0, "=I", len(data))
    with pytest.raises(D.LookupFailed) as ei:
        D.parse_response(data, **kw)
    assert ei.value.reason == want


def test_parse_error_message_carries_errno():
    msg = struct.pack("=IHHII", 36, D.NLMSG_ERROR, 0, 5, 0) + struct.pack("=i", -errno.ENOENT)
    msg += b"\0" * 16
    with pytest.raises(D.LookupFailed) as ei:
        D.parse_response(msg, seq=5, src=(LO, 1), dst=(LO, 2))
    assert ei.value.reason == D.R_ERROR and ei.value.errno == errno.ENOENT


# --- socket の作成と probe ------------------------------------------------------

def test_open_diag_socket_is_cloexec_netlink():
    _require_diag()
    s = D.open_diag_socket()
    try:
        assert s.family == socket.AF_NETLINK
        assert s.proto == D.NETLINK_SOCK_DIAG
        assert fcntl.fcntl(s.fileno(), fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    finally:
        s.close()


def _refuse(*_a, **_k):
    raise OSError(errno.EAFNOSUPPORT, "netlink refused")


def test_open_diag_socket_failure_is_proxy_start_failed():
    with pytest.raises(ProxyStartFailed) as ei:
        D.open_diag_socket(socket_factory=_refuse)
    assert ei.value.reason == PROXY_START_FAILED


def test_inet_diag_probe():
    _require_diag()
    before = set(os.listdir("/proc/self/fd"))
    D.probe_inet_diag()
    assert set(os.listdir("/proc/self/fd")) == before
    with pytest.raises(ProxyStartFailed) as ei:
        D.probe_inet_diag(socket_factory=_refuse)
    assert ei.value.reason == PROXY_START_FAILED
    assert set(os.listdir("/proc/self/fd")) == before


def test_probe_fails_when_lookup_cannot_answer():
    """netlink の代わりに応答しない socket を渡すと、probe は inode を確かめられず失敗する。"""
    held: list[socket.socket] = []

    def silent(*_a, **_k):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        held.append(b)
        return a

    try:
        with pytest.raises(ProxyStartFailed):
            D.probe_inet_diag(socket_factory=silent)
    finally:
        for b in held:
            b.close()


# --- 応答の送信元と multi-part ---------------------------------------------------

@pytest.mark.parametrize("case", ["multi", "done", "nlmsg_pid"])
def test_parse_rejects_multipart_and_foreign_pid(diag, pair, case):
    _c, _a, peer, server = pair
    data = _real_reply(diag, peer, server)
    if case == "multi":
        data = _mutate(data, 6, "=H", struct.unpack_from("=H", data, 6)[0] | D.NLM_F_MULTI)
    elif case == "done":
        data = _mutate(data, 4, "=H", D.NLMSG_DONE)
    else:
        data = _mutate(data, 12, "=I", 4242)
    with pytest.raises(D.LookupFailed) as ei:
        D.parse_response(data, seq=77, src=peer, dst=server,
                         portid=diag.sock.getsockname()[0])
    assert ei.value.reason == D.R_MALFORMED


class _Spoof:
    """実 socket の応答はそのまま、recvmsg の送信元 sockaddr_nl だけを差し替える。"""

    def __init__(self, sock: socket.socket, addr) -> None:
        self._sock = sock
        self._addr = addr

    def recvmsg(self, *a):
        data, anc, flags, _addr = self._sock.recvmsg(*a)
        return data, anc, flags, self._addr

    def __getattr__(self, name):
        return getattr(self._sock, name)


@pytest.mark.parametrize("addr,ok", [((0, 0), True), ((4242, 0), False), (None, False)])
def test_lookup_requires_kernel_sender(diag, pair, addr, ok):
    _c, _a, peer, server = pair
    d = D.InetDiag(_Spoof(diag.sock, addr))  # type: ignore[arg-type]
    if ok:
        assert d.lookup(peer, server).state == D.TCP_ESTABLISHED
        return
    with pytest.raises(D.LookupFailed) as ei:
        d.lookup(peer, server)
    assert ei.value.reason == D.R_MALFORMED


@pytest.mark.parametrize("addr", [(0, 1), (0, 0x10), (1, 0)])
def test_lookup_requires_kernel_sender_with_no_groups(diag, pair, addr):
    _c, _a, peer, server = pair
    d = D.InetDiag(_Spoof(diag.sock, addr))  # type: ignore[arg-type]
    with pytest.raises(D.LookupFailed) as ei:
        d.lookup(peer, server)
    assert ei.value.reason == D.R_MALFORMED


class _EditFirst:
    """最初に受けた datagram だけ書き換える (その前に旧 seq の応答を 1 本差し込んである)。"""

    def __init__(self, sock: socket.socket, peer, server, edit, addr=None) -> None:
        self._late = _LateReply(sock, peer, server, stale=1)
        self._edit = edit
        self._addr = addr
        self._first = True

    def recvmsg(self, *a):
        data, anc, flags, addr = self._late.recvmsg(*a)
        if self._first:
            self._first = False
            data = self._edit(data)
            if self._addr is not None:
                addr = self._addr
        return data, anc, flags, addr

    def __getattr__(self, name):
        return getattr(self._late, name)


@pytest.mark.parametrize("case", ["sender_groups", "sender_pid", "foreign_portid", "multi", "done"])
def test_source_is_checked_before_the_seq_so_a_forged_stale_datagram_is_not_discarded(
        diag, pair, case):
    """seq が違っても、送信元・port id・multi-part の検査に落ちる datagram は捨てずに失敗にする。"""
    _c, _a, peer, server = pair
    edit = {
        "sender_groups": lambda d: d, "sender_pid": lambda d: d,
        "foreign_portid": lambda d: _mutate(d, 12, "=I", 4242),
        "multi": lambda d: _mutate(d, 6, "=H", struct.unpack_from("=H", d, 6)[0] | D.NLM_F_MULTI),
        "done": lambda d: _mutate(d, 4, "=H", D.NLMSG_DONE),
    }[case]
    addr = {"sender_groups": (0, 1), "sender_pid": (4242, 0)}.get(case)
    d = D.InetDiag(_EditFirst(diag.sock, peer, server, edit, addr))  # type: ignore[arg-type]
    with pytest.raises(D.LookupFailed) as ei:
        d.lookup(peer, server)
    assert ei.value.reason == D.R_MALFORMED


def test_genuine_datagram_with_another_seq_is_discarded(diag, pair):
    _c, _a, peer, server = pair
    d = D.InetDiag(_EditFirst(diag.sock, peer, server, lambda x: x))  # type: ignore[arg-type]
    assert d.lookup(peer, server).state == D.TCP_ESTABLISHED


# --- 遅れて届いた旧 seq の応答 -----------------------------------------------------

class _LateReply:
    """send のたびに、直前に旧 seq の要求を 1 本余分に送る。drain の後 (= 送信の最中) に
    旧い応答が届いた状態を、実 kernel の応答で作る。"""

    def __init__(self, sock: socket.socket, peer, server, *, stale: int) -> None:
        self._sock = sock
        self._peer, self._server = peer, server
        self._stale = stale

    def send(self, data: bytes, *a) -> int:
        for i in range(self._stale):
            self._sock.send(D.build_request(self._peer, self._server, seq=0xABC000 + i))
        return self._sock.send(data, *a)

    def __getattr__(self, name):
        return getattr(self._sock, name)


@pytest.mark.parametrize("stale", [1, 3])
def test_late_stale_reply_is_skipped_not_fatal(diag, pair, stale):
    _c, _a, peer, server = pair
    d = D.InetDiag(_LateReply(diag.sock, peer, server, stale=stale))  # type: ignore[arg-type]
    for _ in range(2):
        assert d.lookup(peer, server).state == D.TCP_ESTABLISHED


def test_stale_reply_after_timeout_does_not_break_next_lookup(diag, pair):
    """timeout した lookup の応答が後から届いても、次の lookup は自分の応答を受ける。"""
    _c, _a, peer, server = pair

    class Mute(_LateReply):
        muted = True

        def send(self, data: bytes, *a) -> int:
            if self.muted:
                return len(data)  # 要求を出さない = 応答なしで timeout
            return self._sock.send(data, *a)

    m = Mute(diag.sock, peer, server, stale=0)
    d = D.InetDiag(m, timeout=0.05)  # type: ignore[arg-type]
    with pytest.raises(D.LookupFailed) as ei:
        d.lookup(peer, server)
    assert ei.value.reason == D.R_TIMEOUT
    # その要求の応答が、drain の後で届いたのと同じ状態
    diag.sock.send(D.build_request(peer, server, seq=d._seq))
    m.muted = False
    time.sleep(0.02)
    assert d.lookup(peer, server).state == D.TCP_ESTABLISHED


def test_only_stale_replies_end_at_the_deadline(diag, pair):
    _c, _a, peer, server = pair

    class Wrong(_LateReply):
        def send(self, data: bytes, *a) -> int:
            return self._sock.send(D.build_request(self._peer, self._server, seq=0xDEAD00))

    d = D.InetDiag(Wrong(diag.sock, peer, server, stale=0), timeout=0.2)  # type: ignore[arg-type]
    t0 = time.monotonic()
    with pytest.raises(D.LookupFailed) as ei:
        d.lookup(peer, server)
    assert ei.value.reason == D.R_TIMEOUT
    assert 0.15 <= time.monotonic() - t0 < 1.0


def test_lookup_reuses_one_poll_object(diag, pair, monkeypatch):
    """poll object は socket ごとに 1 度だけ作る (accept thread が lookup ごとに作り直さない)。"""
    _c, _a, peer, server = pair
    made: list = []
    real = D.select.poll

    def counting():
        made.append(1)
        return real()

    monkeypatch.setattr(D.select, "poll", counting)
    d = D.InetDiag(diag.sock)
    for _ in range(3):
        assert d.lookup(peer, server).state == D.TCP_ESTABLISHED
    assert len(made) == 1


@pytest.mark.parametrize("stale", [0, 2, 5, D._DRAIN_MAX + 6])
def test_timeout_records_how_many_stale_replies_were_discarded(diag, pair, stale):
    """自分の seq でない応答を捨てた件数を、timeout の失敗に残す (遅延と攻撃を区別する材料)。"""
    _c, _a, peer, server = pair

    class StaleOnly(_LateReply):
        def send(self, data: bytes, *a) -> int:
            for i in range(self._stale):
                self._sock.send(D.build_request(self._peer, self._server, seq=0xBEE000 + i))
            return len(data)  # 自分の要求は出さない = 自分の応答は来ない

    d = D.InetDiag(StaleOnly(diag.sock, peer, server, stale=stale), timeout=0.1)  # type: ignore[arg-type]
    with pytest.raises(D.LookupFailed) as ei:
        d.lookup(peer, server)
    assert ei.value.reason == D.R_TIMEOUT
    # 捨てる回数の上限に達したときは、上限まで捨てた件数が残る
    want = min(stale, D._DRAIN_MAX)
    assert ei.value.discarded == want
    assert (f"discarded={want}" in str(ei.value)) is (want > 0)
