"""web runtime の書込みの配線: `write` が真のときだけ書込みの adapter が生き、読取りと書込みの
daemon 呼出しは同じ `ops-web-write` の client を通る。偽のときは今までどおり読取りだけ。

隔離は掛けない (実 aiohttp・実 loopback 接続・fake client)。隔離を通した結合は
本体 tests/ops の test_web_write_isolated_e2e が持つ。
"""
from __future__ import annotations

import base64
import socket
import threading
from asyncio import selector_events
from pathlib import Path
from urllib.parse import urlencode

import pytest

from afx_ops.api_client import WebClient, WebWriteClient
from afx_ops.config import OpsConfig
from afx_ops.isolation import startup
from afx_ops.isolation.accept_loop import prebind_listener
from afx_ops.web import runtime

from _write_support import DIGEST, FORM, JOB, FakeClient, token_of

PASSWORD = b"runtime-write-pass-42"
BASIC = "Basic " + base64.b64encode(b"afx:" + PASSWORD).decode("ascii")


@pytest.fixture(autouse=True)
def _send_only(monkeypatch):
    monkeypatch.setattr(selector_events, "_HAS_SENDMSG", True)
    runtime.use_send_only_transport()


class _Response:
    def __init__(self, raw: bytes) -> None:
        head, _, self.body = raw.partition(b"\r\n\r\n")
        self.status = int(head.split(b" ", 2)[1])


class Served:
    """`runtime.run` を fake の Isolated で回す。`connections` 本の接続が閉じたら戻る。

    `client` を渡すと run にそのまま渡す (試験の差し替え)。None なら run は client_for で作る。
    """

    def __init__(self, *, write: bool, connections: int, bind: str = "127.0.0.1",
                 client: object | None = "fake") -> None:
        self.listener = prebind_listener(0)
        self.port = self.listener.getsockname()[1]
        self.bind = bind
        self.client = FakeClient() if client == "fake" else client
        config = OpsConfig(Path("/srv/afx"), Path("/srv/keys"), None, port=self.port,
                           bind=bind, auth="basic", write=write)
        self.isolated = startup.Isolated(
            feature="web", pid=1, proc_dir="/proc/1", secrets={startup.WEB_PASSWORD: PASSWORD},
            listener=self.listener, kept_fds=frozenset({0, 1, 2}), config=config,
            endpoint=startup.endpoint_for(Path("/srv/afx")), api_token=b"x" * 64,
            state_dir=Path("/srv/state"))
        serving = threading.Event()
        self.outcome: list[object] = []

        def main() -> None:
            try:
                kwargs = {} if self.client is None else {"client": self.client}
                self.outcome.append(runtime.run(self.isolated, on_serving=serving.set,
                                                stop_after=connections, **kwargs))
            except BaseException as exc:  # noqa: BLE001 - 試験の報告用
                self.outcome.append(exc)
            finally:
                self.listener.close()

        self.thread = threading.Thread(target=main, daemon=True)
        self.thread.start()
        assert serving.wait(10)

    def ask(self, method: str, path: str, form: dict | None = None, *, origin: bool = True):
        host = f"{self.bind}:{self.port}"
        body = urlencode(form).encode("ascii") if form is not None else b""
        lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", f"Authorization: {BASIC}",
                 "Connection: close"]
        if origin:
            lines.append(f"Origin: http://{host}")
        if form is not None:
            lines += [f"Content-Type: {FORM}", f"Content-Length: {len(body)}"]
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(raw)
            data = b""
            while chunk := sock.recv(65536):
                data += chunk
        return _Response(data)

    def finish(self) -> None:
        self.thread.join(15)
        assert self.outcome == [0], self.outcome


def _calls(client: FakeClient, *names: str) -> list[tuple]:
    return [call for call in client.calls if call[0] in names]


def test_write_true_serves_decision_forms_and_a_confirmed_approval_reaches_the_client():
    served = Served(write=True, connections=4)
    detail = served.ask("GET", "/approvals/7")
    assert detail.status == 200 and b'action="/approvals/7/approve"' in detail.body
    first = served.ask("POST", "/approvals/7/approve", {"payload_sha256": DIGEST})
    assert first.status == 200
    assert _calls(served.client, "approve") == []
    done = served.ask("POST", "/confirm", {"token": token_of(first)})
    assert done.status == 200 and JOB.encode() in done.body
    form = served.ask("GET", "/improve")
    assert form.status == 200
    served.finish()
    assert _calls(served.client, "approve") == [("approve", "web:afx", 7, DIGEST)]


def test_write_true_rejects_a_post_without_the_origin_before_the_client():
    served = Served(write=True, connections=1)
    response = served.ask("POST", "/approvals/7/approve", {"payload_sha256": DIGEST},
                          origin=False)
    served.finish()
    assert response.status == 403
    assert _calls(served.client, "approve", "reject", "improve_run") == []


def test_write_false_keeps_the_read_only_adapter():
    served = Served(write=False, connections=7)
    detail = served.ask("GET", "/approvals/7")
    approve = served.ask("POST", "/approvals/7/approve", {"payload_sha256": DIGEST})
    reject = served.ask("POST", "/approvals/7/reject", {})
    improve = served.ask("POST", "/improve/runs", {"duration_sec": "600"})
    form = served.ask("GET", "/improve")
    job_view = served.ask("GET", "/jobs/" + JOB)
    job_cancel = served.ask("POST", "/jobs/" + JOB + "/cancel", {})
    served.finish()
    assert (job_view.status, job_cancel.status) == (404, 404)
    # 承認詳細は読めるが form は無く、書込みの route は 405 / 404 で、client は 1 回も書かない
    assert detail.status == 200 and b"<form" not in detail.body
    assert (approve.status, reject.status, improve.status, form.status) == (404, 404, 404, 404)
    assert _calls(served.client, "approve", "reject", "improve_run", "job", "cancel_job") == []


def test_write_true_on_a_non_loopback_bind_never_writes():
    # 起動時の検査が先に止めるが、runtime も経路が loopback でなければ書込みを 403 にする
    served = Served(write=True, connections=2, bind="192.0.2.10")
    post = served.ask("POST", "/approvals/7/approve", {"payload_sha256": DIGEST})
    form = served.ask("GET", "/improve")
    served.finish()
    assert post.status == 403 and form.status == 403
    assert _calls(served.client, "approve", "reject", "improve_run") == []


@pytest.mark.parametrize(("write", "expected"), [(False, WebClient), (True, WebWriteClient)])
def test_the_production_client_is_the_one_bound_to_the_chosen_principal(write, expected,
                                                                         monkeypatch):
    asked = []

    def fake_client_for(isolated, client_cls):
        asked.append(client_cls)
        return FakeClient()

    monkeypatch.setattr(runtime, "client_for", fake_client_for)
    served = Served(write=write, connections=1, client=None)
    served.ask("GET", "/approvals/7")
    served.finish()
    assert asked == [expected]
