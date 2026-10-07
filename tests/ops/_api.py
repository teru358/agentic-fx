"""実 UDS + 実 server thread で操作 API を試すための部品。"""
from __future__ import annotations

import concurrent.futures
import socket
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from agentic_fx.ops import keys
from agentic_fx.ops.api_server import ApiLimits, ApiServer
from agentic_fx.ops.contracts import Principal


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


class FakeImprove:
    def __init__(self) -> None:
        self.calls = 0

    def submit_manual(self, *, on_prepared=None):
        self.calls += 1
        if on_prepared is not None:
            on_prepared(41)
        return 41


def answered_ask(question):
    future = concurrent.futures.Future()
    future.set_result("answer")
    return future


@dataclass
class ApiEnv:
    env: object
    service: object
    server: ApiServer
    socket_path: Path
    key_dir: Path
    notifier: FakeNotifier
    improve: FakeImprove
    clients: list = field(default_factory=list)

    def token(self, principal: Principal) -> str:
        return keys.read_token(self.key_dir, principal)

    def client(self, principal: Principal | None = None, *, token: str | None = None,
               timeout: float = 15.0) -> httpx.Client:
        headers = {}
        if principal is not None:
            token = self.token(principal)
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        client = httpx.Client(transport=httpx.HTTPTransport(uds=str(self.socket_path)),
                              base_url="http://afx", headers=headers, timeout=timeout,
                              event_hooks={"request": [_declare_empty_body]})
        self.clients.append(client)
        return client

    def raw(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(self.socket_path))
        return sock


def _declare_empty_body(request) -> None:
    # server は GET にも Content-Length を求めるので、httpx が省く 0 を明示する。
    if request.method == "GET" and "content-length" not in request.headers:
        request.headers["Content-Length"] = "0"


def make_api(ops_env, *, limits: ApiLimits = ApiLimits(), start: bool = True,
             service_kwargs: dict | None = None, **server_kwargs) -> ApiEnv:
    directory = keys.key_dir(ops_env.root)
    keys.ensure_initialized(directory, root=ops_env.root)
    improve = FakeImprove()
    kwargs = dict(ask_submitter=answered_ask, improve_supervisor=improve,
                  data_resume=lambda ack: {"requested": True, "acknowledge": ack})
    kwargs.update(service_kwargs or {})
    service = ops_env.service(**kwargs)
    notifier = FakeNotifier()
    socket_path = ops_env.root / "data" / "run" / "api.sock"
    server = ApiServer(service, keys.load_keyset(directory), socket_path, limits=limits,
                       monotonic=ops_env.mono, wall_clock=ops_env.wall, notifier=notifier,
                       **server_kwargs)
    if start:
        server.start()
    return ApiEnv(ops_env, service, server, socket_path, directory, notifier, improve)


@pytest.fixture
def api(ops_env):
    api_env = make_api(ops_env)
    yield api_env
    for client in api_env.clients:
        client.close()
    api_env.server.stop(5.0)


def closed_order(env) -> int:
    now = env.wall().isoformat()
    cur = env.conn.execute(
        "INSERT INTO orders(pair,direction,entry_type,horizon,status,created_at,updated_at) "
        "VALUES ('EURUSD','long','market','x','closed',?,?)", (now, now))
    env.conn.commit()
    return int(cur.lastrowid)


def backlog_row(env, status: str = "open") -> int:
    from agentic_fx.store import backlog
    backlog_id = backlog.add(env.conn, "idea", "user", env.wall())
    if status != "open":
        env.conn.execute("UPDATE improvement_backlog SET status=? WHERE id=?",
                         (status, backlog_id))
        env.conn.commit()
    return backlog_id
