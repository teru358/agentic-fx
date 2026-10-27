"""繰り返し improve job の registry 契約: 初期 result、進捗の merge、走行中 cancel。"""
from __future__ import annotations

import threading

import pytest

from agentic_fx.ops.contracts import ErrorCode, OpsError, Principal
from agentic_fx.ops.jobs import JobRegistry, JobState

OPERATOR = Principal.OPERATOR
APPROVER = Principal.APPROVER

INITIAL = {"mission_id": None, "runs": 0, "max_runs": 3, "deadline_at": "x",
           "phase": "waiting_slot", "stop_requested": False}


def _registry():
    return JobRegistry(monotonic=lambda: 100.0)


def _single(registry, kind="improve", *, repeat=False, actor=None, principal=OPERATOR):
    kwargs = {}
    if repeat:
        kwargs = {"initial_result": dict(INITIAL), "cancel_event": threading.Event()}
    return registry.enqueue_single(principal, actor, kind, lambda job: {},
                                   deadline_seconds=35.0,
                                   busy_code=ErrorCode.IMPROVE_RUNNING, **kwargs)


def test_initial_result_is_visible_as_soon_as_the_job_is():
    registry = _registry()
    job = _single(registry, repeat=True)
    seen = registry.lookup(job.id)
    assert seen.result == INITIAL
    assert seen.result is not INITIAL
    assert seen.cancel_event is not None


def test_enqueue_without_the_new_arguments_is_unchanged():
    registry = _registry()
    job = _single(registry)
    assert job.result is None and job.cancel_event is None


@pytest.mark.parametrize("kind", ["improve", "ask"])
def test_running_cancel_of_a_non_repeat_job_is_still_job_running(kind):
    registry = _registry()
    job = _single(registry, kind)
    with pytest.raises(OpsError) as raised:
        registry.cancel(job.id, OPERATOR, None)
    assert raised.value.code is ErrorCode.JOB_RUNNING
    assert job.state is JobState.RUNNING


def test_running_decision_cancel_is_still_job_running():
    registry = _registry()
    registry.enqueue_decision(APPROVER, "a", "approve", lambda job: {},
                              deadline_seconds=35.0, target="approval:1")
    job = registry.take_next_decision()
    with pytest.raises(OpsError) as raised:
        registry.cancel(job.id, APPROVER, "a")
    assert raised.value.code is ErrorCode.JOB_RUNNING


def test_running_cancel_of_a_repeat_job_sets_the_event_and_keeps_it_running():
    registry = _registry()
    job = _single(registry, repeat=True)
    done = job.done
    out = registry.cancel(job.id, OPERATOR, None)
    assert out is job and job.state is JobState.RUNNING
    assert job.cancel_event.is_set()
    assert job.result["stop_requested"] is True
    assert job.terminal_mono is None and not done.is_set()
    # 二度目も同じ応答。
    again = registry.cancel(job.id, OPERATOR, None)
    assert again is job and job.state is JobState.RUNNING
    assert job.result["stop_requested"] is True


def test_cancel_owner_rules_are_unchanged_for_a_repeat_job():
    registry = _registry()
    job = _single(registry, repeat=True, actor="a")
    with pytest.raises(OpsError) as other_principal:
        registry.cancel(job.id, APPROVER, "a")
    assert other_principal.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(OpsError) as other_actor:
        registry.cancel(job.id, OPERATOR, "b")
    assert other_actor.value.code is ErrorCode.NOT_FOUND
    assert not job.cancel_event.is_set()
    with pytest.raises(OpsError) as forbidden:
        registry.check_owner(job.id, OPERATOR, "b")
    assert forbidden.value.code is ErrorCode.FORBIDDEN


def test_update_result_merges_into_a_fresh_dict():
    registry = _registry()
    job = _single(registry, repeat=True)
    before = job.result
    registry.update_result(job, {"phase": "running", "runs": 1, "mission_id": 7})
    assert job.result is not before
    assert before == INITIAL
    assert job.result == {**INITIAL, "phase": "running", "runs": 1, "mission_id": 7}


def test_update_result_never_lowers_stop_requested():
    registry = _registry()
    job = _single(registry, repeat=True)
    registry.cancel(job.id, OPERATOR, None)
    registry.update_result(job, {"phase": "running", "stop_requested": False})
    assert job.result["stop_requested"] is True
    assert job.result["phase"] == "running"


def test_concurrent_cancel_and_progress_never_lose_the_stop_request():
    for _ in range(200):
        registry = _registry()
        job = _single(registry, repeat=True)
        start = threading.Barrier(2)

        def progress():
            start.wait()
            for n in range(20):
                registry.update_result(job, {"runs": n})

        thread = threading.Thread(target=progress)
        thread.start()
        start.wait()
        registry.cancel(job.id, OPERATOR, None)
        thread.join()
        assert job.result["stop_requested"] is True
        assert job.result["runs"] == 19


def test_update_result_after_a_terminal_state_is_ignored():
    registry = _registry()
    job = _single(registry, repeat=True)
    registry.finish(job, state=JobState.DONE, result={"stop_reason": "deadline"})
    snapshot = dict(job.result)
    registry.update_result(job, {"phase": "running", "runs": 9})
    assert job.result == snapshot


def test_finish_keeps_the_last_progress_and_progress_wins_on_shared_keys():
    registry = _registry()
    job = _single(registry, repeat=True)
    registry.update_result(job, {"phase": "finished", "stop_reason": "deadline",
                                 "runs": 2, "mission_id": 5})
    registry.finish(job, state=JobState.DONE, result={
        "stop_reason": "max_runs", "runs": 99, "mission_id": 1, "phase": "running",
        "stop_requested": True, "extra": "kept"})
    assert job.result["stop_reason"] == "deadline"
    assert job.result["runs"] == 2 and job.result["mission_id"] == 5
    assert job.result["phase"] == "finished"
    assert job.result["stop_requested"] is False
    assert job.result["extra"] == "kept"
    assert job.result["max_runs"] == 3


def test_finish_with_no_result_keeps_the_progress():
    registry = _registry()
    job = _single(registry, repeat=True)
    registry.update_result(job, {"phase": "finished", "runs": 1})
    registry.finish(job, state=JobState.FAILED, result=None, error_code="internal")
    assert job.result["phase"] == "finished" and job.result["runs"] == 1


def test_finish_result_for_jobs_without_progress_is_unchanged():
    registry = _registry()
    ask = _single(registry, "ask")
    registry.finish(ask, state=JobState.DONE, result={"answer": "x"})
    assert ask.result == {"answer": "x"}
    failed = _single(registry, "improve")
    registry.finish(failed, state=JobState.FAILED, result=None, error_code="internal")
    assert failed.result is None
    one_shot = _single(registry, "improve")
    registry.update_result(one_shot, {"mission_id": 4})
    registry.finish(one_shot, state=JobState.DONE, result={"mission_id": 4})
    assert one_shot.result == {"mission_id": 4}
