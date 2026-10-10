"""実 `ConnectSupervisor(proxy_handoff=h)` と実 `ConnectProxy(admit=h.admit)` の結合。

tracee は `tests/core/_notif_tracee.py` の factory が fork する子で、connect だけを
USER_NOTIF にする最小 filter の下にいる。proxy の listener と upstream 役は 127.0.0.1 の
一時 port、upstream は解決結果を固定の loopback に差し替えた代役 (外部へは出ない)。
"""
from __future__ import annotations

import errno
import os
import socket
import threading
import time

import pytest

from agentic_fx.core import connect_supervisor as CS
from agentic_fx.egress import handoff as H
from agentic_fx.egress import inet_diag as D
from agentic_fx.egress import proxy as px
from agentic_fx.egress.proxy import ProxyStartFailed
from tests.core._notif_tracee import Factory, Gate
from tests.core.test_connect_supervisor import fd_table, leaked, require_kernel, special_fds
from tests.egress._handoff_peers import ExternalRole, dup_from, free_port
from tests.egress.test_proxy import HOST, StubResolver, UpstreamRole, read_exact, request

pytestmark = pytest.mark.skipif(
    os.uname().machine != "x86_64", reason="connect supervisor は x86_64 Linux 専用")

LO = "127.0.0.1"


@pytest.fixture(scope="module")
def factory():
    require_kernel()
    f = Factory()
    yield f
    f.close()


class Mission:
    """1 mission 分の配線: netlink socket、handoff、proxy、supervisor、tracee。"""

    def __init__(self, factory: Factory, *, start_proxy: bool = True,
                 faults: CS.Faults | None = None, **handoff_kw) -> None:
        try:
            self.nl = D.open_diag_socket()
        except ProxyStartFailed as e:
            if os.environ.get("AFX_REQUIRE_SANDBOX_PROBES") == "1":
                pytest.fail(f"NETLINK_SOCK_DIAG を作れない: {e}")
            pytest.skip(f"NETLINK_SOCK_DIAG を作れない: {e}")
        self.h = H.SyncHandoff(D.InetDiag(self.nl), **handoff_kw)
        self.up = UpstreamRole()
        self.resolver = StubResolver()
        self.closed: list = []
        self.proxy = px.ConnectProxy(px.create_listener(), self.resolver, admit=self.h.admit,
                                     upstream_port=self.up.port,
                                     on_tunnel_closed=self.closed.append,
                                     on_stopping=self.h.begin_stop)
        if start_proxy:
            self.proxy.start()
        self.t = factory.spawn()
        self.sup = CS.ConnectSupervisor(CS.Destinations(self.proxy.port, None),
                                        proxy_handoff=self.h, faults=faults)
        self.sup.handoff(self.t.take_listener())
        self.peers: list = []
        self.extra: list[socket.socket] = []

    def connect(self, timeout: float = 5.0, **kw) -> dict:
        return self.t.call("connect", timeout=timeout, port=self.proxy.port, **kw)

    def close(self) -> None:
        try:
            self.sup.stop(time.monotonic() + 5)
            assert self.sup.join(5), "supervisor の thread が残った"
            self.sup.release()
            assert self.proxy.stop(time.monotonic() + 5)
        finally:
            for s in self.extra:
                s.close()
            for p in self.peers:
                p.close()
            self.t.close()
            self.up.close()
            self.nl.close()


@pytest.fixture
def mission(factory):
    before = fd_table()
    threads = set(threading.enumerate())
    made: list[Mission] = []

    def make(**kw) -> Mission:
        m = Mission(factory, **kw)
        made.append(m)
        return m

    try:
        yield make
    finally:
        for m in made:
            m.close()
    for m in made:
        assert m.h.pending_count() == 0, "pending の表に entry が残った"
    end = time.monotonic() + 3
    while time.monotonic() < end and set(threading.enumerate()) - threads:
        time.sleep(0.01)
    assert not (set(threading.enumerate()) - threads), "thread が残った"
    assert not leaked(before, fd_table()), "fd が残った"
    assert special_fds() == []


def _wait(pred, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def _proxy_records(sup: CS.ConnectSupervisor) -> list[dict]:
    return [r for r in sup.records if r.get("dest") == "proxy"]


# --- 受入 39 / 32: 端から端まで ---------------------------------------------------

@pytest.mark.parametrize("nonblock", [False, True], ids=["blocking", "nonblocking"])
def test_supervisor_proxy_handoff_end_to_end(mission, nonblock):
    m = mission()
    # 部分木の外からの接続が同時に来ても、正規の handoff だけが tunnel になる
    e = ExternalRole(free_port(), m.proxy.port)
    m.peers.append(e)
    assert e.info["connect"] == "ok"
    r = m.connect(keep=True, nonblock=nonblock)
    want = -errno.EINPROGRESS if nonblock else 0
    assert r["rc"] == want, r
    if nonblock:
        assert r["so_error"] == 0
    assert _wait(lambda: sorted(m.h.verdicts()) == sorted([H.V_NO_MATCH, H.V_ACCEPT]))
    assert e.recv(1.0) in (0, errno.ECONNRESET)
    rec = _proxy_records(m.sup)
    assert len(rec) == 1 and rec[0]["result"] == want and "ack" not in rec[0]
    # tracee の socket で CONNECT を送ると、固定 loopback の upstream 役まで届く
    s = socket.socket(fileno=dup_from(m.t.pid, r["fd"]))
    m.extra.append(s)
    s.setblocking(True)
    s.settimeout(5)
    s.sendall(request())
    head = read_exact(s, len(px.SUCCESS_RESPONSE))
    assert head == px.SUCCESS_RESPONSE
    up = m.up.accept()
    m.extra.append(up)
    s.sendall(b"hello")
    assert read_exact(up, 5) == b"hello"
    assert m.resolver.calls == [HOST]
    assert m.proxy.active_tunnels() == 1
    assert m.h.pending_count() == 0


# --- 受入 32: どの経路でも pending が空 ------------------------------------------

def _raise_at(point: str):
    def at(p: str, _nid: int) -> None:
        if p == point:
            raise RuntimeError("injected")
    return at


@pytest.mark.parametrize("path", ["accepted", "ack_timeout", "connect_failed",
                                  "supervisor_stop", "handler_exception"])
def test_pending_is_empty_after_every_path(mission, path):
    if path == "accepted":
        m = mission()
        assert m.connect()["rc"] == 0
    elif path == "ack_timeout":
        # listener はあるが accept しない: connect は成功し、ack が来ないまま期限
        m = mission(start_proxy=False)
        t0 = time.monotonic()
        assert m.connect()["rc"] == -errno.ECONNREFUSED
        assert 0.9 <= time.monotonic() - t0 < 1.8
        assert _proxy_records(m.sup)[0]["ack"] is False
    elif path == "connect_failed":
        # proxy の port に listener が居ない: raw connect が失敗し、ack は待たない
        m = mission()
        assert m.proxy.stop(time.monotonic() + 5)
        t0 = time.monotonic()
        assert m.connect()["rc"] == -errno.ECONNREFUSED
        assert time.monotonic() - t0 < 0.5
        assert _proxy_records(m.sup)[0]["connect"] == -errno.ECONNREFUSED
    elif path == "supervisor_stop":
        gate = Gate("after_commit")
        m = mission(faults=CS.Faults(at=gate))
        cid = m.t.start("connect", port=m.proxy.port)
        assert gate.wait()
        assert m.h.pending_count() == 1
        box: dict = {}
        th = threading.Thread(target=lambda: box.update(st=m.sup.stop(time.monotonic() + 5)))
        th.start()
        assert _wait(lambda: m.sup.state != CS.RUNNING)
        gate.release()
        th.join(6)
        # commit 済みの操作は stop 後も実値を返す
        r = m.t.result(cid)
        assert r is not None and r["rc"] == 0, r
        assert box["st"] == CS.CLOSED
    else:
        m = mission(faults=CS.Faults(at=_raise_at("before_ack")))
        r = m.connect()
        assert r["rc"] == -errno.EPERM, r
        assert m.sup.failure is not None
    assert _wait(lambda: m.h.pending_count() == 0)
    assert _wait(lambda: m.sup.in_flight == 0)


def test_proxy_stop_unblocks_waiters(mission):
    """proxy の停止が始まると ack は立たず、待っていた supervisor は ack 期限を待たずに
    ECONNREFUSED を返して待ちを残さない。"""
    held = threading.Event()
    go = threading.Event()
    calls = {"n": 0}

    def hold_first(_conn, _peer):
        calls["n"] += 1
        if calls["n"] == 1:
            held.set()
            go.wait(5)

    m = mission(after_accept=hold_first)
    # accept thread を外部接続の判定で止め、その間に正規の connect を backlog に入れる
    e = ExternalRole(free_port(), m.proxy.port)
    m.peers.append(e)
    assert held.wait(5)
    t0 = time.monotonic()
    cid = m.t.start("connect", port=m.proxy.port)
    assert _wait(lambda: m.h.pending_count() == 1)
    stopper = threading.Thread(target=m.proxy.stop, args=(time.monotonic() + 5,))
    stopper.start()
    time.sleep(0.1)
    go.set()
    stopper.join(6)
    r = m.t.result(cid)
    took = time.monotonic() - t0
    assert r is not None and r["rc"] == -errno.ECONNREFUSED, r
    assert 0.1 <= took < 0.9  # 停止で解放され、1 秒の期限切れまでは待たない
    assert _wait(lambda: m.h.pending_count() == 0)
    assert _wait(lambda: m.sup.in_flight == 0)
    assert m.proxy.active_tunnels() == 0
    assert H.V_ACCEPT not in m.h.verdicts()


def test_ack_deadline_matches_supervisor():
    """handoff 側の ack 期限は supervisor の定数と同じ (試験の期限が実際の挙動からずれない)。"""
    assert H.ACK_DEADLINE_SEC == CS.PROXY_ACK_DEADLINE_SEC


# --- add_pending が使えない entry を返しても、ack 待ちを飛ばして成功にしない ----------

class _Faulty:
    """実 handoff に委譲しつつ、add_pending の返り値だけを差し替える。"""

    def __init__(self, real: H.SyncHandoff, mode: str) -> None:
        self._real = real
        self._mode = mode
        self.waited = 0

    def add_pending(self, dup_fd: int):
        if self._mode == "none":
            return None
        if self._mode == "raise":
            raise MemoryError
        return object()  # PendingEntry でない: wait_ack は偽を返すはずの値

    def wait_ack(self, entry, client, deadline) -> bool:
        self.waited += 1
        return self._real.wait_ack(entry, client, deadline)

    def remove_pending(self, entry) -> None:
        self._real.remove_pending(entry)


@pytest.mark.parametrize("mode", ["none", "foreign"])
def test_unusable_pending_entry_is_refused_not_connected(mission, mode):
    m = mission()
    faulty = _Faulty(m.h, mode)
    m.sup._proxy = faulty
    r = m.connect()
    assert r["rc"] == -errno.ECONNREFUSED, r
    assert _proxy_records(m.sup)[0]["ack"] is False
    assert faulty.waited == 1  # entry の値に関わらず ack の待ちを通る
    assert _wait(lambda: m.sup.in_flight == 0)


def test_add_pending_exception_never_yields_a_connected_socket(mission):
    m = mission()
    m.sup._proxy = _Faulty(m.h, "raise")
    r = m.connect()
    assert r["rc"] < 0, r
    assert _wait(lambda: m.sup.in_flight == 0)
