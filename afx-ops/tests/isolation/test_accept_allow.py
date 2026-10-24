"""accept 直後の peer allow と、1 回の readiness あたりの accept 試行数 (実 socket / 実 loop)。"""
from __future__ import annotations

import asyncio
import errno
import ipaddress
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from afx_ops.isolation import accept_loop
from afx_ops.isolation.accept_loop import (
    ACCEPT_BUDGET,
    AcceptLoop,
    canonical_peer,
    prebind_listener,
)
from afx_ops.web import runtime

NET = ipaddress.IPv4Network
ONLY_127_1 = (NET("127.0.0.1/32"),)


# --- 部品 -------------------------------------------------------------------------

class SpyConn:
    """accepted socket の代わり。呼ばれた操作を記録する (読取りも送信もしないはず)。"""

    def __init__(self, peer=("127.0.0.1", 40000), error: Exception | None = None) -> None:
        self.peer, self.error, self.calls = peer, error, []

    def getpeername(self):
        self.calls.append("getpeername")
        if self.error is not None:
            raise self.error
        return self.peer

    def close(self):
        self.calls.append("close")

    def __getattr__(self, name):
        def forbidden(*args, **kwargs):
            self.calls.append(name)
        return forbidden


class Scripted:
    """accept の結果を順に返す listener の代わり。accept の試行回数を数える。"""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.attempts = 0
        self.closed = 0

    def setblocking(self, flag):
        pass

    def fileno(self):
        return -1

    def accept(self):
        self.attempts += 1
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step, ("0.0.0.0", 0)

    def close(self):
        self.closed += 1


def _errno(code: int) -> OSError:
    return OSError(code, os.strerror(code))


def _taking(listener, **kwargs):
    taken = []
    return AcceptLoop(listener, lambda conn, peer: taken.append((conn, peer)), **kwargs), taken


def _lines(capsys) -> list[str]:
    return [line for line in capsys.readouterr().err.splitlines() if line]


# --- AC-5: allow の判定は accept 直後、応答も読取りもなし ------------------------------

@pytest.mark.parametrize("peer", [
    ("10.1.2.3", 1), ("192.168.1.1", 1), ("127.0.0.2", 1), ("128.0.0.1", 1),
])
def test_a_peer_outside_allow_is_closed_without_a_read_or_a_reply(peer, capsys):
    conn = SpyConn(peer=peer)
    loop, taken = _taking(Scripted([conn, BlockingIOError()]), allow=ONLY_127_1)
    assert loop.accept_ready() == 1
    assert taken == []
    assert conn.calls == ["getpeername", "close"]
    assert loop.web_peer_rejected_total == 1 and loop.web_peer_lookup_failed_total == 0
    assert _lines(capsys) == ["afx-ops: web_peer_rejected"]


@pytest.mark.parametrize("conn", [
    SpyConn(error=OSError(errno.ENOTCONN, "gone")),
    SpyConn(error=OSError(errno.EBADF, "bad")),
    SpyConn(peer=("::1", 1, 0, 0)),
    SpyConn(peer="/run/x.sock"),
    SpyConn(peer=None),
    SpyConn(peer=("010.0.0.1", 1)),
    SpyConn(peer=("not-an-ip", 1)),
    SpyConn(peer=(b"127.0.0.1", 1)),
    SpyConn(peer=("127.0.0.1",)),
    SpyConn(peer=["127.0.0.1", 1]),
])
def test_a_failed_or_unexpected_peer_lookup_is_closed_silently(conn, capsys):
    loop, taken = _taking(Scripted([conn, BlockingIOError()]))
    assert loop.accept_ready() == 1
    assert taken == []
    assert conn.calls == ["getpeername", "close"]
    assert loop.web_peer_lookup_failed_total == 1 and loop.web_peer_rejected_total == 0
    assert _lines(capsys) == ["afx-ops: web_peer_lookup_failed"]


def test_an_allowed_peer_reaches_the_callback_with_its_canonical_ipv4():
    conn = SpyConn(peer=("127.0.0.1", 5))
    loop, taken = _taking(Scripted([conn, BlockingIOError()]), allow=ONLY_127_1)
    loop.accept_ready()
    assert taken == [(conn, "127.0.0.1")]
    assert conn.calls == ["getpeername"]


@pytest.mark.parametrize(("allow", "peer", "ok"), [
    (("192.168.1.0/24",), "192.168.1.0", True), (("192.168.1.0/24",), "192.168.1.255", True),
    (("192.168.1.0/24",), "192.168.2.0", False), (("192.168.1.0/24",), "192.168.0.255", False),
    (("10.0.0.5/32",), "10.0.0.5", True), (("10.0.0.5/32",), "10.0.0.6", False),
    (("10.0.0.5/32", "192.168.1.0/24"), "192.168.1.9", True),
])
def test_allow_networks_are_matched_on_the_peer_address(allow, peer, ok):
    conn = SpyConn(peer=(peer, 1))
    loop, taken = _taking(Scripted([conn, BlockingIOError()]),
                          allow=tuple(NET(item) for item in allow))
    loop.accept_ready()
    assert bool(taken) is ok


def test_the_default_allow_is_the_loopback_range():
    ok, out = SpyConn(peer=("127.200.1.1", 1)), SpyConn(peer=("10.0.0.1", 1))
    loop, taken = _taking(Scripted([ok, out, BlockingIOError()]))
    loop.accept_ready()
    assert [item[1] for item in taken] == ["127.200.1.1"]


def test_canonical_peer_only_accepts_an_ipv4_pair():
    assert canonical_peer(("1.2.3.4", 80)) == "1.2.3.4"
    for bad in (None, "x", ("1.2.3.4",), ("1.2.3.4", "80"), ("::1", 1, 0, 0), ("01.2.3.4", 1)):
        assert canonical_peer(bad) is None


def test_a_rejected_callback_error_does_not_count_toward_fd_exhaustion(capsys):
    taken = []

    def explode(conn, peer):
        raise RuntimeError("boom")
    loop = AcceptLoop(Scripted([SpyConn(), SpyConn(), SpyConn(), BlockingIOError()]), explode,
                      exhausted_limit=2, on_exhausted=lambda: taken.append("exhausted"))
    loop.accept_ready()
    assert taken == [] and loop.accept_callback_failed_total == 3


# --- journal の固定語と sampling ----------------------------------------------------

def test_the_journal_never_carries_errors_addresses_or_exception_text(capsys):
    def explode(conn, peer):
        raise ValueError("secret-detail 10.9.8.7")
    loop = AcceptLoop(Scripted([SpyConn(peer=("10.9.8.7", 1234)), SpyConn(), BlockingIOError()]),
                      explode, allow=ONLY_127_1)
    loop.accept_ready()
    assert _lines(capsys) == ["afx-ops: web_peer_rejected", "afx-ops: accept_callback_failed"]


@pytest.mark.parametrize("kind", ["rejected", "lookup", "callback"])
def test_each_security_or_callback_journal_line_is_limited_to_ten_but_always_counted(kind, capsys):
    if kind == "rejected":
        conns = [SpyConn(peer=("10.0.0.1", 1)) for _ in range(13)]
        loop, _ = _taking(Scripted([*conns, BlockingIOError()]), allow=ONLY_127_1)
        counter = lambda: loop.web_peer_rejected_total  # noqa: E731
        line = "afx-ops: web_peer_rejected"
    elif kind == "lookup":
        conns = [SpyConn(error=OSError(errno.ENOTCONN, "x")) for _ in range(13)]
        loop, _ = _taking(Scripted([*conns, BlockingIOError()]))
        counter = lambda: loop.web_peer_lookup_failed_total  # noqa: E731
        line = "afx-ops: web_peer_lookup_failed"
    else:
        conns = [SpyConn() for _ in range(13)]

        def explode(conn, peer):
            raise RuntimeError

        loop = AcceptLoop(Scripted([*conns, BlockingIOError()]), explode)
        counter = lambda: loop.accept_callback_failed_total  # noqa: E731
        line = "afx-ops: accept_callback_failed"
    loop.accept_ready()
    assert counter() == 13
    assert _lines(capsys) == [line] * 10


def test_a_backoff_episode_logs_once_without_the_errno(capsys):
    loop, _ = _taking(Scripted([_errno(errno.EMFILE), _errno(errno.ENFILE)]))
    loop.accept_ready()
    loop.accept_ready()
    assert loop.accept_resource_error_total == 2
    assert _lines(capsys) == ["afx-ops: accept_backoff"]


# --- AC-35: 1 回の readiness あたり 64 試行 -------------------------------------------

@pytest.mark.parametrize(("queued", "first", "second"), [(63, 63, 0), (64, 64, 0), (65, 64, 1)])
def test_one_readiness_takes_at_most_64_connections_and_the_rest_wait(queued, first, second):
    """M-T1-ACCEPT-64: 65 回目の accept を同じ callback で試行する変異は落ちる。"""
    conns = [SpyConn() for _ in range(queued)]
    listener = Scripted([*conns, BlockingIOError(), BlockingIOError()])
    loop, taken = _taking(listener)
    assert ACCEPT_BUDGET == 64
    assert loop.accept_ready() == first
    # 63 本なら 64 回目は EAGAIN で終わる。64 / 65 本なら 64 試行でちょうど止まる
    assert listener.attempts == min(queued + 1, 64)
    assert loop.accept_ready() == second
    assert len(taken) == queued


def test_64_consecutive_aborted_connections_end_the_callback_without_a_65th_attempt():
    listener = Scripted([ConnectionAbortedError(errno.ECONNABORTED, "x")] * 70)
    loop, taken = _taking(listener)
    assert loop.accept_ready() == 0
    assert listener.attempts == 64 and len(listener.script) == 6 and taken == []


def test_eagain_is_one_attempt_and_a_normal_return():
    listener = Scripted([SpyConn(), BlockingIOError(errno.EAGAIN, "x"), SpyConn()])
    loop, taken = _taking(listener)
    assert loop.accept_ready() == 1
    assert listener.attempts == 2 and len(taken) == 1


def test_eintr_is_counted_and_retried_only_within_the_budget():
    listener = Scripted([InterruptedError(errno.EINTR, "x"), InterruptedError(errno.EINTR, "x"),
                         SpyConn(), BlockingIOError()])
    loop, taken = _taking(listener)
    assert loop.accept_ready() == 1
    assert listener.attempts == 4
    listener = Scripted([InterruptedError(errno.EINTR, "x")] * 70)
    loop, _ = _taking(listener)
    loop.accept_ready()
    assert listener.attempts == 64


def test_a_successful_64th_accept_is_delivered_before_the_callback_returns():
    conns = [SpyConn() for _ in range(65)]
    loop, taken = _taking(Scripted([*conns, BlockingIOError()]))
    loop.accept_ready()
    assert taken[-1][0] is conns[63] and len(taken) == 64


def test_the_deadline_callback_runs_between_two_aborting_readiness_callbacks():
    """64 回連続の ECONNABORTED でも event loop に制御が戻り、同時に ready の timer が進む。"""

    class Aborting(socket.socket):
        attempts = 0

        def accept(self):
            Aborting.attempts += 1
            raise ConnectionAbortedError(errno.ECONNABORTED, "x")

    raw = prebind_listener(0)
    port = raw.getsockname()[1]
    listener = Aborting(fileno=os.dup(raw.fileno()))
    raw.close()
    client = socket.create_connection(("127.0.0.1", port))

    async def main():
        loop = asyncio.get_running_loop()
        calls, ticks = [], []
        acceptor = AcceptLoop(listener, lambda conn, peer: None)
        original = acceptor.accept_ready

        def counted():
            before = Aborting.attempts
            original()
            calls.append(Aborting.attempts - before)

        acceptor._listener = listener
        loop.add_reader(listener.fileno(), counted)
        loop.call_soon(lambda: ticks.append(len(calls)))
        loop.call_later(0, lambda: ticks.append(len(calls)))
        await asyncio.sleep(0.05)
        loop.remove_reader(listener.fileno())
        return calls, ticks

    try:
        calls, ticks = asyncio.run(main())
    finally:
        listener.close()
        client.close()
    assert calls and all(count == 64 for count in calls)
    assert ticks and ticks[0] <= 1


@pytest.mark.parametrize("code", [errno.EMFILE, errno.ENFILE])
def test_an_accept_success_resets_the_exhaustion_count_whatever_happens_to_the_connection(code):
    """M-T1-EMFILE-RESET: 成功後も連続回数を戻さない変異は落ちる。"""
    for survivor in (SpyConn(peer=("10.0.0.1", 1)), SpyConn(error=OSError(errno.ENOTCONN, "x")),
                     SpyConn(peer=("127.0.0.1", 1))):
        gave_up = []
        listener = Scripted([_errno(code), _errno(code), survivor, _errno(code), _errno(code)])

        def refuse(conn, peer):
            raise RuntimeError

        loop = AcceptLoop(listener, refuse, allow=ONLY_127_1, exhausted_limit=3,
                          on_exhausted=lambda: gave_up.append(1))
        for _ in range(4):
            loop.accept_ready()
        assert gave_up == [] and listener.closed == 0


@pytest.mark.parametrize("code", [errno.EMFILE, errno.ENFILE])
def test_consecutive_exhaustion_up_to_the_limit_gives_up_after_closing(code, capsys):
    listener = Scripted([_errno(code)] * 3)
    order = []
    loop = AcceptLoop(listener, lambda c, p: None, exhausted_limit=3,
                      on_exhausted=lambda: order.append(listener.closed))
    for _ in range(3):
        loop.accept_ready()
    assert order == [1] and loop.accept_exhausted_total == 1
    assert _lines(capsys) == ["afx-ops: accept_backoff", "afx-ops: accept_exhausted"]


PER_CONNECTION = [errno.ECONNABORTED, errno.EPROTO, errno.ENETDOWN, errno.ENOPROTOOPT,
                  errno.EHOSTDOWN, errno.ENONET, errno.EHOSTUNREACH, errno.EOPNOTSUPP,
                  errno.ENETUNREACH, errno.ENOBUFS, errno.ENOMEM]


@pytest.mark.parametrize("code", PER_CONNECTION)
def test_a_per_connection_errno_is_skipped_and_the_next_connection_is_taken(code, capsys):
    survivor = SpyConn()
    listener = Scripted([_errno(code), survivor, BlockingIOError()])
    taken = []
    loop = AcceptLoop(listener, lambda c, p: taken.append(c),
                      on_failed=lambda: pytest.fail("listener must stay open"))
    assert loop.accept_ready() == 1 and listener.attempts == 3
    assert taken == [survivor] and listener.closed == 0
    assert loop.accept_failed_total == 0 and loop.accept_exhausted_total == 0
    assert _lines(capsys) == []


@pytest.mark.parametrize("code", PER_CONNECTION)
def test_64_consecutive_per_connection_errors_return_without_closing_or_exhausting(code, capsys):
    listener = Scripted([_errno(code)] * 70)
    loop = AcceptLoop(listener, lambda c, p: None,
                      on_failed=lambda: pytest.fail("listener must stay open"),
                      on_exhausted=lambda: pytest.fail("not fd exhaustion"))
    assert loop.accept_ready() == 0
    assert listener.attempts == ACCEPT_BUDGET == 64 and listener.closed == 0
    assert loop.accept_failed_total == loop.accept_exhausted_total == 0
    assert _lines(capsys) == []


@pytest.mark.parametrize("code", [errno.EPERM, errno.EBADF, errno.EINVAL, errno.EFAULT])
def test_an_unexpected_errno_closes_the_listener_and_ends_with_accept_failed(code, capsys):
    listener = Scripted([_errno(code), SpyConn()])
    order = []
    loop = AcceptLoop(listener, lambda c, p: None,
                      on_failed=lambda: order.append(listener.closed))
    assert loop.accept_ready() == 0
    assert order == [1] and listener.attempts == 1
    assert loop.accept_failed_total == 1 and loop.accept_exhausted_total == 0
    assert _lines(capsys) == ["afx-ops: accept_failed"]


def test_the_default_failure_exit_is_rc_74_apart_from_exhaustion_rc_72():
    loop = AcceptLoop(Scripted([_errno(errno.EPERM)]), lambda c, p: None)
    with pytest.raises(SystemExit) as info:
        loop.accept_ready()
    assert info.value.code == accept_loop.RC_ACCEPT_FAILED == 74
    assert accept_loop.RC_ACCEPT_EXHAUSTED == 72


_FAILED_CHILD = r"""
import asyncio, errno, socket
from afx_ops.isolation.accept_loop import AcceptLoop, prebind_listener

class Broken(socket.socket):
    def accept(self):
        raise OSError(errno.EPERM, "x")

raw = prebind_listener(0)
port = raw.getsockname()[1]
listener = Broken(fileno=raw.detach())
client = socket.create_connection(("127.0.0.1", port))

async def main():
    loop = asyncio.get_running_loop()
    AcceptLoop(listener, lambda c, p: None).attach(loop)
    await asyncio.sleep(5)
    print("still running", flush=True)

asyncio.run(main())
"""


def test_an_unexpected_errno_ends_a_real_process_with_a_fixed_line_and_rc_74():
    done = subprocess.run([sys.executable, "-c", _FAILED_CHILD], capture_output=True, text=True,
                          timeout=30)
    assert done.returncode == 74, done.stderr[-1000:]
    assert done.stderr == "afx-ops: accept_failed\n"
    assert "still running" not in done.stdout


# --- 実 listener と runtime: 許可外は slot も protocol も作らない --------------------------

def _connect(port: int, source: str) -> socket.socket:
    sock = socket.socket()
    sock.bind((source, 0))
    sock.connect(("127.0.0.1", port))
    return sock


class CountingClient:
    def __init__(self) -> None:
        self.calls = []

    def status(self):
        self.calls.append("status")
        raise AssertionError("no daemon call is expected")


def _serve_scenario(allow, sources: list[str], *, settle: float = 0.4, extra=None):
    """listener に `sources` の送信元から順に接続し、runtime の slot / protocol / client を見る。"""
    listener = prebind_listener(0)
    port = listener.getsockname()[1]
    client = CountingClient()

    async def main():
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        slots, protocols = [], []
        original_slot = runtime._Slot

        class CountingSlot(original_slot):
            __slots__ = ()

            def __init__(self, *args, **kwargs):
                slots.append(args)
                super().__init__(*args, **kwargs)

        runtime._Slot = CountingSlot
        service = runtime._Service(runtime._aiohttp_parts(), loop, client, executor, None)
        original_protocol = service.server.protocol

        def counted(slot):
            protocols.append(slot)
            return original_protocol(slot)

        service.server.protocol = counted
        acceptor = AcceptLoop(listener, service.accept, allow=allow)
        acceptor.attach(loop)
        peers = []
        try:
            for source in sources:
                peers.append(_connect(port, source))
            if extra is not None:
                await extra(port, peers)
            await asyncio.sleep(settle)
            return {"slots": len(slots), "live": len(service.slots), "protocols": len(protocols),
                    "refused": service.refused, "rejected": acceptor.web_peer_rejected_total,
                    "peers": [slot.peer for slot in service.slots]}
        finally:
            runtime._Slot = original_slot
            acceptor.detach(loop)
            for peer in peers:
                peer.close()
            service.close()
            executor.shutdown(False, cancel_futures=True)
            await asyncio.sleep(0.05)

    try:
        return asyncio.run(main()), client
    finally:
        listener.close()


def test_connections_outside_allow_create_no_slot_no_protocol_and_no_daemon_call():
    """M-T1-ALLOW-LATE: allow の判定を slot 生成後へ移す変異は落ちる。"""
    result, client = _serve_scenario(ONLY_127_1, ["127.0.0.2"] * 20)
    assert result["slots"] == 0 and result["protocols"] == 0 and result["live"] == 0
    assert result["rejected"] == 20 and client.calls == []


def test_the_peer_stored_in_the_slot_is_the_accepted_peer_address():
    result, _client = _serve_scenario(ONLY_127_1, ["127.0.0.1"])
    assert result["slots"] == 1 and result["protocols"] == 1
    assert result["peers"] == ["127.0.0.1"]


@pytest.mark.parametrize(("allowed", "refused"), [(15, 0), (16, 0), (17, 1)])
def test_rejected_connections_do_not_consume_the_sixteen_slots(allowed, refused):
    sources = ["127.0.0.2"] * 20 + ["127.0.0.1"] * allowed
    result, _client = _serve_scenario(ONLY_127_1, sources, settle=0.6)
    assert result["rejected"] == 20
    assert result["live"] == min(allowed, runtime.MAX_CONNECTIONS)
    assert result["refused"] == refused


def test_a_forwarded_for_header_does_not_change_who_is_allowed():
    """M-T1-ALLOW-PEER-HEADER: allow の元を proxy header にする変異は落ちる。"""

    async def spoof(port, peers):
        for peer in peers:
            peer.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nX-Forwarded-For: 127.0.0.1\r\n"
                         b"Forwarded: for=127.0.0.1\r\n\r\n")

    result, client = _serve_scenario(ONLY_127_1, ["127.0.0.2"], extra=spoof)
    assert result["slots"] == 0 and result["rejected"] == 1 and client.calls == []


def test_the_slot_and_runtime_have_no_proxy_header_source_for_the_peer():
    source = open(runtime.__file__, encoding="utf-8").read().lower()
    for word in ("x-forwarded-for", "forwarded", "x-real-ip"):
        assert word not in source
    assert "peer" in runtime._Slot.__slots__


def test_a_pending_connection_from_outside_allow_is_closed_with_no_bytes():
    listener = prebind_listener(0)
    port = listener.getsockname()[1]
    peer = _connect(port, "127.0.0.2")
    peer.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    loop = AcceptLoop(listener, lambda c, p: pytest.fail("must not be handed over"),
                      allow=ONLY_127_1)
    try:
        deadline = time.monotonic() + 5
        while loop.web_peer_rejected_total == 0 and time.monotonic() < deadline:
            loop.accept_ready()
        peer.settimeout(5)
        received = b""
        try:
            while chunk := peer.recv(4096):
                received += chunk
        except ConnectionResetError:
            pass
        assert received == b""
    finally:
        peer.close()
        listener.close()
