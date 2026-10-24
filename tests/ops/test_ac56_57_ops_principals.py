"""ops-bot / ops-web / ops-notify の scope と ``X-Afx-Asserted-Actor`` の wire 契約。

実 UDS + 実 server thread。期待する scope 集合は spec §3.1 の表をここに書き写したもので、
実装の認可表から作らない (表そのものの誤りを検出するため)。
"""
from __future__ import annotations

import time

import pytest

from agentic_fx.ops import contracts, keys
from agentic_fx.ops.api_server import ROUTES
from agentic_fx.ops.contracts import Principal

from ._api import make_api
from .conftest import digest_of, hold_plugin_lock

OPERATOR, APPROVER = Principal.OPERATOR, Principal.APPROVER
OPS_BOT, OPS_WEB, OPS_NOTIFY = Principal.OPS_BOT, Principal.OPS_WEB, Principal.OPS_NOTIFY

SPEC_SCOPES = {
    "operator": {"status.read", "events.read", "approvals.list", "approvals.detail",
                 "logs.read", "jobs.own", "operate", "guidance.read"},
    "approver": {"status.read", "events.read", "approvals.list", "approvals.detail",
                 "logs.read", "jobs.own", "operate", "decide", "local_guard",
                 "guidance.read"},
    "ops-bot": {"status.read", "events.read", "approvals.list", "approvals.detail",
                "jobs.own", "decide"},
    "ops-web": {"status.read", "events.read", "approvals.list", "approvals.detail",
                "logs.read"},
    "ops-notify": {"events.read"},
}

ACTOR = "discord:123456789012345678"
OTHER_ACTOR = "discord:876543210987654321"
H = "X-Afx-Asserted-Actor"


@pytest.fixture
def api5(ops_env):
    directory = keys.key_dir(ops_env.root)
    keys.initialize(directory, root=ops_env.root)
    for principal in (OPS_BOT, OPS_WEB, OPS_NOTIFY):
        keys.enroll(directory, principal)
    api_env = make_api(ops_env)
    yield api_env
    for client in api_env.clients:
        client.close()
    api_env.server.stop(5.0)


def _wait_job(client, job_id, headers=None, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        view = client.get(f"/v1/jobs/{job_id}", headers=headers or {}).json()["data"]
        if view["state"] not in ("queued", "running"):
            return view
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _raw(api, token: str, actor_line: bytes, path: bytes = b"/v1/whoami") -> tuple[int, bytes]:
    """httpx が送れない値 (非 ASCII・制御文字・裸の LF) を header に載せて送る。"""
    sock = api.raw()
    try:
        sock.settimeout(10)
        sock.sendall(b"GET " + path + b" HTTP/1.1\r\nHost: afx\r\n"
                     b"Authorization: Bearer " + token.encode() + b"\r\n"
                     + actor_line + b"Content-Length: 0\r\n\r\n")
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    status = int(data.split(b" ", 2)[1])
    return status, data.partition(b"\r\n\r\n")[2]


# ---------------------------------------------------------------- AC-56

def test_ac56_authorization_table_matches_the_spec_for_every_principal():
    assert {p.value: {s.value for s in scopes}
            for p, scopes in contracts.PRINCIPAL_SCOPES.items()} == SPEC_SCOPES
    assert {p.value for p in Principal} == set(SPEC_SCOPES)


@pytest.mark.parametrize("principal", [OPS_BOT, OPS_WEB, OPS_NOTIFY])
def test_ac56_every_route_is_allowed_only_by_the_spec_scope(api5, principal):
    env = api5.env
    client = api5.client(principal)
    headers = {H: ACTOR} if principal is OPS_BOT else {}
    allowed = SPEC_SCOPES[principal.value]
    for route in ROUTES:
        path = route.pattern.pattern.strip("^$")
        path = path.replace("(?P<id>[^/]+)", "999999").replace("(?P<order_id>[^/]+)", "999999")
        body = {"payload_sha256": "0" * 64, "expected_generation": 0, "question": "q",
                "idea": "i", "text": "t", "acknowledge": True, "expected_attempts": 0,
                "expected_last_attempt_at": None}
        response = client.request(route.method, path,
                                  json=body if route.method == "POST" else None,
                                  headers={"Idempotency-Key": f"k-{route.handler}", **headers})
        code = response.json().get("error", {}).get("code")
        if route.scope.value in allowed:
            assert code != "forbidden", (principal, route.handler)
            assert response.status_code != 400, (principal, route.handler)
        else:
            assert response.status_code == 403 and code == "forbidden", (principal,
                                                                         route.handler)
    rows = env.conn.execute(
        "SELECT authenticated_principal,endpoint FROM ops_requests "
        "WHERE authenticated_principal NOT IN ('operator','approver')").fetchall()
    # 変更の受理まで進めるのは ops-bot の決定と自分の job の取消だけ
    assert all(r["authenticated_principal"] == "ops-bot" for r in rows)
    assert {r["endpoint"] for r in rows} <= {
        "POST /v1/approvals/{id}/approve", "POST /v1/approvals/{id}/reject",
        "POST /v1/approvals/{id}/retry", "POST /v1/jobs/{id}/cancel"}


def test_ac56_bot_cannot_operate_or_guard_and_web_notify_cannot_change(api5):
    env = api5.env
    approval = env.make_plugin_approval("p1", 1.0)
    bot = api5.client(OPS_BOT)
    web = api5.client(OPS_WEB)
    notify = api5.client(OPS_NOTIFY)
    actor = {H: ACTOR}
    for path, body in (("/v1/policy", {"text": "t"}), ("/v1/backlog", {"idea": "i"}),
                       ("/v1/asks", {"question": "q"}), ("/v1/improve/runs", {}),
                       ("/v1/killswitch/reset", {"expected_generation": 0}),
                       ("/v1/killswitch/reconcile", {}),
                       ("/v1/data/resume", {"acknowledge": True})):
        response = bot.post(path, json=body, headers={"Idempotency-Key": "k", **actor})
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "forbidden"
    digest = {"payload_sha256": digest_of(env, approval)}
    for client in (web, notify):
        for verb in ("approve", "reject", "retry"):
            response = client.post(f"/v1/approvals/{approval}/{verb}", json=digest)
            assert response.status_code == 403
    assert env.approval(approval)["status"] == "pending"
    assert bot.get("/v1/log", headers=actor).status_code == 403
    assert web.get("/v1/log").status_code == 200
    assert notify.get("/v1/status").status_code == 403
    assert notify.get("/v1/approvals").status_code == 403
    assert notify.get("/v1/events").status_code == 200
    assert web.get(f"/v1/approvals/{approval}").status_code == 200
    who = web.get("/v1/whoami").json()["data"]
    assert who == {"authenticated_principal": "ops-web", "asserted_actor": None,
                   "scopes": sorted(SPEC_SCOPES["ops-web"])}


@pytest.mark.parametrize("principal", list(Principal))
def test_ac56_autopilot_on_orders_and_stop_stay_unreachable(api5, principal):
    client = api5.client(principal)
    headers = {H: ACTOR} if principal is OPS_BOT else {}
    for path in ("/v1/autopilot", "/v1/autopilot/on", "/v1/orders", "/v1/orders/1/close",
                 "/v1/stop", "/v1/mode", "/v1/shutdown", "/v1/killswitch/disable"):
        response = client.post(path, json={"on": True}, headers=headers)
        assert response.status_code == 404, (principal, path)
        assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------- AC-57

@pytest.mark.parametrize("value", [
    "discord:12345678901234567", "discord:12345678901234567890", "discord:00000000000000000"])
def test_ac57_valid_actor_is_accepted_and_shown_for_ops_bot(api5, value):
    bot = api5.client(OPS_BOT)
    response = bot.get("/v1/whoami", headers={H: value})
    assert response.status_code == 200
    assert response.json()["data"]["asserted_actor"] == value
    assert response.json()["data"]["authenticated_principal"] == "ops-bot"
    assert bot.get("/v1/whoami").json()["data"]["asserted_actor"] is None
    # header 名は大文字小文字を問わない
    lower = bot.get("/v1/whoami", headers={H.lower(): value})
    assert lower.json()["data"]["asserted_actor"] == value


@pytest.mark.parametrize("value", [
    "", "discord:", "discord:1234567890123456", "discord:123456789012345678901",
    "Discord:123456789012345678", "DISCORD:123456789012345678", "discord123456789012345678",
    "discord:1234567890 12345678", "discord:1234567890\t12345678", "discord: 123456789012345678",
    "discord:+23456789012345678", "discord:-23456789012345678", "discord:12345678901234567a",
    "slack:123456789012345678", "discord:123456789012345678,discord:123456789012345678",
])
def test_ac57_malformed_actor_is_400_for_ops_bot(api5, value):
    bot = api5.client(OPS_BOT)
    response = bot.get("/v1/whoami", headers={H: value})
    assert response.status_code == 400, value
    assert response.json()["error"]["code"] == "invalid_argument"


@pytest.mark.parametrize("raw", [
    b"discord:12345678901234567\x01", b"discord:\x0012345678901234567",
    b"discord:12345678901234567\x7f", "discord:１２３４５６７８９０１２３４５６７".encode(),
    b"discord:1234567890123456\xff7", b"discord:12345678901234567\nX-Other: 1",
    b"discord:1234567890\r12345678", b"   ",
])
def test_ac57_control_non_ascii_and_line_breaks_are_400(api5, raw):
    token = api5.token(OPS_BOT)
    status, body = _raw(api5, token, b"X-Afx-Asserted-Actor: " + raw + b"\r\n")
    assert status == 400, raw
    assert b"invalid_argument" in body
    assert b"discord" not in body


def test_ac57_duplicate_actor_headers_are_400(api5):
    token = api5.token(OPS_BOT)
    line = f"{H}: {ACTOR}\r\n".encode()
    status, _ = _raw(api5, token, line + line.replace(b"X-Afx", b"x-afx"))
    assert status == 400


@pytest.mark.parametrize("principal", [OPERATOR, APPROVER, OPS_WEB, OPS_NOTIFY])
def test_ac57_actor_header_from_other_principals_is_400(api5, principal):
    env = api5.env
    approval = env.make_plugin_approval("p1", 1.0)
    client = api5.client(principal)
    # 認証後・routing 前に検査するので、scope の無い endpoint や無い path でも 400
    for method, path in (("GET", "/v1/whoami"), ("GET", "/v1/events"),
                         ("POST", f"/v1/approvals/{approval}/reject"),
                         ("GET", "/v1/no-such-endpoint")):
        response = client.request(method, path, json={} if method == "POST" else None,
                                  headers={H: ACTOR})
        assert response.status_code == 400, (principal, path)
        assert response.json()["error"]["code"] == "invalid_argument"
    assert env.approval(approval)["status"] == "pending"
    assert env.conn.execute("SELECT COUNT(*) FROM ops_requests").fetchone()[0] == 0


def test_ac57_unauthenticated_request_with_actor_is_401_not_400(api5):
    client = api5.client(token="0" * 64)
    assert client.get("/v1/whoami", headers={H: ACTOR}).status_code == 401
    assert client.get("/v1/whoami", headers={H: "bad"}).status_code == 401


@pytest.mark.parametrize("verb", ["approve", "reject", "retry"])
def test_ac57_bot_decision_without_actor_is_400_and_records_nothing(api5, verb):
    env = api5.env
    approval = env.make_plugin_approval("p1", 1.0)
    bot = api5.client(OPS_BOT)
    response = bot.post(f"/v1/approvals/{approval}/{verb}",
                        json={"payload_sha256": digest_of(env, approval)})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_argument"
    assert env.approval(approval)["status"] == "pending"
    assert env.conn.execute("SELECT COUNT(*) FROM ops_requests").fetchone()[0] == 0
    assert bot.get("/v1/jobs/0123456789abcdef").status_code == 400
    assert bot.post("/v1/jobs/0123456789abcdef/cancel").status_code == 400


def test_ac57_bot_approve_records_actor_and_polling_needs_the_same_actor(api5):
    env = api5.env
    approval = env.make_plugin_approval("p1", 1.0)
    bot = api5.client(OPS_BOT)
    approver = api5.client(APPROVER)
    reply = bot.post(f"/v1/approvals/{approval}/approve",
                     json={"payload_sha256": digest_of(env, approval),
                           "decided_by": "discord:999", "asserted_actor": OTHER_ACTOR},
                     headers={H: ACTOR})
    assert reply.status_code == 202, reply.text
    job_id = reply.json()["data"]["job_id"]
    view = _wait_job(bot, job_id, headers={H: ACTOR})
    assert view["state"] == "done"
    assert env.approval(approval)["status"] == "approved"
    assert env.approval(approval)["decided_by"] == "api:ops-bot"
    # 有効だが別の actor は 403、header 無しは 400、別 principal には見えない (404)
    other = bot.get(f"/v1/jobs/{job_id}", headers={H: OTHER_ACTOR})
    assert other.status_code == 403 and other.json()["error"]["code"] == "forbidden"
    assert bot.get(f"/v1/jobs/{job_id}").status_code == 400
    assert approver.get(f"/v1/jobs/{job_id}").status_code == 404
    cancel = bot.post(f"/v1/jobs/{job_id}/cancel", headers={H: OTHER_ACTOR})
    assert cancel.status_code == 403
    rows = env.conn.execute(
        "SELECT authenticated_principal,asserted_actor,endpoint,phase,job_id "
        "FROM ops_requests ORDER BY id").fetchall()
    decision = [r for r in rows if r["endpoint"] == "POST /v1/approvals/{id}/approve"]
    assert all(r["authenticated_principal"] == "ops-bot" for r in decision)
    # 申告主体は受理行に記録する (終端行は target_ref で受理行を指す)
    accepted = [r for r in decision if r["phase"] == "accepted"]
    assert len(accepted) == 1 and accepted[0]["asserted_actor"] == ACTOR
    assert accepted[0]["job_id"] == job_id
    assert {r["phase"] for r in decision} == {"accepted", "succeeded"}
    service_job = api5.service.jobs.lookup(job_id)
    assert (service_job.authenticated_principal, service_job.asserted_actor) == (OPS_BOT, ACTOR)
    assert OTHER_ACTOR not in str([tuple(r) for r in rows])


def test_ac57_bot_queued_cancel_requires_the_same_actor(api5):
    env = api5.env
    blocker = env.make_plugin_approval()
    queued_approval = env.make_plugin_approval(name="ema", mission_id=2)
    bot = api5.client(OPS_BOT)
    release = hold_plugin_lock(env.plugins_root, "sma")
    try:
        running = bot.post(f"/v1/approvals/{blocker}/reject", json={},
                           headers={H: ACTOR}).json()["data"]
        for _ in range(500):
            job = api5.service.jobs.lookup(running["job_id"])
            if job.state.value == "running":
                break
            time.sleep(0.01)
        queued = bot.post(f"/v1/approvals/{queued_approval}/reject", json={},
                          headers={H: ACTOR}).json()["data"]
        assert queued["state"] == "queued"
        refused = bot.post(f"/v1/jobs/{queued['job_id']}/cancel", headers={H: OTHER_ACTOR})
        assert refused.status_code == 403
        assert api5.service.jobs.lookup(queued["job_id"]).state.value == "queued"
        done = bot.post(f"/v1/jobs/{queued['job_id']}/cancel", headers={H: ACTOR})
        assert done.status_code == 200 and done.json()["data"]["state"] == "cancelled"
    finally:
        release()
    assert _wait_job(bot, running["job_id"], headers={H: ACTOR})["state"] == "done"
    assert env.approval(queued_approval)["status"] == "pending"
    cancels = env.conn.execute(
        "SELECT asserted_actor FROM ops_requests WHERE endpoint=? AND phase='accepted'",
        ("POST /v1/jobs/{id}/cancel",)).fetchall()
    # 403 の取消は受理前に断るので、受理行は同じ actor の 1 件だけ
    assert [r["asserted_actor"] for r in cancels] == [ACTOR]


def test_ac57_cli_principals_record_null_actor(api5):
    env = api5.env
    approval = env.make_plugin_approval("p1", 1.0)
    approver = api5.client(APPROVER)
    reply = approver.post(f"/v1/approvals/{approval}/reject", json={})
    assert reply.status_code == 202
    assert _wait_job(approver, reply.json()["data"]["job_id"])["state"] == "done"
    rows = env.conn.execute("SELECT asserted_actor FROM ops_requests").fetchall()
    assert rows and all(r["asserted_actor"] is None for r in rows)
    assert env.approval(approval)["decided_by"] == "api:approver"


def test_ac57_terminal_row_carries_the_same_actor_as_the_accepted_row(api5):
    env = api5.env
    approval = env.make_plugin_approval("p1", 1.0)
    bot = api5.client(OPS_BOT)
    reply = bot.post(f"/v1/approvals/{approval}/reject", json={}, headers={H: ACTOR})
    assert reply.status_code == 202
    assert _wait_job(bot, reply.json()["data"]["job_id"], headers={H: ACTOR})["state"] == "done"
    rows = env.conn.execute(
        "SELECT phase,asserted_actor FROM ops_requests "
        "WHERE endpoint='POST /v1/approvals/{id}/reject'").fetchall()
    assert {r["phase"] for r in rows} == {"accepted", "succeeded"}
    assert [r["asserted_actor"] for r in rows] == [ACTOR, ACTOR]
