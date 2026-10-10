"""CONNECT proxy の listener、accept、上限、解決から upstream まで、停止の契約。

socket はすべて 127.0.0.1 の一時 port の実物。製品の resolver は loopback を不適格として
拒否するので、upstream 役へ繋ぐ試験では「解決結果を返す相手」を代役に差し替える。
"""

from __future__ import annotations

import errno
import os
import queue
import re
import select
import socket
import ssl
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from agentic_fx.egress import proxy as px
from agentic_fx.egress import upstream as up
from agentic_fx.egress.relay import TunnelState
from agentic_fx.egress.resolver import MissionResolver, ResolveError
from tests.egress.test_upstream import _leak_guard, blackhole  # noqa: F401

LO = "127.0.0.1"
HOST = "api.anthropic.com"


def request(host: str = HOST) -> bytes:
    return f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode()


def accept_all(sock: socket.socket) -> bool:
    return True


class StubResolver:
    """固定の答え (または失敗) を返す代役。呼ばれた host を記録する。"""

    def __init__(self, answers=(LO,), error: ResolveError | None = None, by_host=None):
        self.answers = tuple(answers)
        self.error = error
        self.by_host = by_host or {}
        self.calls: list[str] = []
        self.closed_with: float | None = None

    def resolve(self, host: str) -> tuple[str, ...]:
        self.calls.append(host)
        if self.error is not None:
            raise self.error
        return self.by_host.get(host, self.answers)

    def close(self, deadline: float) -> None:
        self.closed_with = deadline


class UpstreamRole:
    """upstream 役の listener。accept した socket を queue に積む。"""

    def __init__(self, backlog: int = 64) -> None:
        self.srv = socket.socket()
        self.srv.bind((LO, 0))
        self.srv.listen(backlog)
        self.port = self.srv.getsockname()[1]
        self.conns: queue.Queue[socket.socket] = queue.Queue()
        self.accepted = 0
        self._all: list[socket.socket] = []
        self._efd = os.eventfd(0, os.EFD_CLOEXEC | os.EFD_NONBLOCK)
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def _loop(self) -> None:
        p = select.poll()
        p.register(self.srv.fileno(), select.POLLIN)
        p.register(self._efd, select.POLLIN)
        while True:
            ev = dict(p.poll())
            if self._efd in ev:
                return
            c, _ = self.srv.accept()
            self.accepted += 1
            self._all.append(c)
            self.conns.put(c)

    def accept(self, timeout: float = 5.0) -> socket.socket:
        c = self.conns.get(timeout=timeout)
        c.settimeout(5.0)
        return c

    def close(self) -> None:
        os.eventfd_write(self._efd, 1)
        self._th.join(5)
        for c in self._all:
            c.close()
        self.srv.close()
        os.close(self._efd)


@contextmanager
def upstream_role():
    role = UpstreamRole()
    try:
        yield role
    finally:
        role.close()


@contextmanager
def running_proxy(resolver, *, admit=accept_all, **kw):
    closed: list = []
    lst = px.create_listener()
    proxy = px.ConnectProxy(
        lst, resolver, admit=admit, on_tunnel_closed=closed.append, **kw
    )
    proxy.closed = closed
    proxy.start()
    try:
        yield proxy
    finally:
        assert proxy.stop(time.monotonic() + 5.0)


def connect_client(proxy, timeout: float = 5.0) -> socket.socket:
    c = socket.create_connection((LO, proxy.port), timeout=timeout)
    return c


def read_exact(sock: socket.socket, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            break
        out += chunk
    return out


def read_to_eof(sock: socket.socket) -> bytes:
    out = b""
    while True:
        try:
            chunk = sock.recv(65536)
        except ConnectionResetError:
            break
        if not chunk:
            break
        out += chunk
    return out


def open_tunnel(proxy, host: str = HOST, extra: bytes = b"") -> socket.socket:
    c = connect_client(proxy)
    c.sendall(request(host) + extra)
    got = read_exact(c, len(px.SUCCESS_RESPONSE))
    assert got == px.SUCCESS_RESPONSE
    return c


def wait_until(pred, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def client_hello(server_name: str) -> bytes:
    """実 TLS stack が作る ClientHello の bytes (外へは送らない)。"""
    ctx = ssl.create_default_context()
    inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = ctx.wrap_bio(inc, out, server_hostname=server_name)
    with pytest.raises(ssl.SSLWantReadError):
        obj.do_handshake()
    return out.read()


# --- listener ---


def test_proxy_listener_is_loopback_ephemeral_backlog128():
    assert px.LISTEN_BACKLOG == 128
    lst = px.create_listener()
    try:
        host, port = lst.getsockname()
        assert host == LO and port != 0
        assert lst.family == socket.AF_INET
        assert not os.get_inheritable(lst.fileno())
        hexport = f"{port:04X}"
        rows = [
            line.split()
            for line in Path("/proc/net/tcp").read_text().splitlines()[1:]
            if line.split()[1] == f"0100007F:{hexport}"
        ]
        assert rows and rows[0][3] == "0A"  # LISTEN
        try:
            out = subprocess.run(
                ["ss", "-ltnH", f"sport = :{port}"], capture_output=True, text=True, timeout=5
            ).stdout
        except FileNotFoundError:
            return
        # LISTEN の Send-Q は backlog の指定値
        assert out.split()[2] == "128", out
    finally:
        lst.close()


def test_proxy_start_failed_when_socket_cannot_be_made():
    code = (
        "import os, resource\n"
        "from agentic_fx.egress import proxy as px\n"
        "n = len(os.listdir('/proc/self/fd'))\n"
        "resource.setrlimit(resource.RLIMIT_NOFILE, (n - 1, n - 1))\n"
        "try:\n"
        "    px.create_listener()\n"
        "except px.ProxyStartFailed as e:\n"
        "    print(e.reason)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert r.stdout.strip() == "proxy_start_failed", r.stderr


def test_proxy_url_and_constants():
    assert px.MAX_TUNNELS == 32
    assert px.SUCCESS_RESPONSE == b"HTTP/1.1 200 Connection established\r\n\r\n"
    with running_proxy(StubResolver()) as proxy:
        assert proxy.url == f"http://127.0.0.1:{proxy.port}"


# --- accept と判定の差し込み口 ---


def test_default_admit_rejects_everything():
    resolver = StubResolver()
    lst = px.create_listener()
    proxy = px.ConnectProxy(lst, resolver)
    proxy.start()
    try:
        c = connect_client(proxy)
        with c:
            c.sendall(request())
            assert read_to_eof(c) == b""
        assert resolver.calls == []
    finally:
        assert proxy.stop(time.monotonic() + 5)


def test_admit_exception_is_rejection_not_death():
    def boom(sock):
        raise RuntimeError("x")

    resolver = StubResolver()
    with running_proxy(resolver, admit=boom) as proxy:
        c = connect_client(proxy)
        with c:
            c.sendall(request())
            assert read_to_eof(c) == b""
        assert resolver.calls == []
        assert proxy.failure is None


def test_accept_time_is_the_parser_deadline_origin():
    released = threading.Event()
    seen_at: list[float] = []

    def slow_admit(sock):
        seen_at.append(time.monotonic())
        time.sleep(4.0)
        released.set()
        return True

    resolver = StubResolver()
    with upstream_role() as role, running_proxy(
        resolver, admit=slow_admit, upstream_port=role.port
    ) as proxy:
        c = connect_client(proxy, timeout=10)
        t0 = time.monotonic()

        def late_send():
            released.wait(10)
            time.sleep(1.5)
            try:
                c.sendall(request())
            except OSError:
                pass

        sender = threading.Thread(target=late_send)
        sender.start()
        with c:
            data = read_to_eof(c)
            elapsed = time.monotonic() - t0
        sender.join()
        assert data == b""
        # 差し込み口を出た時点 (4 秒) から 5 秒ではなく、accept から 5 秒で閉じる
        assert 4.8 <= elapsed < 5.45, elapsed
        assert resolver.calls == []
        assert role.accepted == 0


# --- tunnel 上限 ---


def test_tunnel_limit_returns_503_after_accept():
    resolver = StubResolver()
    with upstream_role() as role, running_proxy(resolver, upstream_port=role.port) as proxy:
        pairs = []
        try:
            for _ in range(px.MAX_TUNNELS):
                pairs.append((open_tunnel(proxy), role.accept()))
            assert proxy.active_tunnels() == 32
            extra = connect_client(proxy)
            with extra:
                # 要求を送らないまま 503 が来て閉じられる
                assert read_to_eof(extra) == px.STATUS_LINES[503]
            assert len(resolver.calls) == 32
            # 両側が閉じて CLOSED になった時点で 1 本空く (half-close の間は数えたまま)
            c, u = pairs.pop()
            c.close()
            assert read_to_eof(u) == b""
            time.sleep(0.2)
            assert proxy.active_tunnels() == 32
            u.close()
            assert wait_until(lambda: proxy.active_tunnels() == 31)
            pairs.append((open_tunnel(proxy), role.accept()))
            assert proxy.active_tunnels() == 32
        finally:
            for c, u in pairs:
                c.close()
                u.close()


def test_tunnel_limit_counts_connections_awaiting_request():
    resolver = StubResolver()
    with running_proxy(resolver) as proxy:
        idle = [connect_client(proxy) for _ in range(px.MAX_TUNNELS)]
        try:
            assert wait_until(lambda: proxy.active_tunnels() == 32)
            extra = connect_client(proxy)
            with extra:
                assert read_to_eof(extra) == px.STATUS_LINES[503]
        finally:
            for c in idle:
                c.close()
        assert wait_until(lambda: proxy.active_tunnels() == 0)


# --- 解決と upstream ---


@pytest.mark.parametrize("status", [403, 502, 503, 504])
def test_resolver_errors_map_to_status(status):
    expected = {
        403: b"",
        502: b"HTTP/1.1 502 Bad Gateway\r\n\r\n",
        503: b"HTTP/1.1 503 Service Unavailable\r\n\r\n",
        504: b"HTTP/1.1 504 Gateway Timeout\r\n\r\n",
    }[status]
    resolver = StubResolver(error=ResolveError(status, "x"))
    with upstream_role() as role, running_proxy(resolver, upstream_port=role.port) as proxy:
        c = connect_client(proxy)
        with c:
            c.sendall(request())
            assert read_to_eof(c) == expected
        assert resolver.calls == [HOST]
        assert role.accepted == 0
        assert wait_until(lambda: proxy.active_tunnels() == 0)
        assert [t.transitions for t in proxy.closed] == [
            (TunnelState.CONNECTING, TunnelState.CLOSED)
        ]


def test_disallowed_host_never_reaches_resolver():
    calls: list = []

    def fake_getaddrinfo(*a, **k):
        calls.append(a)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]

    connected: list = []

    def connector(targets, **kw):
        connected.append(targets)
        raise AssertionError("upstream へ繋ごうとした")

    resolver = MissionResolver("claude", getaddrinfo=fake_getaddrinfo)
    with upstream_role() as role, running_proxy(resolver, connector=connector) as proxy:
        c = connect_client(proxy)
        with c:
            c.sendall(request("example.com"))
            assert read_to_eof(c) == b""
        assert calls == []
        assert connected == []
        assert role.accepted == 0


def test_connect_uses_fixed_addresses_only(monkeypatch):
    answers = iter(["1.1.1.1", "9.9.9.9", "8.8.8.8"])
    dns_calls: list = []

    def changing_getaddrinfo(host, port, *a, **k):
        dns_calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))]

    # proxy の経路で名前を受ける API が呼ばれたら落ちるようにする
    def tripwire(*a, **k):
        raise AssertionError("名前解決の API が呼ばれた")

    for name in ("getaddrinfo", "create_connection", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, tripwire)

    targets_seen: list = []

    with upstream_role() as role:

        def connector(targets, **kw):
            targets_seen.append(list(targets))
            # 外へは出さず、固定された address を upstream 役の port に読み替えて実接続する
            return up.connect_fixed([(LO, role.port)], **kw)

        resolver = MissionResolver("claude", getaddrinfo=changing_getaddrinfo)
        with running_proxy(resolver, connector=connector) as proxy:
            c1 = _open_tunnel_raw(proxy)
            c2 = _open_tunnel_raw(proxy)
            c1.close()
            c2.close()
    assert dns_calls == [HOST]
    assert targets_seen == [[("1.1.1.1", 443)], [("1.1.1.1", 443)]]


def _open_tunnel_raw(proxy) -> socket.socket:
    # socket.create_connection を tripwire にした試験用の接続
    c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    c.settimeout(5)
    c.connect((LO, proxy.port))
    c.sendall(request())
    assert read_exact(c, len(px.SUCCESS_RESPONSE)) == px.SUCCESS_RESPONSE
    return c


def test_proxy_modules_do_not_use_name_based_connect_apis():
    root = Path(px.__file__).parent
    pattern = re.compile(r"create_connection|getaddrinfo|gethostbyname|getfqdn")
    for name in ("proxy.py", "upstream.py", "relay.py"):
        text = (root / name).read_text()
        assert not pattern.search(text), name


def test_upstream_exhaustion_maps_to_status():
    # 全期限切れは 504
    with ExitStack() as st:
        bh = st.enter_context(blackhole())
        resolver = StubResolver()
        proxy = st.enter_context(
            running_proxy(
                resolver, upstream_port=bh[1], per_address_timeout=0.3, total_timeout=0.6
            )
        )
        c = connect_client(proxy)
        with c:
            c.sendall(request())
            assert read_to_eof(c) == px.STATUS_LINES[504]
    # 接続拒否を含む全滅は 502
    s = socket.socket()
    s.bind((LO, 0))
    refused = s.getsockname()[1]
    s.close()
    with running_proxy(StubResolver(), upstream_port=refused) as proxy:
        c = connect_client(proxy)
        with c:
            c.sendall(request())
            assert read_to_eof(c) == px.STATUS_LINES[502]


# --- TLS の中身を見ないこと ---


def test_sni_and_host_are_not_inspected():
    hello = client_hello("unrelated-tenant.example.net")
    assert b"unrelated-tenant.example.net" in hello
    with upstream_role() as role, running_proxy(StubResolver(), upstream_port=role.port) as proxy:
        c = open_tunnel(proxy)
        with c:
            u = role.accept()
            c.sendall(hello)
            assert read_exact(u, len(hello)) == hello
            # 中の HTTP Host も見ない
            req = b"GET / HTTP/1.1\r\nHost: unrelated-tenant.example.net\r\n\r\n"
            c.sendall(req)
            assert read_exact(u, len(req)) == req


# --- 停止 ---


def test_proxy_stop_within_deadline():
    with upstream_role() as role, blackhole() as bh:
        port_of = {"10.0.0.1": role.port, "10.0.0.2": bh[1]}

        def connector(targets, **kw):
            return up.connect_fixed([(LO, port_of[a]) for a, _ in targets], **kw)

        resolver = StubResolver(
            by_host={HOST: ("10.0.0.1",), "stuck.example.com": ("10.0.0.2",)}
        )
        lst = px.create_listener()
        proxy = px.ConnectProxy(lst, resolver, admit=accept_all, connector=connector)
        proxy.start()
        clients = [open_tunnel(proxy) for _ in range(10)]
        stuck = connect_client(proxy)
        stuck.sendall(request("stuck.example.com"))
        waiting = connect_client(proxy)  # 要求待ちの接続
        assert wait_until(lambda: "stuck.example.com" in resolver.calls)
        time.sleep(0.2)
        assert proxy.active_tunnels() == 12
        deadline = time.monotonic() + 3.0
        assert proxy.stop(deadline)
        assert time.monotonic() < deadline
        assert resolver.closed_with == deadline
        assert proxy.active_tunnels() == 0
        for c in clients + [stuck, waiting]:
            c.settimeout(2)
            assert read_to_eof(c) == b""
            c.close()
        with socket.socket() as probe, pytest.raises(OSError):
            probe.connect((LO, proxy.port))
        assert proxy.failure is None
        assert not [t for t in threading.enumerate() if t.name.startswith("egress-proxy")]


def test_accept_thread_death_is_proxy_died():
    class BrokenListener(socket.socket):
        def accept(self):
            raise RuntimeError("accept broke")

    lst = BrokenListener(socket.AF_INET, socket.SOCK_STREAM)
    lst.bind((LO, 0))
    lst.listen(px.LISTEN_BACKLOG)
    proxy = px.ConnectProxy(lst, StubResolver(), admit=accept_all)
    proxy.start()
    try:
        c = socket.socket()
        c.connect((LO, proxy.port))
        assert wait_until(lambda: proxy.failure == px.PROXY_DIED)
        c.close()
    finally:
        assert proxy.stop(time.monotonic() + 5)


class _FlakyListener(socket.socket):
    """accept が先頭 fail_times 回だけ errno 付きの OSError で失敗し、以降は実物に委ねる。"""

    def __init__(self, err: int, fail_times: int | None):
        super().__init__(socket.AF_INET, socket.SOCK_STREAM)
        self.err = err
        self.fail_times = fail_times
        self.failures = 0

    def accept(self):
        if self.fail_times is None or self.failures < self.fail_times:
            self.failures += 1
            raise OSError(self.err, os.strerror(self.err))
        return super().accept()


def _flaky_proxy(err: int, fail_times: int | None, **kw):
    lst = _FlakyListener(err, fail_times)
    lst.bind((LO, 0))
    lst.listen(px.LISTEN_BACKLOG)
    return lst, px.ConnectProxy(lst, StubResolver(), admit=accept_all, **kw)


@pytest.mark.parametrize(
    "err",
    [errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM],
)
def test_transient_accept_errors_are_retried(monkeypatch, err):
    monkeypatch.setattr(px, "ACCEPT_BACKOFF_INITIAL_SEC", 0.01)
    monkeypatch.setattr(px, "ACCEPT_BACKOFF_MAX_SEC", 0.05)
    lst, proxy = _flaky_proxy(err, 3)
    proxy.start()
    try:
        c = socket.socket()
        c.settimeout(5)
        c.connect((LO, proxy.port))
        c.sendall(request())
        assert wait_until(lambda: lst.failures >= 3)
        assert wait_until(lambda: proxy._resolver.calls == [HOST], timeout=5)
        assert proxy.failure is None
        c.close()
    finally:
        assert proxy.stop(time.monotonic() + 5)


def test_persistent_accept_error_dies_after_the_limit(monkeypatch):
    monkeypatch.setattr(px, "ACCEPT_BACKOFF_INITIAL_SEC", 0.01)
    monkeypatch.setattr(px, "ACCEPT_BACKOFF_MAX_SEC", 0.02)
    monkeypatch.setattr(px, "ACCEPT_FAILURE_LIMIT_SEC", 0.3)
    lst, proxy = _flaky_proxy(errno.EMFILE, None)
    proxy.start()
    try:
        c = socket.socket()
        c.connect((LO, proxy.port))
        started = time.monotonic()
        assert wait_until(lambda: proxy.failure == px.PROXY_DIED, timeout=5)
        assert time.monotonic() - started >= 0.25
        assert lst.failures >= 3
        c.close()
    finally:
        assert proxy.stop(time.monotonic() + 5)


def test_other_accept_errno_dies_immediately(monkeypatch):
    monkeypatch.setattr(px, "ACCEPT_FAILURE_LIMIT_SEC", 30.0)
    lst, proxy = _flaky_proxy(errno.EBADF, None)
    proxy.start()
    try:
        c = socket.socket()
        c.connect((LO, proxy.port))
        assert wait_until(lambda: proxy.failure == px.PROXY_DIED, timeout=2)
        assert lst.failures == 1
        c.close()
    finally:
        assert proxy.stop(time.monotonic() + 5)


def test_stop_cuts_the_accept_backoff(monkeypatch):
    monkeypatch.setattr(px, "ACCEPT_BACKOFF_INITIAL_SEC", 5.0)
    monkeypatch.setattr(px, "ACCEPT_BACKOFF_MAX_SEC", 5.0)
    monkeypatch.setattr(px, "ACCEPT_FAILURE_LIMIT_SEC", 60.0)
    lst, proxy = _flaky_proxy(errno.EMFILE, None)
    proxy.start()
    c = socket.socket()
    c.connect((LO, proxy.port))
    assert wait_until(lambda: lst.failures >= 1)
    t0 = time.monotonic()
    assert proxy.stop(time.monotonic() + 3)
    assert time.monotonic() - t0 < 2.0
    assert proxy.failure is None
    c.close()
