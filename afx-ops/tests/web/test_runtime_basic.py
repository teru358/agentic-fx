"""web runtime の Basic 認証: 実 aiohttp を loopback の実接続で叩き、wire の Authorization が
OWS の正規化前のまま認証器へ届くこと、rate limit、固定の journal、daemon 呼出し 0 を見る。

隔離 (Landlock / seccomp) は掛けない。隔離下の観測は test_runtime_isolated の basic 項目が持つ。
"""
from __future__ import annotations

import asyncio
import base64
import socket
import threading
from asyncio import selector_events

import pytest

from afx_ops.api_client import Reply
from afx_ops.isolation.accept_loop import prebind_listener
from afx_ops.web import auth, runtime
from afx_ops.web.request import HostPolicy

PASSWORD = b"loopback-secret-0123"
TOKEN = base64.b64encode(b"afx:" + PASSWORD).decode("ascii")
WRONG = base64.b64encode(b"afx:wrong-wrong-wrong-1").decode("ascii")


@pytest.fixture(autouse=True)
def _send_only(monkeypatch):
    monkeypatch.setattr(selector_events, "_HAS_SENDMSG", True)
    runtime.use_send_only_transport()


class Clock:
    now = 100.0

    def __call__(self) -> float:
        return self.now


class Client:
    def __init__(self) -> None:
        self.calls = 0

    def status(self):
        self.calls += 1
        return Reply(200, {"mode": "paper"}, None)


class Server:
    def __init__(self, connections: int, authenticator) -> None:
        self.client = Client()
        listener = prebind_listener(0)
        self.port = listener.getsockname()[1]
        serving = threading.Event()
        self.outcome: list[object] = []

        def main() -> None:
            try:
                asyncio.run(runtime._serve(
                    runtime._aiohttp_parts(), listener, self.client, serving.set, connections,
                    host_policy=HostPolicy("127.0.0.1", self.port, True),
                    authenticator=authenticator))
                self.outcome.append("done")
            except BaseException as exc:  # noqa: BLE001 - 試験の報告用
                self.outcome.append(exc)
            finally:
                listener.close()

        self.thread = threading.Thread(target=main, daemon=True)
        self.thread.start()
        assert serving.wait(10)

    def ask(self, authorization: bytes | None, method: bytes = b"GET", path: bytes = b"/",
            source: str | None = None) -> bytes:
        line = b"" if authorization is None else b"Authorization:" + authorization + b"\r\n"
        raw = (method + b" " + path + b" HTTP/1.1\r\nHost: 127.0.0.1:"
               + str(self.port).encode() + b"\r\n" + line + b"\r\n")
        with socket.create_connection(("127.0.0.1", self.port), timeout=10,
                                      source_address=None if source is None else (source, 0)) as sock:
            sock.sendall(raw)
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        return data

    def finish(self) -> None:
        self.thread.join(15)
        assert self.outcome == ["done"]


def status_of(response: bytes) -> int:
    return int(response.split(b" ", 2)[1])


def header(response: bytes, name: bytes) -> bytes | None:
    for line in response.split(b"\r\n\r\n", 1)[0].split(b"\r\n")[1:]:
        key, _, value = line.partition(b":")
        if key.lower() == name.lower():
            return value.strip()
    return None


def body_of(response: bytes) -> bytes:
    return response.split(b"\r\n\r\n", 1)[1]


def test_the_wire_value_reaches_the_authenticator_without_ows_normalization():
    token = TOKEN.encode()
    forms = [
        (b" Basic " + token, 200),
        (b"Basic " + token, 200),
        (b" Basic  " + token, 401),
        (b" Basic\t" + token, 401),
        (b" Basic " + token + b" ", 401),
        (b" Basic " + token + b"\t", 401),
        (b"  Basic " + token, 401),
        (b"\tBasic " + token, 401),
        (b" Basic " + WRONG.encode(), 401),
        (None, 401),
    ]
    clock = Clock()
    server = Server(len(forms), auth.BasicAuthenticator(PASSWORD, clock=clock))
    try:
        got = []
        for value, _ in forms:
            got.append(status_of(server.ask(value)))
            clock.now += 61.0
    finally:
        server.finish()
    assert got == [expected for _, expected in forms]
    assert server.client.calls == 2


def test_the_401_carries_the_challenge_and_the_fixed_body():
    server = Server(2, auth.BasicAuthenticator(PASSWORD))
    try:
        first, second = server.ask(None), server.ask(b" Basic " + WRONG.encode())
    finally:
        server.finish()
    for response in (first, second):
        assert status_of(response) == 401
        assert header(response, b"WWW-Authenticate") == b'Basic realm="afx"'
    assert body_of(first) == body_of(second)
    assert server.client.calls == 0


def test_the_sixth_failure_is_429_for_get_and_head_with_the_same_length(capfd):
    server = Server(8, auth.BasicAuthenticator(PASSWORD))
    try:
        codes = [status_of(server.ask(b" Basic " + WRONG.encode())) for _ in range(5)]
        get = server.ask(b" Basic " + TOKEN.encode())
        head = server.ask(b" Basic " + TOKEN.encode(), method=b"HEAD")
        again = server.ask(b" Basic " + TOKEN.encode())
    finally:
        server.finish()
    assert codes == [401] * 5
    assert status_of(get) == status_of(head) == status_of(again) == 429
    assert 1 <= int(header(get, b"Retry-After")) <= 60
    assert header(head, b"Content-Length") == header(get, b"Content-Length") == str(len(body_of(get))).encode()
    assert body_of(head) == b""
    assert server.client.calls == 0
    err = capfd.readouterr().err
    assert err.splitlines() == ["afx-ops: web_auth_failed"] * 5 + ["afx-ops: web_auth_limited"] * 3


def test_a_head_401_has_the_get_length():
    server = Server(2, auth.BasicAuthenticator(PASSWORD))
    try:
        get, head = server.ask(None), server.ask(None, method=b"HEAD")
    finally:
        server.finish()
    assert body_of(head) == b"" and body_of(get) != b""
    assert header(head, b"Content-Length") == header(get, b"Content-Length")


def test_without_an_authenticator_the_header_is_not_needed():
    server = Server(1, None)
    try:
        assert status_of(server.ask(None)) == 200
    finally:
        server.finish()


def test_the_rate_limit_key_is_the_accepted_socket_peer():
    """M-T2-PEER-FROM-SOCKET: 送信元ごとの窓は accept した socket の peer で分かれる。"""
    server = Server(8, auth.BasicAuthenticator(PASSWORD))
    try:
        failures = [status_of(server.ask(b" Basic " + WRONG.encode(), source="127.0.0.2"))
                    for _ in range(5)]
        limited = status_of(server.ask(b" Basic " + TOKEN.encode(), source="127.0.0.2"))
        other = status_of(server.ask(b" Basic " + TOKEN.encode(), source="127.0.0.1"))
        spoofed = status_of(server.ask(None, source="127.0.0.3"))
    finally:
        server.finish()
    assert failures == [401] * 5
    assert (limited, other, spoofed) == (429, 200, 401)


def test_the_authorization_of_one_header_block_is_taken_once_per_connection():
    """2 本目の要求は 1 本目の header block を再利用できず、資格情報なしとして扱われる。"""
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    seen: list[list[str]] = []

    async def run() -> None:
        service = runtime._Service.__new__(runtime._Service)
        service.slots = set()
        service.calls = set()
        slot = runtime._Slot(lambda _slot: None, "127.0.0.1")
        service.slots.add(slot)
        service._loop = asyncio.get_running_loop()
        service._executor = ThreadPoolExecutor(max_workers=1)
        service._response = lambda status, headers, body: status

        def fake_run(_call, _raw, _peer, authorization):
            seen.append(authorization)
            return SimpleNamespace(status=200, headers={}, body=b"")

        service._run = fake_run

        async def raw(_request):
            return b""

        original = runtime._raw_request
        runtime._raw_request = raw
        try:
            protocol = SimpleNamespace(
                _slot=slot, wire_head=b"GET / HTTP/1.1\r\nAuthorization: Basic abc\r\n")
            request = SimpleNamespace(protocol=protocol)
            await service.handle(request)
            await service.handle(request)
        finally:
            runtime._raw_request = original
            service._executor.shutdown()

    asyncio.run(run())
    assert seen == [["Basic abc"], []]
