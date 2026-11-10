"""書込みの adapter 試験の共通部品: 呼出しを数える fake の client と、要求の組み立て。"""
from __future__ import annotations

import base64
import re
from urllib.parse import urlencode

from afx_ops.api_client import ApiError, Reply
from afx_ops.web import auth
from afx_ops.web.request import HostPolicy, Request, handle_request
from afx_ops.web.write import ConfirmationStore, WriteContext

PASSWORD = b"correct-horse-battery-1"
PORT = 8788
PEER = "127.0.0.1"
DIGEST = "ab" * 32
OTHER_DIGEST = "cd" * 32
JOB = "0123456789abcdef" * 2
BASIC = "Basic " + base64.b64encode(b"afx:" + PASSWORD).decode("ascii")
FORM = "application/x-www-form-urlencoded"
TOKEN_FIELD = re.compile(r'name="token" value="([A-Za-z0-9_-]{43})"')


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeClient:
    """daemon の fake。どの呼出しも `calls` に残る (読取りも含む)。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.detail = Reply(200, {"id": 7, "kind": "plugin", "status": "pending",
                                  "payload_sha256": DIGEST}, None)
        self.approve_reply: object = Reply(200, {"job_id": JOB}, None)
        self.reject_reply: object = Reply(200, {"job_id": JOB}, None)
        self.improve_reply: object = Reply(200, {"job_id": JOB}, None)
        self.job_reply: object = Reply(200, {"id": JOB, "kind": "improve", "state": "running",
                                             "result": {}, "result_code": None}, None)
        self.cancel_reply: object = Reply(200, {"id": JOB, "state": "running",
                                                "stop_requested": True}, None)
        self.fail: dict[str, BaseException] = {}

    def _go(self, name: str, *args, reply):
        self.calls.append((name, *args))
        failure = self.fail.get(name)
        if failure is not None:
            raise failure
        return reply

    def approval(self, approval_id):
        return self._go("approval", approval_id, reply=self.detail)

    def approve(self, actor, approval_id, *, payload_sha256):
        return self._go("approve", actor, approval_id, payload_sha256, reply=self.approve_reply)

    def reject(self, actor, approval_id, *, reason):
        return self._go("reject", actor, approval_id, reason, reply=self.reject_reply)

    def improve_run(self, actor, body, *, idempotency_key):
        return self._go("improve_run", actor, dict(body), idempotency_key,
                        reply=self.improve_reply)

    def job(self, actor, job_id):
        return self._go("job", actor, job_id, reply=self.job_reply)

    def cancel_job(self, actor, job_id):
        return self._go("cancel_job", actor, job_id, reply=self.cancel_reply)

    # 読取りの adapter が使う口 (書込みの試験では使わない)
    def status(self):
        return self._go("status", reply=Reply(200, {"mode": "paper"}, None))

    def approvals(self, *, limit=20):
        return self._go("approvals", reply=Reply(200, {"approvals": []}, None))

    def approval_change(self, approval_id):
        return self._go("approval_change", approval_id, reply=Reply(404, None, "not_found"))


class Rig:
    """書込みを有効にした adapter 1 式。"""

    def __init__(self, *, loopback: bool = True, tls: bool = False, port: int = PORT,
                 bind: str = "127.0.0.1") -> None:
        self.clock = Clock()
        self.client = FakeClient()
        self.store = ConfirmationStore(self.clock)
        self.port = port
        self.context = WriteContext(user="afx", port=port, client=self.client, store=self.store,
                                    loopback=loopback, tls=tls)
        self.host_policy = HostPolicy(bind, port, True)
        self.authenticator = auth.BasicAuthenticator(PASSWORD, clock=self.clock)

    def host(self) -> str:
        return "127.0.0.1" if self.port == 80 else f"127.0.0.1:{self.port}"

    def origin(self) -> str:
        return "http://" + self.host()

    def request(self, method: str, path: str, form: dict | bytes | None = None, *,
                authorization: str | None = BASIC, origin: str | None = "default",
                host: str | None = "default", content_type: str | None = FORM,
                extra: tuple = ()) -> Request:
        headers = []
        headers.append(("Host", self.host() if host == "default" else host))
        if authorization is not None:
            headers.append(("Authorization", authorization))
        if origin is not None:
            headers.append(("Origin", self.origin() if origin == "default" else origin))
        body = b""
        if form is not None:
            body = form if isinstance(form, bytes) else urlencode(form).encode("ascii")
        if content_type is not None and method == "POST":
            headers.append(("Content-Type", content_type))
        return Request(method, path, {}, tuple(headers) + tuple(extra), body)

    def send(self, request: Request, *, writable: bool = True):
        return handle_request(request, self.client, host_policy=self.host_policy,
                              authenticator=self.authenticator, peer=PEER,
                              write_context=self.context if writable else None)

    def post(self, path: str, form: dict | bytes | None = None, **kwargs):
        return self.send(self.request("POST", path, form, **kwargs))

    def first_post(self, action: str = "approve", approval_id: int = 7, **fields) -> str:
        """1 回目の POST を通し、確認画面の token を返す。"""
        form = {"payload_sha256": DIGEST, **fields}
        response = self.post(f"/approvals/{approval_id}/{action}", form)
        assert response.status == 200, response.status
        return token_of(response)

    def first_improve(self, **fields) -> str:
        response = self.post("/improve/runs", fields)
        assert response.status == 200, response.status
        return token_of(response)

    def confirm(self, token: str, path: str = "/confirm", **kwargs):
        return self.post(path, {"token": token}, **kwargs)


def token_of(response) -> str:
    found = TOKEN_FIELD.findall(response.body.decode("utf-8"))
    assert len(found) == 1
    return found[0]


def api_error(reason: str = "connect_failed", *, token_sent: bool = False) -> ApiError:
    return ApiError(reason, token_sent=token_sent)
