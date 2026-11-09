"""書込みが有効な web の principal (ops-web-write) の認可・actor・監査・job 所有。

期待する許可集合と actor の規則は、実装の表から作らず、ここに独立に書き写す。
"""
from __future__ import annotations

import threading

import pytest

from agentic_fx.ops import ErrorCode, OpsError, contracts
from agentic_fx.ops.api_server import ROUTES
from agentic_fx.ops.contracts import Principal, ShellCaller

from .conftest import APPROVER, OPERATOR, digest_of
from .test_ac56_57_ops_principals import _raw, _wait_job

OPS_WEB_WRITE = Principal.OPS_WEB_WRITE
H = "X-Afx-Asserted-Actor"
ALICE = "web:alice"
BOB = "web:bob"

# ops-web-write が呼べる endpoint (handler 名)。ここに無いものはすべて 403。
READS = {"status", "log", "activity", "approval_detail", "approval_change", "approval_list",
         "plugin_list", "plugin_detail", "improve_run", "improve_runs", "reflection_status",
         "whoami", "job_get", "events"}
WRITES = {"approve", "reject", "retry", "improve", "job_cancel"}
ALLOWED = READS | WRITES


def _path(route) -> str:
    path = route.pattern.pattern.strip("^$")
    path = path.replace("(?P<id>[^/]+)", "999999").replace("(?P<order_id>[^/]+)", "999999")
    return path.replace("(?P<name>[^/]+)", "p1")


def test_every_route_is_allowed_to_ops_web_write_exactly_as_designed(api5):
    client = api5.client(OPS_WEB_WRITE)
    body = {"payload_sha256": "0" * 64, "expected_generation": 0, "question": "q",
            "idea": "i", "text": "t", "acknowledge": True, "expected_attempts": 0,
            "expected_last_attempt_at": None}
    seen = set()
    for route in ROUTES:
        seen.add(route.handler)
        # improve だけは本当に起動させない: 本文の形を壊して受理前に落とす。
        payload = {"zzz": 1} if route.handler == "improve" else body
        response = client.request(route.method, _path(route),
                                  json=payload if route.method == "POST" else None,
                                  headers={"Idempotency-Key": f"k-{route.handler}", H: ALICE})
        code = response.json().get("error", {}).get("code")
        if route.handler in ALLOWED:
            assert response.status_code != 403, route.handler
        else:
            assert response.status_code == 403 and code == "forbidden", route.handler
    assert seen >= ALLOWED
    assert len(ROUTES) == 33


def test_ops_web_write_cannot_reach_ask_backlog_policy_or_the_local_guard(api5, ops_env):
    client = api5.client(OPS_WEB_WRITE)
    for path, body in (("/v1/policy", {"text": "t"}), ("/v1/backlog", {"idea": "i"}),
                       ("/v1/asks", {"question": "q"}),
                       ("/v1/killswitch/reset", {"expected_generation": 0}),
                       ("/v1/killswitch/reconcile", {}),
                       ("/v1/data/resume", {"acknowledge": True}),
                       ("/v1/backlog/1/reject", {}), ("/v1/policy/1/remove", {}),
                       ("/v1/policy/reconcile", {}),
                       ("/v1/reflections/1/retry", {"expected_attempts": 0,
                                                    "expected_last_attempt_at": None})):
        response = client.post(path, json=body, headers={"Idempotency-Key": "k", H: ALICE})
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "forbidden"
    assert client.get("/v1/backlog", headers={H: ALICE}).status_code == 403
    assert client.get("/v1/policy", headers={H: ALICE}).status_code == 403
    assert ops_env.ops_rows() == []


def test_improve_run_scope_opens_only_the_improve_start():
    holders = {code for code, scopes in
               ((p.value, s) for p, s in contracts.PRINCIPAL_SCOPES.items())
               if "improve.run" in {x.value for x in scopes}}
    assert holders == {"operator", "approver", "ops-web-write"}
    from agentic_fx.ops.service import ENDPOINTS
    assert [c for c, e in ENDPOINTS.items() if e.scope is contracts.Scope.IMPROVE_RUN] == [
        "improve"]


# ---------------------------------------------------------------- actor の規則

@pytest.mark.parametrize("value", [
    "web:a", "web:alice", "web:a-b_c9", "web:" + "a" * 32])
def test_valid_web_actor_is_accepted_for_ops_web_write_only(api5, value):
    response = api5.client(OPS_WEB_WRITE).get("/v1/whoami", headers={H: value})
    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["authenticated_principal"], data["asserted_actor"]) == ("ops-web-write", value)
    assert len(value.encode()) <= 36


@pytest.mark.parametrize("value", [
    "", "web:", "web:A", "web:Alice", "web:1abc", "web:-a", "web:_a", "web:a b", "web:a\tb",
    "web:" + "a" * 33, "Web:alice", "WEB:alice", "web alice", "web:alice,web:bob",
    "discord:123456789012345678", "slack:alice", "web:alice:x", "web:a.b", "web:a@b",
])
def test_malformed_web_actor_is_400_for_ops_web_write(api5, value):
    response = api5.client(OPS_WEB_WRITE).get("/v1/whoami", headers={H: value})
    assert response.status_code == 400, value
    assert response.json()["error"]["code"] == "invalid_argument"


@pytest.mark.parametrize("raw", [
    b"web:alice\x01", b"web:\x00alice", b"web:alic\xff", b"web:alice\nX-Other: 1",
    b"web:ali\rce", "web:ａ".encode(), b"web:" + b"a" * 32 + b"\x7f"])
def test_control_and_non_ascii_web_actor_is_400(api5, raw):
    status, body = _raw(api5, api5.token(OPS_WEB_WRITE),
                        b"X-Afx-Asserted-Actor: " + raw + b"\r\n")
    assert status == 400, raw
    assert b"invalid_argument" in body


def test_the_two_actor_forms_are_not_interchangeable(api5):
    bot, web = api5.client(Principal.OPS_BOT), api5.client(OPS_WEB_WRITE)
    assert bot.get("/v1/whoami", headers={H: ALICE}).status_code == 400
    assert web.get("/v1/whoami", headers={H: "discord:123456789012345678"}).status_code == 400
    assert bot.get("/v1/whoami", headers={H: "discord:123456789012345678"}).status_code == 200


@pytest.mark.parametrize("principal", [Principal.OPERATOR, Principal.APPROVER,
                                       Principal.OPS_WEB, Principal.OPS_NOTIFY])
def test_web_actor_from_principals_outside_the_table_is_400(api5, principal):
    response = api5.client(principal).get("/v1/whoami", headers={H: ALICE})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_argument"


def test_the_actor_table_holds_exactly_the_two_principals_with_their_limits():
    assert set(contracts.ACTOR_RULES) == {Principal.OPS_BOT, Principal.OPS_WEB_WRITE}
    assert contracts.ACTOR_PRINCIPALS == frozenset(contracts.ACTOR_RULES)
    assert contracts.ACTOR_RULES[Principal.OPS_BOT].max_bytes == 28
    assert contracts.ACTOR_RULES[Principal.OPS_WEB_WRITE].max_bytes == 36
    assert contracts.validate_asserted_actor(OPS_WEB_WRITE, None) is None
    for caller in (OPERATOR, APPROVER, ShellCaller.SHELL, Principal.OPS_WEB,
                   Principal.OPS_NOTIFY):
        with pytest.raises(OpsError):
            contracts.validate_asserted_actor(caller, ALICE)


@pytest.mark.parametrize("call", ["get_job", "cancel_job", "approve", "reject", "improve"])
def test_a_missing_actor_is_refused_for_every_actor_required_call(ops_env, call):
    service = ops_env.service(improve_supervisor=_Improve())
    approval = ops_env.make_plugin_approval("p1", 1.0)
    calls = {
        "get_job": lambda: service.get_job(OPS_WEB_WRITE, "0" * 32),
        "cancel_job": lambda: service.cancel_job(OPS_WEB_WRITE, "0" * 32),
        "approve": lambda: service.approve(OPS_WEB_WRITE, approval, digest_of(ops_env, approval)),
        "reject": lambda: service.reject(OPS_WEB_WRITE, approval),
        "improve": lambda: service.improve(OPS_WEB_WRITE, "k"),
    }
    with pytest.raises(OpsError) as raised:
        calls[call]()
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert ops_env.ops_rows() == []
    assert ops_env.approval(approval)["status"] == "pending"


# ---------------------------------------------------------------- 決定の監査

def test_a_decision_records_the_principal_in_decided_by_and_the_actor_apart(api5):
    env = api5.env
    approval = env.make_plugin_approval("p1", 1.0)
    web = api5.client(OPS_WEB_WRITE)
    reply = web.post(f"/v1/approvals/{approval}/approve",
                     json={"payload_sha256": digest_of(env, approval),
                           "decided_by": "web:mallory", "asserted_actor": BOB},
                     headers={H: ALICE})
    assert reply.status_code == 202, reply.text
    job_id = reply.json()["data"]["job_id"]
    assert _wait_job(web, job_id, headers={H: ALICE})["state"] == "done"
    assert env.approval(approval)["status"] == "approved"
    assert env.approval(approval)["decided_by"] == "api:ops-web-write"
    rows = env.conn.execute(
        "SELECT authenticated_principal,asserted_actor,phase FROM ops_requests "
        "WHERE endpoint='POST /v1/approvals/{id}/approve'").fetchall()
    assert {r["phase"] for r in rows} == {"accepted", "succeeded"}
    assert all((r["authenticated_principal"], r["asserted_actor"]) == ("ops-web-write", ALICE)
               for r in rows)
    assert "mallory" not in str([tuple(r) for r in rows]) and BOB not in str(
        [tuple(r) for r in rows])


def test_decisions_still_need_a_pidfd_verified_peer(ops_env):
    from agentic_fx.ops import api_server
    assert api_server._PIDFD_REQUIRED == frozenset({contracts.Scope.DECIDE,
                                                    contracts.Scope.LOCAL_GUARD})


# ---------------------------------------------------------------- 改善の開始

class _Improve:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def submit_manual(self, *, on_prepared=None):
        self.calls += 1
        if on_prepared is not None:
            on_prepared(17)
        self.started.set()
        self.release.wait(10)
        return 17


def _post_improve(client, actor=ALICE, key="k1", body=None):
    headers = {"Idempotency-Key": key}
    if actor is not None:
        headers[H] = actor
    return client.post("/v1/improve/runs", json={} if body is None else body, headers=headers)


@pytest.fixture
def improve_api(ops_env):
    from ._api import make_api
    from agentic_fx.ops import keys
    directory = keys.key_dir(ops_env.root)
    keys.initialize(directory, root=ops_env.root)
    keys.enroll(directory, OPS_WEB_WRITE)
    sup = _Improve()
    api_env = make_api(ops_env, service_kwargs={"improve_supervisor": sup})
    api_env.sup = sup
    yield api_env
    sup.release.set()
    for client in api_env.clients:
        client.close()
    api_env.server.stop(5.0)


def test_improve_start_stores_the_actor_on_the_job_and_the_audit_row(improve_api, ops_env):
    web = improve_api.client(OPS_WEB_WRITE)
    reply = _post_improve(web)
    assert reply.status_code == 202, reply.text
    job_id = reply.json()["data"]["job_id"]
    assert improve_api.sup.started.wait(5)
    job = improve_api.service.jobs.lookup(job_id)
    assert (job.authenticated_principal, job.asserted_actor) == (OPS_WEB_WRITE, ALICE)
    rows = ops_env.conn.execute(
        "SELECT authenticated_principal,asserted_actor,phase FROM ops_requests "
        "WHERE endpoint='POST /v1/improve/runs'").fetchall()
    assert [(r["authenticated_principal"], r["asserted_actor"]) for r in rows
            if r["phase"] == "accepted"] == [("ops-web-write", ALICE)]


def test_improve_job_is_visible_and_cancellable_only_by_the_same_principal_and_actor(
        improve_api):
    web = improve_api.client(OPS_WEB_WRITE)
    job_id = _post_improve(web).json()["data"]["job_id"]
    assert improve_api.sup.started.wait(5)
    assert web.get(f"/v1/jobs/{job_id}", headers={H: ALICE}).status_code == 200
    other = web.get(f"/v1/jobs/{job_id}", headers={H: BOB})
    assert other.status_code == 403 and other.json()["error"]["code"] == "forbidden"
    assert web.get(f"/v1/jobs/{job_id}").status_code == 400
    assert improve_api.client(Principal.OPERATOR).get(f"/v1/jobs/{job_id}").status_code == 404
    assert improve_api.client(Principal.APPROVER).get(f"/v1/jobs/{job_id}").status_code == 404
    refused = web.post(f"/v1/jobs/{job_id}/cancel", headers={H: BOB})
    assert refused.status_code == 403
    improve_api.sup.release.set()
    assert _wait_job(web, job_id, headers={H: ALICE})["state"] == "done"


def test_the_operator_still_starts_an_improve_without_an_actor(improve_api, ops_env):
    reply = _post_improve(improve_api.client(Principal.OPERATOR), actor=None)
    assert reply.status_code == 202, reply.text
    job = improve_api.service.jobs.lookup(reply.json()["data"]["job_id"])
    assert (job.authenticated_principal, job.asserted_actor) == (Principal.OPERATOR, None)
    assert _post_improve(improve_api.client(Principal.APPROVER), actor=None,
                         key="k2").status_code == 409


def test_improve_without_an_actor_or_with_a_forged_body_actor_is_400_and_records_nothing(
        improve_api, ops_env):
    web = improve_api.client(OPS_WEB_WRITE)
    assert _post_improve(web, actor=None).status_code == 400
    forged = _post_improve(web, actor=ALICE, body={"asserted_actor": BOB})
    assert forged.status_code == 400
    assert forged.json()["error"]["code"] == "invalid_argument"
    assert ops_env.ops_rows() == []
    assert improve_api.sup.calls == 0


def test_a_second_improve_while_one_runs_is_improve_running(improve_api):
    web = improve_api.client(OPS_WEB_WRITE)
    assert _post_improve(web, key="k1").status_code == 202
    assert improve_api.sup.started.wait(5)
    second = _post_improve(web, key="k2")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "improve_running"
    assert improve_api.sup.calls == 1


def test_the_same_idempotency_key_returns_the_same_job(improve_api):
    web = improve_api.client(OPS_WEB_WRITE)
    first = _post_improve(web, key="afxweb-same")
    assert first.status_code == 202
    assert improve_api.sup.started.wait(5)
    again = _post_improve(web, key="afxweb-same")
    assert again.status_code == 202, again.text
    assert again.json()["data"]["job_id"] == first.json()["data"]["job_id"]
    assert improve_api.sup.calls == 1
    improve_api.sup.release.set()
    done = _post_improve(web, key="afxweb-same")
    assert done.json()["data"]["job_id"] == first.json()["data"]["job_id"]
    assert improve_api.sup.calls == 1


def test_improve_is_refused_under_autopilot_for_ops_web_write(improve_api, ops_env):
    ops_env.state.update(kill_switch_latched=True)
    ops_env.state.update(autopilot=True)
    response = _post_improve(improve_api.client(OPS_WEB_WRITE))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "autopilot_restricted"
    assert improve_api.sup.calls == 0


def test_a_repeat_improve_job_carries_the_actor_too(ops_env):
    from agentic_fx.core.improve_supervisor import RepeatResult
    from agentic_fx.ops import keys
    from ._api import make_api
    from ._improve_repeat_fake import FakeSupervisor
    directory = keys.key_dir(ops_env.root)
    keys.initialize(directory, root=ops_env.root)
    keys.enroll(directory, OPS_WEB_WRITE)
    gate = threading.Event()
    sup = FakeSupervisor(lambda **kw: (gate.wait(10), RepeatResult("max_runs", 1, 3))[1])
    api_env = make_api(ops_env, service_kwargs={"improve_supervisor": sup})
    try:
        web = api_env.client(OPS_WEB_WRITE)
        reply = _post_improve(web, body={"max_runs": 1})
        assert reply.status_code == 202, reply.text
        job_id = reply.json()["data"]["job_id"]
        job = api_env.service.jobs.lookup(job_id)
        assert (job.authenticated_principal, job.asserted_actor) == (OPS_WEB_WRITE, ALICE)
        assert web.get(f"/v1/jobs/{job_id}", headers={H: BOB}).status_code == 403
        assert web.post(f"/v1/jobs/{job_id}/cancel", headers={H: BOB}).status_code == 403
    finally:
        gate.set()
        for client in api_env.clients:
            client.close()
        api_env.server.stop(5.0)
