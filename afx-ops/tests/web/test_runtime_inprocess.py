"""web runtime を同じ process の loopback で回す: 切断・取消し・停止の経路。

隔離 (Landlock / seccomp) は掛けない。隔離下の応答そのものは test_runtime_isolated が見る。
daemon の代わりに、呼出しが固まる fake client を渡す。
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from asyncio import selector_events

import pytest

from afx_ops.api_client import Reply
from afx_ops.isolation.accept_loop import prebind_listener
from afx_ops.web import runtime

GET = b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"


@pytest.fixture(autouse=True)
def _send_only(monkeypatch):
    monkeypatch.setattr(selector_events, "_HAS_SENDMSG", True)
    runtime.use_send_only_transport()


class BlockingClient:
    """`blocking` の間の呼出しは、その thread への interrupt まで戻らない。"""

    def __init__(self) -> None:
        self.blocking = True
        self.calls = 0
        self.lock = threading.Lock()
        self.events: dict[int, threading.Event] = {}
        self.marked: set[int] = set()

    def status(self):
        ident = threading.get_ident()
        with self.lock:
            self.calls += 1
            stuck = self.blocking
            event = self.events.setdefault(ident, threading.Event())
            if ident in self.marked:
                return Reply(200, {"mode": "paper"}, None)
        if stuck:
            event.wait(20)
        return Reply(200, {"mode": "paper"}, None)

    def interrupt_thread(self, ident: int) -> None:
        with self.lock:
            self.marked.add(ident)
            self.events.setdefault(ident, threading.Event()).set()

    def release_thread(self, ident: int) -> None:
        with self.lock:
            self.marked.discard(ident)
            self.events.setdefault(ident, threading.Event()).clear()

    def free_all(self) -> None:
        with self.lock:
            for event in self.events.values():
                event.set()
            self.blocking = False


def start(client, *, stop_after: int | None = None):
    listener = prebind_listener(0)
    port = listener.getsockname()[1]
    serving = threading.Event()
    outcome: list[object] = []

    def main() -> None:
        try:
            asyncio.run(runtime._serve(runtime._aiohttp_parts(), listener, client,
                                       serving.set, stop_after))
            outcome.append("done")
        except BaseException as exc:  # noqa: BLE001 - 試験の報告用
            outcome.append(exc)
        finally:
            listener.close()

    thread = threading.Thread(target=main, daemon=True)
    thread.start()
    assert serving.wait(10)
    return port, thread, outcome


def exchange(port: int, raw: bytes, timeout: float = 10) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(raw)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    return data


def test_requests_of_dropped_connections_do_not_hold_the_workers():
    client = BlockingClient()
    port, thread, outcome = start(client, stop_after=runtime.MAX_CONNECTIONS + 1)
    try:
        for _ in range(runtime.MAX_CONNECTIONS):
            sock = socket.create_connection(("127.0.0.1", port), timeout=10)
            sock.sendall(GET)
            sock.close()
        # worker が全部固まった呼出しで埋まるまで待ち、以後の呼出しは固まらないようにする
        deadline = time.monotonic() + 10
        while client.calls < runtime.MAX_WORKERS and time.monotonic() < deadline:
            time.sleep(0.02)
        assert client.calls >= runtime.MAX_WORKERS
        client.blocking = False
        started = time.monotonic()
        reply = b""
        # 切断が runtime に届くまでは枠が埋まっていて、受けてすぐ閉じられる
        while not reply and time.monotonic() - started < 8:
            reply = exchange(port, GET)
        elapsed = time.monotonic() - started
        assert reply.startswith(b"HTTP/1.1 200"), reply[:80]
        assert elapsed < 8
        thread.join(15)
        assert outcome == ["done"]
    finally:
        client.free_all()
        thread.join(5)


def test_a_cancelled_connect_gives_back_its_socket_and_slot():
    async def main() -> tuple[int, int, bool]:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()

        async def hang(factory, sock):
            started.set()
            await asyncio.Event().wait()

        loop.connect_accepted_socket = hang
        executor = runtime.ThreadPoolExecutor(max_workers=1)
        service = runtime._Service(runtime._aiohttp_parts(), loop, object(), executor, 1)
        left, right = socket.socketpair()
        try:
            service.accept(left, "127.0.0.1")
            await started.wait()
            assert len(service.slots) == 1
            for task in list(service.tasks):
                task.cancel()
            await asyncio.wait_for(service.stopped.wait(), 5)
            return len(service.slots), left.fileno(), service.closed == 1
        finally:
            right.close()
            left.close()
            executor.shutdown(False)

    slots, fileno, counted = asyncio.run(main())
    assert slots == 0 and fileno == -1 and counted


def test_the_loop_keeps_running_while_shutdown_waits_for_a_call(monkeypatch):
    monkeypatch.setattr(runtime, "SHUTDOWN_GRACE", 5.0)
    release = threading.Event()

    class Slow:
        def status(self):
            release.wait(1.5)
            return Reply(200, {"mode": "paper"}, None)

    listener = prebind_listener(0)
    port = listener.getsockname()[1]
    gaps: list[float] = []

    async def main() -> None:
        loop = asyncio.get_running_loop()
        serving = asyncio.Event()

        async def tick() -> None:
            last = loop.time()
            while True:
                await asyncio.sleep(0.05)
                now = loop.time()
                gaps.append(now - last)
                last = now

        async def client_side() -> None:
            await serving.wait()
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(GET)
            await writer.drain()
            await asyncio.sleep(0.3)
            writer.close()

        ticker = loop.create_task(tick())
        client_task = loop.create_task(client_side())
        await runtime._serve(runtime._aiohttp_parts(), listener, Slow(), serving.set, 1)
        # 塞がれた間の隙間は、塞ぎが解けた後の最初の tick で記録される
        await asyncio.sleep(0.2)
        ticker.cancel()
        await client_task

    try:
        asyncio.run(main())
    finally:
        release.set()
        listener.close()
    # drain が loop を塞ぐと、この間 tick が止まって大きな隙間になる
    assert max(gaps) < 0.6, max(gaps)


def _split_reply(raw: bytes) -> tuple[bytes, bytes]:
    head, _sep, body = raw.partition(b"\r\n\r\n")
    return head, body


def _content_length(head: bytes) -> int:
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            return int(line.split(b":", 1)[1])
    raise AssertionError(head)


def test_head_refusals_before_the_adapter_carry_no_body_and_the_get_length():
    client = BlockingClient()
    client.blocking = False
    port, thread, _outcome = start(client, stop_after=4)
    try:
        huge = b"X-Pad: " + b"a" * (runtime.MAX_HEADER_BYTES + 64) + b"\r\n"
        cases = {
            "wire": (b"HEAD / HTTP/1.1\r\nHost: x\r\n" + huge + b"Connection: close\r\n\r\n",
                     b"GET / HTTP/1.1\r\nHost: x\r\n" + huge + b"Connection: close\r\n\r\n"),
            "parser": (b"HEAD / HTTP/1.1\r\nBad Header\r\n\r\n",
                       b"GET / HTTP/1.1\r\nBad Header\r\n\r\n"),
        }
        for name, (head_raw, get_raw) in cases.items():
            head_head, head_body = _split_reply(exchange(port, head_raw))
            get_head, get_body = _split_reply(exchange(port, get_raw))
            assert get_body, name
            assert head_head.split(b"\r\n")[0] == get_head.split(b"\r\n")[0], name
            assert head_body == b"", name
            assert _content_length(head_head) == _content_length(get_head) == len(get_body), name
    finally:
        client.free_all()
        thread.join(15)


def test_shutdown_frees_the_workers_of_connections_still_open(monkeypatch):
    monkeypatch.setattr(runtime, "SHUTDOWN_GRACE", 10.0)
    client = BlockingClient()
    port, thread, outcome = start(client, stop_after=1)
    live = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        live.sendall(GET)
        deadline = time.monotonic() + 10
        while client.calls < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert client.calls == 1
        # 別の接続が閉じて停止に入る。live の呼出しは固まったまま、接続は開いている
        socket.create_connection(("127.0.0.1", port), timeout=10).close()
        started = time.monotonic()
        thread.join(8)
        assert not thread.is_alive()
        # 取消しが走れば drain は猶予 (10 s) を待たずに終わる
        assert time.monotonic() - started < 5
        assert outcome == ["done"]
    finally:
        live.close()
        client.free_all()
        thread.join(5)


class BigPageClient:
    def status(self):
        return Reply(200, {"mode": "paper"}, None)


@pytest.fixture
def services(monkeypatch):
    made: list[runtime._Service] = []
    original = runtime._Service.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        made.append(self)

    monkeypatch.setattr(runtime._Service, "__init__", init)
    return made


def _wait_for(condition, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@pytest.mark.parametrize("size", [2000, 2048, 4096, 20000])
def test_a_large_reply_ends_without_a_loop_error(monkeypatch, capsys, services, size):
    from afx_ops.web import render
    monkeypatch.setattr(render, "render_status", lambda data: b"x" * size)
    port, thread, outcome = start(BigPageClient(), stop_after=3)
    try:
        for _ in range(3):
            reply = exchange(port, GET)
            assert reply.startswith(b"HTTP/1.1 200"), reply[:80]
            assert len(reply) > size
        thread.join(10)
        assert outcome == ["done"]
    finally:
        thread.join(5)
    assert "web_loop_error" not in capsys.readouterr().err
    assert len(services[0].slots) == 0


def test_repeated_large_replies_keep_the_slots_and_the_next_request_answers(
        monkeypatch, capsys, services):
    from afx_ops.web import render
    monkeypatch.setattr(render, "render_status", lambda data: b"x" * 2500)
    port, thread, outcome = start(BigPageClient(), stop_after=60)
    try:
        for _ in range(50):
            assert exchange(port, GET).startswith(b"HTTP/1.1 200")
        service = services[0]
        assert _wait_for(lambda: len(service.slots) == 0)
        assert exchange(port, GET).startswith(b"HTTP/1.1 200")
        assert _wait_for(lambda: len(service.slots) == 0)
        assert service.refused == 0
    finally:
        for _ in range(10):
            try:
                exchange(port, GET)
            except OSError:
                break
        thread.join(10)
    assert "web_loop_error" not in capsys.readouterr().err


def test_a_slot_does_not_abort_a_transport_that_is_already_closing():
    class Transport:
        def __init__(self, closing: bool) -> None:
            self.closing = closing
            self.aborted = 0

        def is_closing(self) -> bool:
            return self.closing

        def abort(self) -> None:
            self.aborted += 1

    for closing, aborted in ((True, 0), (False, 1)):
        slot = runtime._Slot(lambda _slot: None, "peer")
        slot.transport = Transport(closing)
        slot.close()
        assert slot.transport.aborted == aborted


def test_a_finished_connection_leaves_no_transport_in_its_slot():
    seen: list[object] = []
    parts = runtime._aiohttp_parts()
    server_cls = parts[0]
    slot = runtime._Slot(lambda s: seen.append(s.transport), "peer")

    async def main() -> None:
        loop = asyncio.get_running_loop()
        server = server_cls(lambda request: None, loop=loop)
        protocol = server.protocol(slot)
        slot.transport = object()  # type: ignore[assignment]
        protocol.connection_lost(None)

    asyncio.run(main())
    assert seen == [None]
