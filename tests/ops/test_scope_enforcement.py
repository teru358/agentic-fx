"""ops 関数は scope 表だけで認可する。operator は decide / local_guard を持たない。"""
from __future__ import annotations

import dataclasses
import time

import pytest

from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.ops.contracts import (PRINCIPAL_SCOPES, Principal, Scope, assert_authorized,
                                      validate_principal_scopes)
from agentic_fx.ops.jobs import JobRegistry
from agentic_fx.ops.service import ENDPOINTS

from .conftest import OPERATOR


def test_operator_cannot_decide_or_use_local_guard(ops_env):
    service = ops_env.service(data_resume=lambda ack: {})
    approval_id = ops_env.raw_approval()
    calls = [
        lambda: service.approve(OPERATOR, approval_id, "0" * 64),
        lambda: service.reject(OPERATOR, approval_id),
        lambda: service.retry(OPERATOR, approval_id, "0" * 64),
        lambda: service.reset_kill_switch(OPERATOR, 1),
        lambda: service.reconcile_kill_switch(OPERATOR),
        lambda: service.resume_data(OPERATOR, acknowledge=True),
    ]
    for call in calls:
        with pytest.raises(OpsError) as raised:
            call()
        assert raised.value.code is ErrorCode.FORBIDDEN
    assert ops_env.ops_rows() == []
    assert ops_env.approval(approval_id)["status"] == "pending"


def test_scope_table_matches_the_spec():
    assert PRINCIPAL_SCOPES[Principal.OPERATOR] == frozenset({
        Scope.STATUS_READ, Scope.EVENTS_READ, Scope.APPROVALS_LIST, Scope.APPROVALS_DETAIL,
        Scope.LOGS_READ, Scope.JOBS_OWN, Scope.OPERATE})
    assert PRINCIPAL_SCOPES[Principal.APPROVER] == frozenset(Scope)
    validate_principal_scopes()
    with pytest.raises(ValueError):
        validate_principal_scopes({Principal.OPERATOR: frozenset({Scope.LOCAL_GUARD}),
                                   Principal.APPROVER: frozenset(Scope)})


def test_jobs_are_visible_only_to_their_owner(ops_env):
    import concurrent.futures
    future = concurrent.futures.Future()
    service = ops_env.service(ask_submitter=lambda q: future)
    reply = service.ask(OPERATOR, "k", "質問")
    with pytest.raises(OpsError) as raised:
        service.get_job(Principal.APPROVER, reply["job_id"])
    assert raised.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(OpsError) as raised:
        service.cancel_job(Principal.APPROVER, reply["job_id"])
    assert raised.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(OpsError) as raised:
        service.cancel_job(OPERATOR, reply["job_id"])
    assert raised.value.code is ErrorCode.JOB_RUNNING
    future.set_result("x")


def test_scope_table_without_every_principal_or_with_a_repeated_scope_is_refused():
    with pytest.raises(ValueError):
        validate_principal_scopes({Principal.APPROVER: frozenset(Scope)})
    with pytest.raises(ValueError):
        validate_principal_scopes({Principal.OPERATOR: (Scope.STATUS_READ, Scope.STATUS_READ),
                                   Principal.APPROVER: frozenset(Scope)})


@pytest.mark.parametrize("caller", ["approver", None, object()])
def test_caller_that_is_neither_a_principal_nor_the_shell_is_forbidden(caller):
    with pytest.raises(OpsError) as raised:
        assert_authorized(caller, Scope.STATUS_READ)
    assert raised.value.code is ErrorCode.FORBIDDEN


def _every_endpoint(service):
    return {
        "status": lambda p: service.status(p),
        "log": lambda p: service.log(p, 1),
        "activity": lambda p: service.activity(p, 1),
        "approval_detail": lambda p: service.approval_detail(p, 9999),
        "approval_list": lambda p: service.approval_list(p),
        "reflection_status": lambda p: service.reflection_status(p, 9999),
        "whoami": lambda p: service.whoami(p),
        "job_get": lambda p: service.get_job(p, "missing"),
        "events": lambda p: service.events_after(p, after=0, limit=1),
        "ask": lambda p: service.ask(p, f"k-ask-{p}", "質問"),
        "data_resume": lambda p: service.resume_data(p, acknowledge=True),
        "approve": lambda p: service.approve(p, 9999, "0" * 64),
        "reject": lambda p: service.reject(p, 9999),
        "retry": lambda p: service.retry(p, 9999, "0" * 64),
        "killswitch_reset": lambda p: service.reset_kill_switch(p, 0),
        "killswitch_reconcile": lambda p: service.reconcile_kill_switch(p),
        "reflection_retry": lambda p: service.retry_reflection(
            p, f"k-ref-{p}", 9999, expected_attempts=0, expected_last_attempt_at=None),
        "improve": lambda p: service.improve(p, f"k-imp-{p}"),
        "backlog_add": lambda p: service.add_backlog(p, f"k-bl-{p}", "idea"),
        "backlog_reject": lambda p: service.transition_backlog(p, 9999, "reject"),
        "backlog_reopen": lambda p: service.transition_backlog(p, 9999, "reopen"),
        "backlog_note": lambda p: service.transition_backlog(p, 9999, "note"),
        "policy": lambda p: service.add_policy(p, f"k-pol-{p}", "text"),
        "job_cancel": lambda p: service.cancel_job(p, "missing"),
    }


def test_every_endpoint_admits_exactly_the_principals_holding_its_scope(ops_env):
    """endpoint 表の scope と ops 関数の認可が同じ答えを返す (総当たり)。"""
    service = ops_env.service()
    calls = _every_endpoint(service)
    assert set(calls) == set(ENDPOINTS)
    for code, call in calls.items():
        for principal in Principal:
            allowed = ENDPOINTS[code].scope in PRINCIPAL_SCOPES[principal]
            try:
                call(principal)
                outcome = None
            except OpsError as exc:
                outcome = exc.code
            assert (outcome is ErrorCode.FORBIDDEN) is (not allowed), (code, principal.value)


def test_job_owner_is_the_pair_of_principal_and_asserted_actor():
    registry = JobRegistry(monotonic=time.monotonic)
    job = registry.enqueue_single(OPERATOR, None, "ask", lambda j: {}, deadline_seconds=60,
                                  busy_code=ErrorCode.MISSION_BUSY)
    assert registry.get(job.id, OPERATOR, None) is job
    with pytest.raises(OpsError) as raised:
        registry.get(job.id, OPERATOR, "someone")
    assert raised.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(OpsError) as raised:
        registry.cancel(job.id, OPERATOR, "someone")
    assert raised.value.code is ErrorCode.NOT_FOUND


@pytest.mark.parametrize("scope", [Scope.DECIDE, Scope.STATUS_READ])
def test_authorization_follows_the_endpoint_table(ops_env, monkeypatch, scope):
    """表の scope を書き換えると ops 関数の認可がそれに従う (表が唯一の正)。"""
    service = ops_env.service()
    calls = _every_endpoint(service)
    for code in ENDPOINTS:
        monkeypatch.setitem(ENDPOINTS, code, dataclasses.replace(ENDPOINTS[code], scope=scope))
    for code, call in calls.items():
        try:
            call(OPERATOR)
            outcome = None
        except OpsError as exc:
            outcome = exc.code
        forbidden = scope not in PRINCIPAL_SCOPES[OPERATOR]
        assert (outcome is ErrorCode.FORBIDDEN) is forbidden, (code, outcome)
