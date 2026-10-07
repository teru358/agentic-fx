"""AC-1: 24 endpoint の全数実行。AC-2: 認証と principal。AC-43: scope の総当たり。"""
from __future__ import annotations

import time

import pytest

from agentic_fx.ops import contracts
from agentic_fx.ops.api_server import ROUTES, _match
from agentic_fx.ops.contracts import Principal, Scope

from ._api import api, backlog_row, closed_order  # noqa: F401
from .conftest import digest_of


def _wait_job(client, job_id, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        view = client.get(f"/v1/jobs/{job_id}").json()["data"]
        if view["state"] not in ("queued", "running"):
            return view
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _calls(api, env):
    """endpoint ごとに (route handler 名, method, path, json, headers, 期待 status)。"""
    a1 = env.make_plugin_approval("p1", 1.0, mission_id=1)
    a2 = env.make_plugin_approval("p2", 2.0, mission_id=2)
    a3 = env.make_plugin_approval("p3", 3.0, mission_id=3)
    order_id = closed_order(env)
    open_backlog = backlog_row(env)
    note_backlog = backlog_row(env)
    rejected_backlog = backlog_row(env, "rejected")
    key = {"Idempotency-Key": "k-1"}
    return [
        ("status", "GET", "/v1/status", None, {}, 200),
        ("log", "GET", "/v1/log?n=5", None, {}, 200),
        ("activity", "GET", "/v1/activity?n=5", None, {}, 200),
        ("approval_list", "GET", "/v1/approvals?status=pending&limit=5", None, {}, 200),
        ("approval_detail", "GET", f"/v1/approvals/{a1}", None, {}, 200),
        ("reflection_status", "GET", f"/v1/reflections/{order_id}", None, {}, 200),
        ("whoami", "GET", "/v1/whoami", None, {}, 200),
        ("events", "GET", "/v1/events?after=0&limit=10", None, {}, 200),
        ("ask", "POST", "/v1/asks", {"question": "q"}, key, 202),
        ("job_get", "GET", "/v1/jobs/{ask_job}", None, {}, 200),
        ("job_cancel", "POST", "/v1/jobs/{ask_job}/cancel", {}, {}, 200),
        ("data_resume", "POST", "/v1/data/resume", {"acknowledge": True}, {}, 200),
        ("approve", "POST", f"/v1/approvals/{a1}/approve",
         {"payload_sha256": digest_of(env, a1)}, {}, 202),
        ("reject", "POST", f"/v1/approvals/{a2}/reject", {"reason": "no"}, {}, 202),
        ("retry", "POST", f"/v1/approvals/{a3}/retry",
         {"payload_sha256": digest_of(env, a3)}, {}, 202),
        ("killswitch_reset", "POST", "/v1/killswitch/reset", {"expected_generation": 0}, {},
         409),
        ("killswitch_reconcile", "POST", "/v1/killswitch/reconcile", {}, {}, 409),
        ("reflection_retry", "POST", f"/v1/reflections/{order_id}/retry",
         {"expected_attempts": 0, "expected_last_attempt_at": None}, key, 200),
        ("improve", "POST", "/v1/improve/runs", {}, key, 202),
        ("backlog_add", "POST", "/v1/backlog", {"idea": "i"}, key, 200),
        ("backlog_reject", "POST", f"/v1/backlog/{open_backlog}/reject", {}, {}, 200),
        ("backlog_reopen", "POST", f"/v1/backlog/{rejected_backlog}/reopen", {}, {}, 200),
        ("backlog_note", "POST", f"/v1/backlog/{note_backlog}/note", {}, {}, 200),
        ("policy", "POST", "/v1/policy", {"text": "be careful"}, key, 200),
    ]


def test_ac1_all_24_endpoints_over_real_uds(api):
    from agentic_fx.ops.api_server import _match
    client = api.client(Principal.APPROVER)
    seen = set()
    ask_job = None
    jobs = []
    for name, method, path, body, headers, expected in _calls(api, api.env):
        path = path.replace("{ask_job}", ask_job or "x")
        response = client.request(method, path, json=body, headers=headers)
        assert response.status_code == expected, (name, response.text)
        payload = response.json()
        assert payload["ok"] is (expected < 300)
        if expected >= 300:
            assert set(payload["error"]) == {"code", "message"}
        route, _ = _match(method, path.split("?")[0], "uds")
        assert route.handler == name
        seen.add(route)
        if name == "ask":
            ask_job = payload["data"]["job_id"]
            _wait_job(client, ask_job)
        if expected == 202:
            jobs.append(payload["data"]["job_id"])
    assert len(ROUTES) == 24 and seen == set(ROUTES)
    for job_id in jobs:
        assert _wait_job(client, job_id)["state"] in ("done", "failed")
    codes = {client.post("/v1/killswitch/reset", json={"expected_generation": 0})
             .json()["error"]["code"],
             client.post("/v1/killswitch/reconcile", json={}).json()["error"]["code"]}
    assert codes == {"not_latched", "invalid_state"}


def test_ac1_unknown_path_and_method_are_404(api):
    client = api.client(Principal.APPROVER)
    assert client.get("/v1/nothing").status_code == 404
    assert client.get("/v1/policy").status_code == 404
    assert client.post("/v1/status", json={}).status_code == 404
    assert client.get("/v1/approvals/abc").status_code == 400


@pytest.mark.parametrize("path", ["/v1/data/resume", "/v1/killswitch/reset",
                                  "/v1/killswitch/reconcile"])
def test_ac1_local_guard_routes_do_not_match_tcp(path):
    assert _match("POST", path, "tcp") is None
    assert _match("POST", path, "uds") is not None


# ---------------------------------------------------------------- AC-2

def test_ac2_missing_or_wrong_key_is_401(api):
    assert api.client().get("/v1/status").status_code == 401
    wrong = api.client(token="0" * 64).get("/v1/status")
    assert wrong.status_code == 401
    assert wrong.json() == {"ok": False, "error": {"code": "unauthenticated",
                                                   "message": "unauthenticated"}}
    basic = api.client()
    response = basic.get("/v1/status", headers={
        "Authorization": "Basic " + api.token(Principal.APPROVER)})
    assert response.status_code == 401


def test_ac2_operator_cannot_decide_or_guard_and_approver_can(api):
    env = api.env
    approval = env.make_plugin_approval("p1", 1.0)
    operator = api.client(Principal.OPERATOR)
    approver = api.client(Principal.APPROVER)
    digest = {"payload_sha256": digest_of(env, approval)}
    for path, body in ((f"/v1/approvals/{approval}/approve", digest),
                       (f"/v1/approvals/{approval}/reject", {}),
                       (f"/v1/approvals/{approval}/retry", digest),
                       ("/v1/killswitch/reset", {"expected_generation": 0}),
                       ("/v1/killswitch/reconcile", {}),
                       ("/v1/data/resume", {"acknowledge": True})):
        response = operator.post(path, json=body)
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "forbidden"
    assert env.approval(approval)["status"] == "pending"
    reply = approver.post(f"/v1/approvals/{approval}/approve", json=digest)
    assert reply.status_code == 202
    _wait_job(approver, reply.json()["data"]["job_id"])
    assert env.approval(approval)["decided_by"] == "api:approver"
    assert approver.post("/v1/data/resume", json={"acknowledge": True}).status_code == 200


# ---------------------------------------------------------------- AC-43

_ALLOWED = {
    Principal.OPERATOR: contracts.PRINCIPAL_SCOPES[Principal.OPERATOR],
    Principal.APPROVER: contracts.PRINCIPAL_SCOPES[Principal.APPROVER],
}


@pytest.mark.parametrize("principal", list(Principal))
def test_ac43_every_route_by_scope_matches_the_table(api, principal):
    env = api.env
    client = api.client(principal)
    for route in ROUTES:
        path = route.pattern.pattern.strip("^$")
        path = path.replace("(?P<id>[^/]+)", "999999").replace("(?P<order_id>[^/]+)", "999999")
        body = {"payload_sha256": "0" * 64, "expected_generation": 0, "question": "q",
                "idea": "i", "text": "t", "acknowledge": True, "expected_attempts": 0,
                "expected_last_attempt_at": None}
        response = client.request(route.method, path,
                                  json=body if route.method == "POST" else None,
                                  headers={"Idempotency-Key": f"k-{route.handler}"})
        code = response.json().get("error", {}).get("code")
        if route.scope in _ALLOWED[principal]:
            assert code != "forbidden", (principal, route.handler)
        else:
            assert response.status_code == 403 and code == "forbidden", (principal,
                                                                         route.handler)
    assert env.conn.execute("SELECT COUNT(*) FROM ops_requests WHERE "
                            "authenticated_principal NOT IN ('operator','approver')"
                            ).fetchone()[0] == 0


def test_ac43_body_cannot_set_decided_by_or_asserted_actor(api):
    env = api.env
    approval = env.make_plugin_approval("p1", 1.0)
    approver = api.client(Principal.APPROVER)
    reply = approver.post(f"/v1/approvals/{approval}/approve", json={
        "payload_sha256": digest_of(env, approval), "decided_by": "human:alice",
        "asserted_actor": "alice", "authenticated_principal": "operator"})
    assert reply.status_code == 202
    _wait_job(approver, reply.json()["data"]["job_id"])
    assert env.approval(approval)["decided_by"] == "api:approver"
    who = approver.get("/v1/whoami", headers={"X-Asserted-Actor": "alice"}).json()["data"]
    assert who["authenticated_principal"] == "approver" and who["asserted_actor"] is None
    rows = env.conn.execute("SELECT authenticated_principal,asserted_actor FROM ops_requests"
                            ).fetchall()
    assert rows and all(r["asserted_actor"] is None for r in rows)
    assert {r["authenticated_principal"] for r in rows} == {"approver"}


def test_ac43_scope_set_without_approvals_detail_cannot_read_detail(api, monkeypatch):
    env = api.env
    approval = env.make_plugin_approval("p1", 1.0)
    reduced = dict(contracts.PRINCIPAL_SCOPES)
    reduced[Principal.OPERATOR] = reduced[Principal.OPERATOR] - {Scope.APPROVALS_DETAIL}
    monkeypatch.setattr(contracts, "PRINCIPAL_SCOPES", reduced)
    operator = api.client(Principal.OPERATOR)
    assert operator.get(f"/v1/approvals/{approval}").status_code == 403
    assert operator.get("/v1/approvals").status_code == 200


@pytest.mark.parametrize("mutation", ["unknown_scope", "local_guard_to_operator",
                                      "missing_principal"])
def test_ac43_bad_authorization_table_refuses_to_start(ops_env, monkeypatch, mutation):
    from agentic_fx.ops.api_server import ApiServer
    from ._api import make_api
    table = dict(contracts.PRINCIPAL_SCOPES)
    if mutation == "unknown_scope":
        table[Principal.OPERATOR] = frozenset(table[Principal.OPERATOR] | {"admin"})
    elif mutation == "local_guard_to_operator":
        table[Principal.OPERATOR] = frozenset(table[Principal.OPERATOR] | {Scope.LOCAL_GUARD})
    else:
        table.pop(Principal.OPERATOR)
    import agentic_fx.ops.api_server as api_server
    monkeypatch.setattr(api_server, "PRINCIPAL_SCOPES", table)
    with pytest.raises(ValueError):
        make_api(ops_env, start=False)
    assert not (ops_env.root / "data" / "run" / "api.sock").exists()
    assert ApiServer is api_server.ApiServer


def test_events_cursor_polling_redelivers_same_ids_and_bounds_limit(api):
    client = api.client(Principal.OPERATOR)
    for i in range(3):
        assert client.post("/v1/backlog", json={"idea": f"i{i}"},
                           headers={"Idempotency-Key": f"e{i}"}).status_code == 200
    first = client.get("/v1/events?after=0&limit=2").json()["data"]
    again = client.get("/v1/events?after=0&limit=2").json()["data"]
    assert [e["id"] for e in first["events"]] == [e["id"] for e in again["events"]]
    assert len(first["events"]) == 2 and first["high_watermark"] >= 3
    cursor = first["events"][-1]["id"]
    rest = client.get(f"/v1/events?after={cursor}&limit=500").json()["data"]["events"]
    assert all(e["id"] > cursor for e in rest)
    for bad in ("limit=0", "limit=501", "after=x"):
        assert client.get(f"/v1/events?{bad}").status_code == 400


# ---------------------------------------------------------------- 表の一致

_EXPECTED_TABLE = {
    "status": ("GET", "/v1/status", Scope.STATUS_READ),
    "log": ("GET", "/v1/log", Scope.LOGS_READ),
    "activity": ("GET", "/v1/activity", Scope.LOGS_READ),
    "approval_list": ("GET", "/v1/approvals", Scope.APPROVALS_LIST),
    "approval_detail": ("GET", "/v1/approvals/{id}", Scope.APPROVALS_DETAIL),
    "reflection_status": ("GET", "/v1/reflections/{order_id}", Scope.STATUS_READ),
    "whoami": ("GET", "/v1/whoami", Scope.STATUS_READ),
    "job_get": ("GET", "/v1/jobs/{id}", Scope.JOBS_OWN),
    "events": ("GET", "/v1/events", Scope.EVENTS_READ),
    "ask": ("POST", "/v1/asks", Scope.OPERATE),
    "data_resume": ("POST", "/v1/data/resume", Scope.LOCAL_GUARD),
    "approve": ("POST", "/v1/approvals/{id}/approve", Scope.DECIDE),
    "reject": ("POST", "/v1/approvals/{id}/reject", Scope.DECIDE),
    "retry": ("POST", "/v1/approvals/{id}/retry", Scope.DECIDE),
    "killswitch_reset": ("POST", "/v1/killswitch/reset", Scope.LOCAL_GUARD),
    "killswitch_reconcile": ("POST", "/v1/killswitch/reconcile", Scope.LOCAL_GUARD),
    "reflection_retry": ("POST", "/v1/reflections/{order_id}/retry", Scope.OPERATE),
    "improve": ("POST", "/v1/improve/runs", Scope.OPERATE),
    "backlog_add": ("POST", "/v1/backlog", Scope.OPERATE),
    "backlog_reject": ("POST", "/v1/backlog/{id}/reject", Scope.OPERATE),
    "backlog_reopen": ("POST", "/v1/backlog/{id}/reopen", Scope.OPERATE),
    "backlog_note": ("POST", "/v1/backlog/{id}/note", Scope.OPERATE),
    "policy": ("POST", "/v1/policy", Scope.OPERATE),
    "job_cancel": ("POST", "/v1/jobs/{id}/cancel", Scope.JOBS_OWN),
}


def test_ac1_routes_and_endpoints_match_the_independent_table_exactly():
    from agentic_fx.ops.service import ENDPOINTS
    assert len(_EXPECTED_TABLE) == 24
    assert {code: (e.route.split(" ", 1)[0], e.route.split(" ", 1)[1], e.scope)
            for code, e in ENDPOINTS.items()} == _EXPECTED_TABLE
    assert {r.handler: (r.method, r.scope) for r in ROUTES} == {
        code: (v[0], v[2]) for code, v in _EXPECTED_TABLE.items()}
    for route in ROUTES:
        assert route.pattern.pattern == "^" + _regex(_EXPECTED_TABLE[route.handler][1]) + "$"


def _regex(path: str) -> str:
    import re
    return re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path)


def test_ac1_routes_follow_the_endpoint_table_when_it_changes():
    import dataclasses
    from agentic_fx.ops.api_server import build_routes
    from agentic_fx.ops.service import ENDPOINTS
    changed = dict(ENDPOINTS)
    changed["policy"] = dataclasses.replace(ENDPOINTS["policy"], scope=Scope.LOGS_READ)
    by_handler = {r.handler: r for r in build_routes(changed)}
    assert by_handler["policy"].scope is Scope.LOGS_READ
    assert {r.handler: r for r in ROUTES}["policy"].scope is Scope.OPERATE


def test_ac1_building_routes_from_a_broken_table_fails_closed():
    import dataclasses
    from agentic_fx.ops.api_server import build_routes
    from agentic_fx.ops.service import ENDPOINTS
    missing = {k: v for k, v in ENDPOINTS.items() if k != "whoami"}
    with pytest.raises(RuntimeError):
        build_routes(missing)
    bad = dict(ENDPOINTS)
    bad["status"] = dataclasses.replace(ENDPOINTS["status"], route="FETCH /v1/status")
    with pytest.raises(RuntimeError):
        build_routes(bad)
