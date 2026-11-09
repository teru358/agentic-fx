"""改善の開始の冪等 replay は、受理した actor だけに返る。"""
from __future__ import annotations

import threading
import time

import pytest

from agentic_fx.core.improve_supervisor import RepeatResult
from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.ops.contracts import Principal

from ._improve_repeat_fake import FakeSupervisor

WEB = Principal.OPS_WEB_WRITE
BOT = Principal.OPS_BOT
ALICE, BOB = "web:alice", "web:bob"
D1, D2 = "discord:" + "1" * 18, "discord:" + "2" * 18


@pytest.fixture
def gate():
    event = threading.Event()
    yield event
    event.set()


def _svc(env, gate):
    sup = FakeSupervisor(lambda **kw: (gate.wait(10), RepeatResult("max_runs", 1, 3))[1])
    return env.service(improve_supervisor=sup)


def _wait_terminal(svc, job_id, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = svc.jobs.lookup(job_id)
        if job is None or job.state.value not in ("queued", "running"):
            return
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _code(call):
    with pytest.raises(OpsError) as raised:
        call()
    return raised.value.code


CASES = [(WEB, ALICE, BOB)]


@pytest.mark.parametrize("principal,first,second", CASES)
def test_another_actor_cannot_replay_a_running_start(ops_env, gate, principal, first, second):
    svc = _svc(ops_env, gate)
    started = svc.improve(principal, "k", max_runs=2, asserted_actor=first)
    again = svc.improve(principal, "k", max_runs=2, asserted_actor=first)
    assert again["job_id"] == started["job_id"]
    assert _code(lambda: svc.improve(principal, "k", max_runs=2,
                                     asserted_actor=second)) is ErrorCode.FORBIDDEN
    # 本文が違っても、先に所有で断る (本文の不一致を教えない)。
    assert _code(lambda: svc.improve(principal, "k", max_runs=3,
                                     asserted_actor=second)) is ErrorCode.FORBIDDEN


@pytest.mark.parametrize("principal,first,second", CASES)
def test_another_actor_cannot_replay_a_finished_start(ops_env, gate, principal, first, second):
    svc = _svc(ops_env, gate)
    started = svc.improve(principal, "k", asserted_actor=first)
    _wait_terminal(svc, started["job_id"])
    assert svc.improve(principal, "k", asserted_actor=first)["job_id"] == started["job_id"]
    assert _code(lambda: svc.improve(principal, "k", asserted_actor=second)) is ErrorCode.FORBIDDEN


@pytest.mark.parametrize("principal,first,second", CASES)
def test_ownership_survives_a_restart(ops_env, gate, principal, first, second):
    svc = _svc(ops_env, gate)
    started = svc.improve(principal, "k", asserted_actor=first)
    _wait_terminal(svc, started["job_id"])
    fresh = _svc(ops_env, gate)
    assert _code(lambda: fresh.improve(principal, "k", asserted_actor=second)) is ErrorCode.FORBIDDEN
    assert fresh.improve(principal, "k", asserted_actor=first)["job_id"] == started["job_id"]


def test_the_same_actor_with_a_different_body_is_a_mismatch(ops_env, gate):
    svc = _svc(ops_env, gate)
    svc.improve(WEB, "k", max_runs=2, asserted_actor=ALICE)
    for kwargs in ({"max_runs": 3}, {"duration_sec": 600}, {}):
        assert _code(lambda: svc.improve(WEB, "k", asserted_actor=ALICE,
                                         **kwargs)) is ErrorCode.IDEMPOTENCY_MISMATCH


def test_the_audit_store_checks_the_actor_for_the_bot_too(ops_env):
    audit = _svc(ops_env, threading.Event()).audit
    route = "POST /v1/improve/runs"

    def accept(actor, body):
        return audit.accept_idempotent(endpoint=route, principal=BOT, asserted_actor=actor,
                                       key="k", body=body, target_ref="improve", job_id="j1")

    first = accept(D1, {})
    assert first.request_id
    assert accept(D1, {}).job_id == "j1"
    assert _code(lambda: accept(D2, {})) is ErrorCode.FORBIDDEN
    assert _code(lambda: accept(D2, {"max_runs": 2})) is ErrorCode.FORBIDDEN
    assert _code(lambda: accept(D1, {"max_runs": 2})) is ErrorCode.IDEMPOTENCY_MISMATCH


def test_a_refused_replay_clears_the_comment(ops_env, gate, monkeypatch):
    import agentic_fx.core.improve_repeat as ir
    svc = _svc(ops_env, gate)
    svc.improve(WEB, "k", max_runs=2, asserted_actor=ALICE)
    created = []
    real = ir.CommentHolder

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            created.append(self)

    monkeypatch.setattr(ir, "CommentHolder", Spy)
    assert _code(lambda: svc.improve(WEB, "k", max_runs=2, comment="secret note",
                                     asserted_actor=BOB)) is ErrorCode.FORBIDDEN
    assert created and all(h.get() is None for h in created)
