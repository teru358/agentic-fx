"""status 行だけを返して閉じる失敗は、未読データが残っていても client が読み切れる。

未読の受信 data を抱えたまま close すると kernel が RST を送り、client の受信 buffer にある
status 行が捨てられる。実 socket の client で確かめる (kernel の応答はモックしない)。
"""

from __future__ import annotations

import socket
import time

from agentic_fx.egress import proxy as px
from agentic_fx.egress.resolver import ResolveError
from tests.egress.test_proxy import (
    HOST,
    StubResolver,
    connect_client,
    request,
    running_proxy,
    wait_until,
)


def _read_status_then_eof(sock: socket.socket) -> bytes:
    """EOF まで読む。RST で切られたら ConnectionResetError のまま上げる (握りつぶさない)。"""
    out = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            return out
        out += chunk


def _slow_admit(sock: socket.socket) -> bool:
    # 判定の間に client の要求が届いて未読のまま残る状況を作る
    time.sleep(0.05)
    return True


def test_limit_503_is_readable_by_every_client_that_sent_a_request():
    resolver = StubResolver()
    with running_proxy(resolver, admit=_slow_admit) as proxy:
        idle = [connect_client(proxy) for _ in range(px.MAX_TUNNELS)]
        try:
            assert wait_until(lambda: proxy.active_tunnels() == px.MAX_TUNNELS)
            got = []
            for _ in range(20):
                with connect_client(proxy, timeout=5.0) as c:
                    c.sendall(request())
                    got.append(_read_status_then_eof(c))
            assert got == [px.STATUS_LINES[503]] * 20
        finally:
            for c in idle:
                c.close()
        assert wait_until(lambda: proxy.active_tunnels() == 0)


def test_limit_503_lingering_is_bounded_for_a_client_that_keeps_sending():
    resolver = StubResolver()
    with running_proxy(resolver) as proxy:
        idle = [connect_client(proxy) for _ in range(px.MAX_TUNNELS)]
        try:
            assert wait_until(lambda: proxy.active_tunnels() == px.MAX_TUNNELS)
            c = connect_client(proxy, timeout=5.0)
            with c:
                start = time.monotonic()
                c.sendall(request())
                closed_at = None
                while time.monotonic() - start < 4.0:
                    try:
                        c.sendall(b"x" * 512)
                        c.settimeout(0.05)
                        data = c.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        closed_at = time.monotonic() - start
                        break
                    if data == b"":
                        # FIN は送り終えた直後に届く。close の確認は書き込みの失敗で行う
                        continue
                assert closed_at is not None
                assert px.LINGER_TIMEOUT_SEC <= closed_at < px.LINGER_TIMEOUT_SEC + 0.8
        finally:
            for c in idle:
                c.close()


def test_resolver_status_survives_unread_data_and_counts_tunnel_while_lingering():
    resolver = StubResolver(error=ResolveError(502, "x"))
    with running_proxy(resolver) as proxy:
        c = connect_client(proxy, timeout=5.0)
        with c:
            c.sendall(request(HOST) + b"y" * 4096)
            assert _read_status_then_eof(c) == px.STATUS_LINES[502]
            # 読み捨てて待っている間も 1 本と数える
            assert proxy.active_tunnels() == 1
            time.sleep(px.LINGER_TIMEOUT_SEC * 0.4)
            assert proxy.active_tunnels() == 1
        # client が閉じれば期限を待たずに数から外れる
        assert wait_until(lambda: proxy.active_tunnels() == 0, timeout=0.5)


def test_lingering_tunnel_is_released_at_the_deadline_when_client_stays_silent():
    resolver = StubResolver(error=ResolveError(504, "x"))
    with running_proxy(resolver) as proxy:
        c = connect_client(proxy, timeout=5.0)
        with c:
            c.sendall(request(HOST))
            start = time.monotonic()
            assert _read_status_then_eof(c) == px.STATUS_LINES[504]
            assert wait_until(lambda: proxy.active_tunnels() == 0, timeout=3.0)
            elapsed = time.monotonic() - start
            assert px.LINGER_TIMEOUT_SEC * 0.8 <= elapsed < px.LINGER_TIMEOUT_SEC + 0.8
