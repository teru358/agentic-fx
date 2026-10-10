"""tunnel の中継、half-close、fault、idle、preread の順序の契約。

client → proxy → upstream 役の実 socket (127.0.0.1 の一時 port) で観測する。
"""

from __future__ import annotations

import socket
import struct
import time

import pytest

from agentic_fx.egress import relay as rl
from agentic_fx.egress.relay import RelayEnd, TunnelState
from tests.egress.test_proxy import (
    StubResolver,
    open_tunnel,
    read_exact,
    read_to_eof,
    request,
    running_proxy,
    upstream_role,
    wait_until,
    connect_client,
)
from tests.egress.test_upstream import _leak_guard  # noqa: F401

FULL = (TunnelState.CONNECTING, TunnelState.OPEN, TunnelState.HALF_CLOSED, TunnelState.CLOSED)


def rst_close(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


def test_idle_default_is_pinned():
    assert rl.IDLE_TIMEOUT_SEC == 300.0


def test_client_half_close_propagates_and_reverse_flows():
    payload = bytes(range(256)) * 2000  # 送信 buffer を超える量
    with upstream_role() as role, running_proxy(StubResolver(), upstream_port=role.port) as proxy:
        c = open_tunnel(proxy)
        u = role.accept()
        c.sendall(b"ping")
        c.shutdown(socket.SHUT_WR)
        assert read_exact(u, 4) == b"ping"
        assert u.recv(1) == b""  # client の EOF が upstream 役に届く
        u.sendall(payload)
        u.close()
        c.settimeout(5)
        assert read_to_eof(c) == payload
        c.close()
        assert wait_until(lambda: len(proxy.closed) == 1)
        t = proxy.closed[0]
        assert t.transitions == FULL
        assert t.end is RelayEnd.CLOSED


def test_upstream_half_close_propagates():
    with upstream_role() as role, running_proxy(StubResolver(), upstream_port=role.port) as proxy:
        c = open_tunnel(proxy)
        u = role.accept()
        u.sendall(b"hello")
        u.shutdown(socket.SHUT_WR)
        c.settimeout(5)
        assert read_to_eof(c) == b"hello"
        # 逆方向は流れ続ける
        for i in range(3):
            c.sendall(b"more%d" % i)
            assert read_exact(u, 5) == b"more%d" % i
        c.shutdown(socket.SHUT_WR)
        assert u.recv(1) == b""
        c.close()
        assert wait_until(lambda: len(proxy.closed) == 1)
        assert proxy.closed[0].transitions == FULL
        assert proxy.closed[0].end is RelayEnd.CLOSED


def test_upstream_reset_closes_both_sides():
    with upstream_role() as role, running_proxy(StubResolver(), upstream_port=role.port) as proxy:
        c = open_tunnel(proxy)
        u = role.accept()
        assert proxy.active_tunnels() == 1
        rst_close(u)
        c.settimeout(5)
        assert read_to_eof(c) == b""  # EOF か ECONNRESET
        c.close()
        assert wait_until(lambda: proxy.active_tunnels() == 0)
        assert proxy.closed[0].end is RelayEnd.FAULT
        assert proxy.closed[0].transitions[-1] is TunnelState.CLOSED


def test_client_reset_closes_upstream():
    with upstream_role() as role, running_proxy(StubResolver(), upstream_port=role.port) as proxy:
        c = open_tunnel(proxy)
        u = role.accept()
        rst_close(c)
        assert read_to_eof(u) == b""
        assert wait_until(lambda: proxy.active_tunnels() == 0)
        assert proxy.closed[0].end is RelayEnd.FAULT


def test_idle_tunnel_is_closed():
    with upstream_role() as role, running_proxy(
        StubResolver(), upstream_port=role.port, idle_timeout=0.8
    ) as proxy:
        c = open_tunnel(proxy)
        u = role.accept()
        # 通信がある間は閉じない
        for _ in range(3):
            time.sleep(0.4)
            c.sendall(b"x")
            assert read_exact(u, 1) == b"x"
        t0 = time.monotonic()
        c.settimeout(5)
        assert read_to_eof(c) == b""
        elapsed = time.monotonic() - t0
        assert read_to_eof(u) == b""
        c.close()
        assert 0.7 <= elapsed < 1.5, elapsed
        assert wait_until(lambda: len(proxy.closed) == 1)
        assert proxy.closed[0].end is RelayEnd.IDLE


def test_preread_is_forwarded_before_relay():
    hello = b"\x16\x03\x01" + b"H" * 300
    with upstream_role() as role, running_proxy(StubResolver(), upstream_port=role.port) as proxy:
        c = connect_client(proxy)
        c.sendall(request() + hello)  # 1 回の send で要求と後続を送る
        u = role.accept()
        # upstream 役が最初に受ける bytes は preread で、後続の送信より前に届く
        assert read_exact(u, len(hello)) == hello
        assert c.recv(4096).startswith(b"HTTP/1.1 200 ")
        c.sendall(b"after")
        assert read_exact(u, 5) == b"after"
        c.close()


@pytest.mark.parametrize("n", [1, 7])
def test_bytes_are_relayed_in_order_both_ways(n):
    with upstream_role() as role, running_proxy(StubResolver(), upstream_port=role.port) as proxy:
        c = open_tunnel(proxy)
        u = role.accept()
        for i in range(n):
            msg = b"%d-" % i * 1000
            c.sendall(msg)
            assert read_exact(u, len(msg)) == msg
            u.sendall(msg[::-1])
            assert read_exact(c, len(msg)) == msg[::-1]
        c.close()
        assert read_to_eof(u) == b""
