"""固定 address 列への順次接続の契約。

接続先は 127.0.0.1 の一時 port だけ。止まる upstream は backlog 0 の listener の受付
queue を埋め、次の SYN を kernel に捨てさせて作る (blackhole)。
"""

from __future__ import annotations

import os
import select
import socket
import threading
import time
from contextlib import contextmanager

import pytest

from agentic_fx.egress import upstream as up

LO = "127.0.0.1"


def fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


@contextmanager
def no_leaks():
    """前後で thread 数と fd 数が戻ることを確かめる。"""
    threads, fds = threading.active_count(), fd_count()
    yield
    end = time.monotonic() + 3.0
    while time.monotonic() < end:
        if threading.active_count() <= threads and fd_count() <= fds:
            break
        time.sleep(0.02)
    assert threading.active_count() <= threads, "thread が残った"
    assert fd_count() <= fds, "fd が残った"


@pytest.fixture(autouse=True)
def _leak_guard():
    with no_leaks():
        yield


@contextmanager
def blackhole():
    """SYN を捨てる listener の (host, port)。受付 queue を埋めた後の connect は進まない。"""
    bl = socket.socket()
    bl.bind((LO, 0))
    bl.listen(0)
    fillers = []
    try:
        for _ in range(4):
            f = socket.socket()
            f.setblocking(False)
            f.connect_ex(bl.getsockname())
            fillers.append(f)
        probe = socket.socket()
        probe.setblocking(False)
        probe.connect_ex(bl.getsockname())
        fillers.append(probe)
        _, w, _ = select.select([], [probe], [], 0.5)
        full = not w or probe.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) != 0
        if not full:
            pytest.skip("受付 queue を埋められない環境")
        yield bl.getsockname()
    finally:
        for f in fillers:
            f.close()
        bl.close()


@contextmanager
def listener():
    srv = socket.socket()
    srv.bind((LO, 0))
    srv.listen(8)
    try:
        yield srv
    finally:
        srv.close()


def closed_port() -> int:
    s = socket.socket()
    s.bind((LO, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_default_timeouts_are_pinned():
    assert up.PER_ADDRESS_TIMEOUT_SEC == 5.0
    assert up.TOTAL_TIMEOUT_SEC == 15.0


def test_upstream_tries_addresses_in_order_with_per_address_timeout():
    with blackhole() as bh, listener() as srv:
        t0 = time.monotonic()
        sock = up.connect_fixed(
            [bh, srv.getsockname()], per_address_timeout=0.8, total_timeout=3.0
        )
        elapsed = time.monotonic() - t0
        try:
            assert sock.getpeername() == srv.getsockname()
            assert sock.gettimeout() is None  # blocking のまま渡す
            assert 0.75 <= elapsed < 1.5
        finally:
            sock.close()


def test_upstream_total_budget_15s():
    with blackhole() as b1, blackhole() as b2, blackhole() as b3, blackhole() as b4:
        t0 = time.monotonic()
        with pytest.raises(up.UpstreamError) as ei:
            up.connect_fixed([b1, b2, b3, b4], per_address_timeout=0.8, total_timeout=2.0)
        elapsed = time.monotonic() - t0
    assert ei.value.status == 504
    # 4 本 × 0.8 秒ではなく全体の 2 秒で打ち切る
    assert 1.95 <= elapsed < 2.6


def test_refused_address_is_502_and_next_is_tried():
    refused = (LO, closed_port())
    with listener() as srv:
        sock = up.connect_fixed([refused, srv.getsockname()], per_address_timeout=1, total_timeout=2)
        sock.close()
    with pytest.raises(up.UpstreamError) as ei:
        up.connect_fixed([refused], per_address_timeout=1, total_timeout=2)
    assert ei.value.status == 502


def test_mixed_timeout_and_refusal_is_502():
    with blackhole() as bh:
        with pytest.raises(up.UpstreamError) as ei:
            up.connect_fixed([bh, (LO, closed_port())], per_address_timeout=0.3, total_timeout=2)
    assert ei.value.status == 502


def test_non_literal_address_is_never_resolved(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("名前解決が呼ばれた")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(up.UpstreamError) as ei:
        up.connect_fixed([("localhost", 443), ("127.0.0.1%lo", 443)], total_timeout=1)
    assert ei.value.status == 502


def test_stop_fd_interrupts_connect():
    efd = os.eventfd(0, os.EFD_CLOEXEC | os.EFD_NONBLOCK)
    try:
        with blackhole() as bh:
            timer = threading.Timer(0.3, os.eventfd_write, (efd, 1))
            timer.start()
            t0 = time.monotonic()
            with pytest.raises(up.UpstreamStopped):
                up.connect_fixed([bh], per_address_timeout=5, total_timeout=5, stop_fd=efd)
            assert time.monotonic() - t0 < 1.0
            timer.join()
    finally:
        os.close(efd)


def test_connection_that_completes_after_the_total_deadline_is_504(monkeypatch):
    """全体期限の後に完了した接続は成功扱いにしない (実 socket、時計だけ進める)。"""
    offset = [0.0]
    polled = []
    real_poll = select.poll

    class ShiftedClock:
        def monotonic(self):
            return time.monotonic() + offset[0]

    class ShiftedPoll:
        def __init__(self):
            self._p = real_poll()

        def register(self, *a):
            return self._p.register(*a)

        def poll(self, *a):
            events = self._p.poll(*a)
            if events:
                polled.append(True)
                offset[0] += 100.0  # 接続が返った時点で期限を過ぎている状況
            return events

    class ShiftedSelect:
        poll = ShiftedPoll
        POLLOUT = select.POLLOUT
        POLLIN = select.POLLIN

    srv = socket.socket()
    srv.bind((LO, 0))
    srv.listen(8)
    monkeypatch.setattr(up, "time", ShiftedClock())
    monkeypatch.setattr(up, "select", ShiftedSelect)
    try:
        with pytest.raises(up.UpstreamError) as ei:
            up.connect_fixed([srv.getsockname()], total_timeout=5.0)
        assert polled, "connect が poll を通る経路でなければ検証にならない"
        assert ei.value.status == 504
    finally:
        srv.close()
