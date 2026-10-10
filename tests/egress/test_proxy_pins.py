"""proxy・relay・upstream の契約のうち、既存の試験が観測していなかった点の固定。

socket はすべて 127.0.0.1 (と ::1) の一時 port の実物。
"""

from __future__ import annotations

import inspect
import os
import socket
import threading
import time

import pytest

from agentic_fx.egress import proxy as px
from agentic_fx.egress import relay as rl
from agentic_fx.egress import upstream as up
from agentic_fx.egress.relay import RelayEnd, TunnelState
from tests.egress.test_proxy import (
    HOST,
    StubResolver,
    accept_all,
    connect_client,
    open_tunnel,
    read_exact,
    read_to_eof,
    request,
    running_proxy,
    upstream_role,
    wait_until,
)
from tests.egress.test_upstream import _leak_guard, blackhole, closed_port  # noqa: F401

LO = "127.0.0.1"


def _no_pending_accept(srv: socket.socket) -> bool:
    srv.setblocking(False)
    try:
        c, _ = srv.accept()
    except BlockingIOError:
        return True
    c.close()
    return False


# --- 判定の差し込み口 ---


def test_admit_must_return_true_itself():
    resolver = StubResolver()
    with running_proxy(resolver, admit=lambda sock: 1) as proxy:
        c = connect_client(proxy)
        with c:
            c.sendall(request())
            assert read_to_eof(c) == b""
        assert resolver.calls == []


def test_admit_is_consulted_before_the_tunnel_limit():
    calls: list[int] = []

    def admit_first_32(sock):
        calls.append(1)
        return len(calls) <= px.MAX_TUNNELS

    with running_proxy(StubResolver(), admit=admit_first_32) as proxy:
        idle = [connect_client(proxy) for _ in range(px.MAX_TUNNELS)]
        try:
            assert wait_until(lambda: proxy.active_tunnels() == px.MAX_TUNNELS)
            extra = connect_client(proxy)
            with extra:
                # 判定で拒否された接続には、上限中でも 503 を含めて何も返さない
                assert read_to_eof(extra) == b""
            assert len(calls) == px.MAX_TUNNELS + 1
        finally:
            for c in idle:
                c.close()
        assert wait_until(lambda: proxy.active_tunnels() == 0)


# --- 上限の数え方 ---


def test_tunnel_is_counted_until_its_sockets_are_closed(monkeypatch):
    seen: list[int] = []
    holder: dict = {}
    real_close = rl.Tunnel.close

    def observing_close(self):
        seen.append(holder["proxy"].active_tunnels())
        real_close(self)

    monkeypatch.setattr(rl.Tunnel, "close", observing_close)
    with upstream_role() as role, running_proxy(StubResolver(), upstream_port=role.port) as proxy:
        holder["proxy"] = proxy
        c = open_tunnel(proxy)
        u = role.accept()
        c.close()
        u.close()
        assert wait_until(lambda: proxy.active_tunnels() == 0)
    assert seen == [1]


# --- idle ---


def test_idle_is_extended_by_upstream_to_client_traffic():
    with upstream_role() as role, running_proxy(
        StubResolver(), upstream_port=role.port, idle_timeout=0.8
    ) as proxy:
        c = open_tunnel(proxy)
        u = role.accept()
        c.settimeout(5)
        for _ in range(3):
            time.sleep(0.4)
            u.sendall(b"y")
            assert read_exact(c, 1) == b"y"
        assert proxy.active_tunnels() == 1
        assert read_to_eof(c) == b""
        assert read_to_eof(u) == b""
        c.close()
        assert wait_until(lambda: len(proxy.closed) == 1)
        assert proxy.closed[0].end is RelayEnd.IDLE


def test_default_deadlines_reach_relay_and_connector(monkeypatch):
    assert inspect.signature(rl.relay).parameters["idle_timeout"].default == 300.0
    params = inspect.signature(up.connect_fixed).parameters
    assert params["per_address_timeout"].default == 5.0
    assert params["total_timeout"].default == 15.0

    relay_kw: list[dict] = []
    real_relay = px.relay

    def recording_relay(tunnel, **kw):
        relay_kw.append(kw)
        return real_relay(tunnel, **kw)

    monkeypatch.setattr(px, "relay", recording_relay)
    connector_calls: list = []

    with upstream_role() as role:

        def connector(targets, **kw):
            connector_calls.append((list(targets), kw))
            return up.connect_fixed([(LO, role.port)], **kw)

        resolver = StubResolver(answers=("10.0.0.1", "10.0.0.2", "10.0.0.3"))
        with running_proxy(resolver, connector=connector) as proxy:
            c = open_tunnel(proxy)
            u = role.accept()
            c.close()
            u.close()
            assert wait_until(lambda: len(proxy.closed) == 1)
    assert relay_kw[0]["idle_timeout"] == 300.0
    (targets, kw), = connector_calls
    # 解決結果の全 address を順のまま、既定 port 443 と既定の期限で渡す
    assert targets == [("10.0.0.1", 443), ("10.0.0.2", 443), ("10.0.0.3", 443)]
    assert kw["per_address_timeout"] == 5.0
    assert kw["total_timeout"] == 15.0


def test_configured_deadlines_reach_connector_unswapped():
    seen: list[dict] = []

    def connector(targets, **kw):
        seen.append(kw)
        raise up.UpstreamError(502, "x")

    with running_proxy(
        StubResolver(), connector=connector, per_address_timeout=0.3, total_timeout=0.9
    ) as proxy:
        c = connect_client(proxy)
        with c:
            c.sendall(request())
            assert read_to_eof(c) == px.STATUS_LINES[502]
    assert seen[0]["per_address_timeout"] == 0.3
    assert seen[0]["total_timeout"] == 0.9


# --- upstream の address ---


def test_upstream_never_resolves_a_name():
    srv = socket.socket()
    srv.bind((LO, 0))
    srv.listen(8)
    try:
        with pytest.raises(up.UpstreamError) as ei:
            up.connect_fixed([("localhost", srv.getsockname()[1])], total_timeout=1)
        assert ei.value.status == 502
        assert _no_pending_accept(srv)
    finally:
        srv.close()


def _v6_listener() -> socket.socket:
    try:
        srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        srv.bind(("::1", 0))
    except OSError:
        pytest.skip("IPv6 loopback が使えない環境")
    srv.listen(8)
    return srv


def test_ipv6_literal_is_connected_as_ipv6():
    srv = _v6_listener()
    try:
        sock = up.connect_fixed([("::1", srv.getsockname()[1])], total_timeout=2)
        try:
            assert sock.family == socket.AF_INET6
            assert sock.getpeername()[:2] == ("::1", srv.getsockname()[1])
        finally:
            sock.close()
    finally:
        srv.close()


def test_scoped_ipv6_address_is_not_used():
    srv = _v6_listener()
    try:
        with pytest.raises(up.UpstreamError) as ei:
            up.connect_fixed([("::1%lo", srv.getsockname()[1])], total_timeout=1)
        assert ei.value.status == 502
        assert _no_pending_accept(srv)
    finally:
        srv.close()


def test_per_address_wait_is_cut_by_the_total_deadline():
    with blackhole() as b1, blackhole() as b2:
        t0 = time.monotonic()
        with pytest.raises(up.UpstreamError) as ei:
            up.connect_fixed([b1, b2], per_address_timeout=0.8, total_timeout=1.0)
        elapsed = time.monotonic() - t0
    assert ei.value.status == 504
    # 2 本目の待ちは 0.8 秒ではなく全体の残り 0.2 秒
    assert 0.95 <= elapsed < 1.4, elapsed


def test_addresses_left_after_total_deadline_are_not_tried():
    srv = socket.socket()
    srv.bind((LO, 0))
    srv.listen(8)
    try:
        with blackhole() as bh:
            with pytest.raises(up.UpstreamError) as ei:
                up.connect_fixed(
                    [bh, (LO, closed_port()), srv.getsockname()],
                    per_address_timeout=0.6,
                    total_timeout=0.4,
                )
        assert ei.value.status == 504
        assert _no_pending_accept(srv)
    finally:
        srv.close()


def test_upstream_exhaustion_is_not_a_relay_end():
    with running_proxy(StubResolver(), upstream_port=closed_port()) as proxy:
        c = connect_client(proxy)
        with c:
            c.sendall(request())
            assert read_to_eof(c) == px.STATUS_LINES[502]
        assert wait_until(lambda: len(proxy.closed) == 1)
        assert proxy.closed[0].end is None
        assert proxy.closed[0].transitions == (TunnelState.CONNECTING, TunnelState.CLOSED)


# --- relay ---


def test_partial_sends_are_completed_toward_a_slow_reader():
    # proxy から client への送信 buffer を小さくし、送信が一部ずつしか入らない状態を作る
    payload = os.urandom(1024 * 1024)
    a, b = socket.socketpair()
    x, y = socket.socketpair()
    a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    result: list = []
    t = rl.Tunnel(a)
    assert t.attach_upstream(x)
    worker = threading.Thread(target=lambda: result.append(rl.relay(t, idle_timeout=10)))
    sender = threading.Thread(target=lambda: (y.sendall(payload), y.shutdown(socket.SHUT_WR)))
    try:
        worker.start()
        sender.start()
        time.sleep(0.3)
        b.settimeout(10)
        got = read_to_eof(b)
        b.shutdown(socket.SHUT_WR)
        sender.join(10)
        worker.join(10)
        assert got == payload
        assert result == [RelayEnd.CLOSED]
    finally:
        for s in (a, b, x, y):
            s.close()


def test_unexpected_relay_error_is_fault():
    a, b = socket.socketpair()
    x, y = socket.socketpair()
    calls = [0]

    def failing_clock():
        calls[0] += 1
        if calls[0] >= 3:
            raise RuntimeError("clock broke")
        return time.monotonic()

    try:
        t = rl.Tunnel(a)
        assert t.attach_upstream(x)
        b.sendall(b"z")
        assert rl.relay(t, clock=failing_clock) is RelayEnd.FAULT
        assert t.end is RelayEnd.FAULT
    finally:
        for s in (a, b, x, y):
            s.close()


# --- 停止 ---


def test_stop_marks_open_tunnels_stopped():
    with upstream_role() as role, running_proxy(StubResolver(), upstream_port=role.port) as proxy:
        clients = [open_tunnel(proxy) for _ in range(3)]
        ups = [role.accept() for _ in range(3)]
        assert proxy.stop(time.monotonic() + 3.0)
        assert len(proxy.closed) == 3
        assert {t.end for t in proxy.closed} == {RelayEnd.STOPPED}
        for s in clients + ups:
            s.close()


def test_stop_twice_is_harmless():
    with running_proxy(StubResolver()) as proxy:
        assert proxy.stop(time.monotonic() + 3.0)
        assert proxy.stop(time.monotonic() + 3.0)


def test_resolution_finishing_after_stop_does_not_connect():
    gate = threading.Event()
    connector_calls: list = []

    class BlockingResolver(StubResolver):
        def resolve(self, host):
            self.calls.append(host)
            gate.wait(5)
            return ("10.0.0.1",)

        def close(self, deadline):
            self.closed_with = deadline
            gate.set()

    def connector(targets, **kw):
        connector_calls.append(targets)
        raise up.UpstreamError(502, "x")

    resolver = BlockingResolver()
    with running_proxy(resolver, connector=connector) as proxy:
        c = connect_client(proxy)
        with c:
            c.sendall(request())
            assert wait_until(lambda: resolver.calls == [HOST])
            assert proxy.stop(time.monotonic() + 3.0)
            assert read_to_eof(c) == b""
    assert connector_calls == []


def test_admission_finishing_after_stop_opens_no_tunnel():
    gate = threading.Event()
    entered = threading.Event()

    def slow_admit(sock):
        entered.set()
        gate.wait(5)
        time.sleep(0.3)
        return True

    class GateResolver(StubResolver):
        def close(self, deadline):
            super().close(deadline)
            gate.set()

    resolver = GateResolver()
    with running_proxy(resolver, admit=slow_admit) as proxy:
        c = connect_client(proxy)
        with c:
            c.sendall(request())
            assert entered.wait(5)
            assert proxy.stop(time.monotonic() + 3.0)
            # 判定の途中だった accept thread も join してから返る
            assert not [t for t in threading.enumerate() if t.name.startswith("egress-proxy")]
            assert read_to_eof(c) == b""
        assert resolver.calls == []
        assert proxy.closed == []


def test_stop_returns_false_at_deadline_and_keeps_stop_fd_open():
    release = threading.Event()

    def stuck_connector(targets, **kw):
        # 停止の合図を見ない接続側 (deadline 内に終わらない thread の代役)
        release.wait(10)
        raise up.UpstreamError(502, "x")

    lst = px.create_listener()
    proxy = px.ConnectProxy(lst, StubResolver(), admit=accept_all, connector=stuck_connector)
    proxy.start()
    timer = threading.Timer(3.0, release.set)
    timer.start()
    try:
        c = connect_client(proxy)
        c.sendall(request())
        assert wait_until(lambda: proxy.active_tunnels() == 1)
        time.sleep(0.2)
        t0 = time.monotonic()
        assert proxy.stop(t0 + 0.5) is False
        assert time.monotonic() - t0 < 0.9
        # 残った thread が待っている間、停止の fd は閉じない
        assert os.readlink(f"/proc/self/fd/{proxy._stop_fd}") == "anon_inode:[eventfd]"
    finally:
        release.set()
        timer.join()
        assert proxy.stop(time.monotonic() + 5.0)
        c.close()


# --- tunnel の想定外の失敗と観測側の失敗 ---


def test_unexpected_tunnel_error_is_fault_and_closes_client():
    def broken_connector(targets, **kw):
        raise RuntimeError("broken")

    with running_proxy(StubResolver(), connector=broken_connector) as proxy:
        c = connect_client(proxy)
        with c:
            c.sendall(request())
            assert read_to_eof(c) == b""
        assert wait_until(lambda: len(proxy.closed) == 1)
        assert proxy.closed[0].end is RelayEnd.FAULT
        assert proxy.failure is None


def test_observer_failure_does_not_escape_the_tunnel_thread(monkeypatch):
    escaped: list = []
    monkeypatch.setattr(threading, "excepthook", lambda args: escaped.append(args.exc_value))

    def bad_observer(tunnel):
        raise RuntimeError("observer broke")

    lst = px.create_listener()
    proxy = px.ConnectProxy(
        lst,
        StubResolver(error=px.ResolveError(502, "x")),
        admit=accept_all,
        on_tunnel_closed=bad_observer,
    )
    proxy.start()
    try:
        c = connect_client(proxy)
        with c:
            c.sendall(request())
            assert read_to_eof(c) == px.STATUS_LINES[502]
        assert wait_until(lambda: proxy.active_tunnels() == 0)
        time.sleep(0.2)
    finally:
        assert proxy.stop(time.monotonic() + 5.0)
    assert escaped == []


def test_upstream_connect_interrupted_by_stop_writes_nothing(monkeypatch):
    real_shutdown = rl.Tunnel.shutdown_sockets

    def late_shutdown(self):
        # 接続待ちの側が停止の合図を先に受け取る順序を作る
        time.sleep(0.3)
        real_shutdown(self)

    monkeypatch.setattr(rl.Tunnel, "shutdown_sockets", late_shutdown)
    with blackhole() as bh:

        def connector(targets, **kw):
            return up.connect_fixed([bh], **kw)

        resolver = StubResolver()
        with running_proxy(resolver, connector=connector) as proxy:
            c = connect_client(proxy)
            with c:
                c.sendall(request())
                assert wait_until(lambda: resolver.calls == [HOST])
                time.sleep(0.2)
                assert proxy.stop(time.monotonic() + 3.0)
                assert read_to_eof(c) == b""
