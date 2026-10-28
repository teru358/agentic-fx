"""improve の繰り返し・一時指示の service 層: 受理前の検査、結線、進捗、comment の経路。"""
from __future__ import annotations

import gc
import hashlib
import json
import logging
import sqlite3
import threading
import time
import types
from datetime import timezone

import pytest

from agentic_fx.core.improve_supervisor import RepeatResult
from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.ops.contracts import ShellCaller
from agentic_fx.ops.jobs import JobState

from ._improve_repeat_fake import FakeSupervisor
from .conftest import OPERATOR, wait_job

CANARY = "".join(["CANARY", "-ONEOFF-", "77c2"])


def _service(env, sup, **kw):
    return env.service(improve_supervisor=sup, **kw)


def _holder(service, job_id):
    job = service.jobs.lookup(job_id)
    for cell in job.action.__closure__:
        value = cell.cell_contents
        if hasattr(value, "clear") and hasattr(value, "get") and not isinstance(
                value, (dict, list, set)):
            return value
    return None


# ---- 本文なし ----

def test_empty_body_is_the_same_digest_and_path_as_before(ops_env):
    sup = FakeSupervisor()
    service = _service(ops_env, sup)
    reply = service.improve(OPERATOR, "k1")
    job = wait_job(service, reply["job_id"])
    assert job["result"] == {"mission_id": 17} and job["state"] == "done"
    assert sup.manual_calls == [{"on_prepared": sup.manual_calls[0]["on_prepared"]}]
    assert sup.repeat_calls == []
    row = ops_env.ops_rows()[0]
    assert row["body_sha256"] == hashlib.sha256(b"{}").hexdigest()


def test_comment_only_runs_one_shot_without_the_gate_or_repeat(ops_env):
    sup = FakeSupervisor()
    service = _service(ops_env, sup)
    reply = service.improve(OPERATOR, "k1", comment="  今回は  慎重に ")
    job = wait_job(service, reply["job_id"])
    assert job["result"] == {"mission_id": 17}
    assert sup.repeat_calls == []
    assert sup.manual_calls[0]["comment"] == "今回は 慎重に"
    assert service.jobs.lookup(reply["job_id"]).cancel_event is None


def test_max_runs_one_is_a_repeat_job(ops_env):
    release = threading.Event()
    sup = FakeSupervisor(lambda **kw: (release.wait(10), RepeatResult("max_runs", 1, 5))[1])
    service = _service(ops_env, sup)
    reply = service.improve(OPERATOR, "k1", max_runs=1)
    assert sup.entered.wait(5)
    assert sup.manual_calls == []
    job = service.get_job(OPERATOR, reply["job_id"])
    assert job["result"]["max_runs"] == 1
    release.set()
    done = wait_job(service, reply["job_id"])
    assert done["state"] == "done" and done["result"]["stop_reason"] == "max_runs"
    call = sup.repeat_calls[0]
    assert call["max_runs"] == 1 and call["duration_sec"] is None


def test_duration_only_does_not_take_the_one_shot_path(ops_env):
    sup = FakeSupervisor()
    service = _service(ops_env, sup)
    reply = service.improve(OPERATOR, "k1", duration_sec=3600)
    wait_job(service, reply["job_id"])
    assert sup.manual_calls == []
    assert sup.repeat_calls[0]["duration_sec"] == 3600


# ---- 受理前の検査 ----

@pytest.mark.parametrize("kwargs", [
    {"duration_sec": 59}, {"duration_sec": 86401}, {"duration_sec": True},
    {"duration_sec": "3600"}, {"duration_sec": 3600.0}, {"duration_sec": -1},
    {"max_runs": 0}, {"max_runs": 25}, {"max_runs": True}, {"max_runs": "3"},
    {"max_runs": 1.0},
    {"comment": ""}, {"comment": "   "}, {"comment": "a\x00b"}, {"comment": "a\x07b"},
    {"comment": "a b"}, {"comment": 12}, {"comment": "x" * 1001},
    {"comment": b"bytes"}, {"comment": ["a"]},
])
def test_daemon_rejects_bad_arguments_before_any_audit_row(ops_env, kwargs):
    sup = FakeSupervisor()
    service = _service(ops_env, sup)
    with pytest.raises(OpsError) as raised:
        service.improve(OPERATOR, "k1", **kwargs)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert ops_env.ops_rows() == []
    assert service.jobs._jobs == {}
    assert sup.manual_calls == [] and sup.repeat_calls == []


@pytest.mark.parametrize("seconds", [60, 61, 86400])
def test_duration_bounds_are_accepted(ops_env, seconds):
    service = _service(ops_env, FakeSupervisor())
    reply = service.improve(OPERATOR, f"k{seconds}", duration_sec=seconds)
    assert wait_job(service, reply["job_id"])["state"] == "done"


def _all_persisted_text(ops_env):
    """監査表と冪等表に残る全 column の文字列表現。"""
    out = []
    for table in ("ops_requests", "ops_idempotency"):
        for row in ops_env.conn.execute(f"SELECT * FROM {table}"):
            out.extend(str(v) for v in tuple(row))
    return out


def _digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def test_the_comment_and_its_digest_are_not_persisted_only_its_presence_is(ops_env):
    service = _service(ops_env, FakeSupervisor())
    reply = service.improve(OPERATOR, "k1", max_runs=2, comment=CANARY)
    wait_job(service, reply["job_id"])
    persisted = _all_persisted_text(ops_env)
    assert not any(CANARY in text for text in persisted)
    for candidate in ({"max_runs": 2, "comment": CANARY},
                      {"comment": CANARY}, {"comment": CANARY, "max_runs": 2}):
        assert _digest(candidate) not in persisted
    assert _digest({"max_runs": 2, "has_comment": True}) in persisted
    assert hashlib.sha256(CANARY.encode()).hexdigest() not in persisted


def test_a_resend_with_the_same_key_returns_the_same_job_whatever_the_comment(ops_env):
    service = _service(ops_env, FakeSupervisor())
    first = service.improve(OPERATOR, "same", comment=" a   b ")
    wait_job(service, first["job_id"])
    assert service.improve(OPERATOR, "same", comment="a b")["job_id"] == first["job_id"]
    # 本文に残さないので、comment だけが違う再送は見分けない (同じ job を返す)。
    assert service.improve(OPERATOR, "same", comment="a c")["job_id"] == first["job_id"]
    assert len({r["body_sha256"] for r in ops_env.ops_rows() if r["body_sha256"]}) == 1


def test_a_resend_that_differs_in_the_rest_of_the_body_is_still_a_mismatch(ops_env):
    service = _service(ops_env, FakeSupervisor())
    first = service.improve(OPERATOR, "same", max_runs=2, comment="a")
    wait_job(service, first["job_id"])
    with pytest.raises(OpsError) as raised:
        service.improve(OPERATOR, "same", max_runs=3, comment="a")
    assert raised.value.code is ErrorCode.IDEMPOTENCY_MISMATCH
    with pytest.raises(OpsError) as no_comment:
        service.improve(OPERATOR, "same", max_runs=2)
    assert no_comment.value.code is ErrorCode.IDEMPOTENCY_MISMATCH


def test_a_body_without_comment_keeps_only_the_keys_that_were_given(ops_env):
    service = _service(ops_env, FakeSupervisor())
    reply = service.improve(OPERATOR, "k1", max_runs=2)
    wait_job(service, reply["job_id"])
    digest = hashlib.sha256(json.dumps({"max_runs": 2}, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    assert ops_env.ops_rows()[0]["body_sha256"] == digest


# ---- 初期 result と進捗 ----

def test_the_initial_result_is_complete_before_the_job_thread_starts(ops_env):
    service = _service(ops_env, FakeSupervisor())
    service._start_thread = lambda job: None
    reply = service.improve(OPERATOR, "k1", duration_sec=5400, max_runs=3)
    job = service.get_job(OPERATOR, reply["job_id"])
    expected_deadline = (ops_env.wall() + __import__("datetime").timedelta(seconds=5400)
                         ).astimezone(timezone.utc).isoformat()
    assert job["state"] == "running"
    assert job["result"] == {
        "mission_id": None, "runs": 0, "max_runs": 3, "deadline_at": expected_deadline,
        "duration_sec": 5400, "elapsed_sec": 0,
        "phase": "waiting_slot", "stop_requested": False}
    assert job["result"]["deadline_at"].endswith("+00:00")


def test_defaults_fill_the_initial_result_for_single_option_requests(ops_env):
    service = _service(ops_env, FakeSupervisor())
    service._start_thread = lambda job: None
    only_n = service.get_job(OPERATOR, service.improve(OPERATOR, "a", max_runs=2)["job_id"])
    assert only_n["result"]["max_runs"] == 2
    wall = ops_env.wall()
    expected = (wall.replace(tzinfo=wall.tzinfo or timezone.utc).astimezone(timezone.utc)
                .timestamp() + 86400)
    got = __import__("datetime").datetime.fromisoformat(only_n["result"]["deadline_at"])
    assert got.timestamp() == pytest.approx(expected)
    service.shutdown(join_timeout=0.1)


def test_the_deadline_is_fixed_at_acceptance_not_in_the_job_thread(ops_env):
    seen = {}

    def repeat(**kw):
        seen.update(kw)
        return RepeatResult("deadline", 1, 5)

    sup = FakeSupervisor(repeat)
    service = _service(ops_env, sup)
    started = []
    real_start = service._start_thread
    service._start_thread = lambda job: started.append(job)
    mono_before = ops_env.mono()
    reply = service.improve(OPERATOR, "k1", duration_sec=600)
    ops_env.mono.advance(1000)
    ops_env.wall.advance(seconds=1000)
    real_start(started[0])
    wait_job(service, reply["job_id"])
    assert seen["deadline_mono"] == pytest.approx(mono_before + 600, abs=2.0)
    assert seen["deadline_at"].startswith("2026-10-05T00:10:00")


def test_a_delay_after_acceptance_does_not_move_the_deadline(ops_env):
    class SlowStore(_Store):
        def load(self, **kw):
            ops_env.mono.advance(1000)
            ops_env.wall.advance(seconds=1000)
            return self._real.load(**kw)

    seen = {}

    def repeat(**kw):
        seen.update(kw)
        return RepeatResult("deadline", 1, 5)

    service = _service(ops_env, FakeSupervisor(repeat), state_store=SlowStore(ops_env.state, []))
    mono_at_acceptance = ops_env.mono()
    reply = service.improve(OPERATOR, "k1", duration_sec=600)
    wait_job(service, reply["job_id"])
    assert seen["deadline_mono"] == pytest.approx(mono_at_acceptance + 600, abs=2.0)
    assert seen["deadline_at"].startswith("2026-10-05T00:10:00")
    initial = service.get_job(OPERATOR, reply["job_id"])["result"]
    assert initial["deadline_at"].startswith("2026-10-05T00:10:00")


def test_the_duration_floor_is_the_one_the_client_grammar_uses(ops_env, monkeypatch):
    from agentic_fx.core import improve_repeat as ir
    monkeypatch.setattr(ir, "MIN_DURATION_SEC", 120)
    service = _service(ops_env, FakeSupervisor())
    with pytest.raises(OpsError) as raised:
        service.improve(OPERATOR, "k1", duration_sec=119)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(ValueError):
        ir.parse_duration("1m")
    assert wait_job(service, service.improve(OPERATOR, "k2", duration_sec=120)["job_id"]
                    )["state"] == "done"


@pytest.mark.parametrize("kind", ["repeat", "one-shot"])
def test_a_thread_that_cannot_start_fails_the_job_and_frees_the_slot(ops_env, kind):
    service = _service(ops_env, FakeSupervisor())

    def broken(job):
        raise RuntimeError("can't start new thread")

    service._start_thread = broken
    kwargs = {"max_runs": 2} if kind == "repeat" else {}
    with pytest.raises(RuntimeError):
        service.improve(OPERATOR, "k1", comment=CANARY, **kwargs)
    (job,) = service.jobs._jobs.values()
    assert job.state is JobState.FAILED and job.error_code == "internal"
    assert job.done is None or job.done.is_set()
    view = service.get_job(OPERATOR, job.id)
    assert (view["state"], view["result_code"]) == ("failed", "internal")
    holder = _holder(service, job.id)
    assert holder is not None and holder.get() is None
    assert ops_env.ops_rows()[-1]["phase"] == "failed"
    # 同種の improve は拒否されない。
    service._start_thread = lambda job: None
    assert service.improve(OPERATOR, "k2", max_runs=2)["state"] == "running"


def test_a_real_thread_start_failure_leaves_no_registered_thread(ops_env, monkeypatch):
    service = _service(ops_env, FakeSupervisor())
    real_start = threading.Thread.start

    def start(self):
        if self.name == "afx-ops-improve":
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    with pytest.raises(RuntimeError):
        service.improve(OPERATOR, "k1", max_runs=2, comment=CANARY)
    assert service._operation_threads == set()
    (job,) = service.jobs._jobs.values()
    assert job.state is JobState.FAILED


def test_progress_is_visible_through_get_job_and_the_final_progress_stays(ops_env):
    gate = threading.Event()

    def repeat(*, on_progress, **kw):
        on_progress({"phase": "running", "runs": 1, "mission_id": 9})
        gate.wait(10)
        on_progress({"phase": "finished", "stop_reason": "deadline"})
        return RepeatResult("deadline", 1, 9)

    sup = FakeSupervisor(repeat)
    service = _service(ops_env, sup)
    reply = service.improve(OPERATOR, "k1", max_runs=3)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        view = service.get_job(OPERATOR, reply["job_id"])
        if view["result"]["runs"] == 1:
            break
        time.sleep(0.01)
    assert view["state"] == "running"
    assert view["result"]["phase"] == "running" and view["result"]["mission_id"] == 9
    gate.set()
    final = wait_job(service, reply["job_id"])
    assert final["state"] == "done"
    assert final["result"]["stop_reason"] == "deadline"
    assert final["result"]["phase"] == "finished"
    assert final["result"]["max_runs"] == 3 and "deadline_at" in final["result"]


def test_the_final_result_follows_the_return_value_when_no_progress_arrived(ops_env):
    sup = FakeSupervisor(lambda **kw: RepeatResult("max_runs", 3, 9))
    service = _service(ops_env, sup)
    reply = service.improve(OPERATOR, "k1", max_runs=3)
    final = wait_job(service, reply["job_id"])
    assert final["state"] == "done"
    assert (final["result"]["stop_reason"], final["result"]["runs"],
            final["result"]["mission_id"], final["result"]["phase"]) == (
        "max_runs", 3, 9, "finished")
    assert final["result"]["max_runs"] == 3 and "deadline_at" in final["result"]


@pytest.mark.parametrize("reason", ["deadline", "max_runs", "stopped",
                                    "consecutive_failures", "resource_exhausted",
                                    "autopilot", "shutdown"])
def test_every_stop_reason_ends_as_done_with_the_reason(ops_env, reason):
    def repeat(*, on_progress, **kw):
        on_progress({"phase": "finished", "stop_reason": reason, "runs": 2,
                     "mission_id": 8})
        return RepeatResult(reason, 2, 8)

    service = _service(ops_env, FakeSupervisor(repeat))
    reply = service.improve(OPERATOR, "k1", max_runs=3)
    job = wait_job(service, reply["job_id"])
    assert job["state"] == "done"
    assert job["result"]["stop_reason"] == reason
    assert job["result"]["runs"] == 2 and job["result"]["mission_id"] == 8


def test_get_job_returns_a_copy_of_the_result(ops_env):
    sup = FakeSupervisor()
    service = _service(ops_env, sup)
    service._start_thread = lambda job: None
    reply = service.improve(OPERATOR, "k1", max_runs=3)
    view = service.get_job(OPERATOR, reply["job_id"])
    view["result"]["runs"] = 99
    assert service.get_job(OPERATOR, reply["job_id"])["result"]["runs"] == 0
    assert service.jobs.lookup(reply["job_id"]).result["runs"] == 0


def test_an_unexpected_controller_error_fails_the_job_as_internal(ops_env, caplog):
    def repeat(*, on_progress, comment_holder, **kw):
        on_progress({"phase": "running", "runs": 1, "mission_id": 4})
        raise RuntimeError("controller broke")

    sup = FakeSupervisor(repeat)
    service = _service(ops_env, sup)
    reply = service.improve(OPERATOR, "k1", max_runs=3, comment=CANARY)
    job = wait_job(service, reply["job_id"])
    assert (job["state"], job["result_code"]) == ("failed", "internal")
    assert job["result"]["runs"] == 1 and job["result"]["mission_id"] == 4
    assert _holder(service, reply["job_id"]).get() is None


# ---- autopilot ----

class _Store:
    """本物の StateStore を包み、開始時の autopilot 読みだけ差し替える。"""

    def __init__(self, real, reads):
        self._real = real
        self.reads = reads
        self.calls = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def autopilot_at_start(self, *, deadline, stop_event):
        self.calls.append((deadline, time.monotonic()))
        value = self.reads.pop(0) if self.reads else False
        if isinstance(value, BaseException):
            raise value
        return value


def test_the_recheck_uses_a_fresh_deadline_each_time_not_the_job_deadline(ops_env):
    store = _Store(ops_env.state, [False, False])
    checks = []

    def repeat(*, autopilot_check, **kw):
        checks.append(autopilot_check())
        ops_env.mono.advance(7200)
        checks.append(autopilot_check())
        return RepeatResult("max_runs", 1, 1)

    service = _service(ops_env, FakeSupervisor(repeat), state_store=store)
    reply = service.improve(OPERATOR, "k1", max_runs=3)
    assert wait_job(service, reply["job_id"])["state"] == "done"
    assert checks == [False, False]
    assert len(store.calls) == 2  # controller の再確認 2 回だけ (service は事前に読まない)
    for deadline, now in store.calls:
        assert 3.0 < deadline - now < 6.0


def test_an_autopilot_true_in_the_recheck_is_the_controllers_decision(ops_env):
    store = _Store(ops_env.state, [True])

    def repeat(*, autopilot_check, **kw):
        assert autopilot_check() is True
        return RepeatResult("autopilot", 0, None)

    service = _service(ops_env, FakeSupervisor(repeat), state_store=store)
    reply = service.improve(OPERATOR, "k1", max_runs=3)
    job = wait_job(service, reply["job_id"])
    assert (job["state"], job["result"]["stop_reason"]) == ("done", "autopilot")


def test_the_shell_caller_is_never_restricted_by_the_recheck(ops_env):
    store = _Store(ops_env.state, [True, True])
    results = []

    def repeat(*, autopilot_check, **kw):
        results.append(autopilot_check())
        return RepeatResult("max_runs", 1, 1)

    service = _service(ops_env, FakeSupervisor(repeat), state_store=store)
    reply = service.improve(ShellCaller.SHELL, "k1", max_runs=3)
    assert wait_job(service, reply["job_id"])["state"] == "done"
    assert results == [False]
    assert store.calls == []


def test_a_failing_recheck_fails_the_job_closed(ops_env):
    store = _Store(ops_env.state, [RuntimeError("flock unreadable")])
    started = []

    def repeat(*, autopilot_check, **kw):
        autopilot_check()
        started.append(True)
        return RepeatResult("max_runs", 1, 1)

    service = _service(ops_env, FakeSupervisor(repeat), state_store=store)
    reply = service.improve(OPERATOR, "k1", max_runs=3)
    job = wait_job(service, reply["job_id"])
    assert (job["state"], job["result_code"]) == ("failed", "internal")
    assert started == []


# ---- cancel ----

def test_cancel_on_a_running_repeat_job_requests_the_stop_and_stays_running(ops_env):
    release = threading.Event()

    def repeat(*, cancel_event, **kw):
        cancel_event.wait(10)
        release.wait(10)
        return RepeatResult("stopped", 1, 5)

    sup = FakeSupervisor(repeat)
    service = _service(ops_env, sup)
    reply = service.improve(OPERATOR, "k1", max_runs=3)
    assert sup.entered.wait(5)
    first = service.cancel_job(OPERATOR, reply["job_id"], asserted_actor=None)
    second = service.cancel_job(OPERATOR, reply["job_id"], asserted_actor=None)
    assert first == second == {"id": reply["job_id"], "state": "running",
                               "stop_requested": True}
    assert service.get_job(OPERATOR, reply["job_id"])["result"]["stop_requested"] is True
    events = [r for r in ops_env.conn.execute(
        "SELECT code, fields_json FROM ops_events WHERE ref=?", (f"job:{reply['job_id']}",))]
    assert events == []
    release.set()
    done = wait_job(service, reply["job_id"])
    assert (done["state"], done["result"]["stop_reason"]) == ("done", "stopped")
    assert done["result"]["stop_requested"] is True
    after = service.cancel_job(OPERATOR, reply["job_id"], asserted_actor=None)
    assert after == {"id": reply["job_id"], "state": "done", "stop_requested": True}


def test_cancel_response_of_other_jobs_keeps_its_shape(ops_env):
    gate = threading.Event()

    class One(FakeSupervisor):
        def submit_manual(self, **kwargs):
            gate.wait(10)
            return 3

    service = _service(ops_env, One())
    reply = service.improve(OPERATOR, "k1")
    with pytest.raises(OpsError) as raised:
        service.cancel_job(OPERATOR, reply["job_id"], asserted_actor=None)
    assert raised.value.code is ErrorCode.JOB_RUNNING
    gate.set()
    wait_job(service, reply["job_id"])
    assert service.cancel_job(OPERATOR, reply["job_id"], asserted_actor=None) == {
        "id": reply["job_id"], "state": "done"}


def test_a_second_improve_while_a_repeat_runs_is_improve_running(ops_env):
    release = threading.Event()
    sup = FakeSupervisor(lambda **kw: release.wait(10) and RepeatResult("max_runs", 1, 1))
    service = _service(ops_env, sup)
    service.improve(OPERATOR, "k1", max_runs=2)
    assert sup.entered.wait(5)
    with pytest.raises(OpsError) as raised:
        service.improve(OPERATOR, "k2", comment="x")
    assert raised.value.code is ErrorCode.IMPROVE_RUNNING
    release.set()


# ---- activity ----

def test_activity_records_only_fixed_words_for_a_comment(ops_env):
    sup = FakeSupervisor()
    service = _service(ops_env, sup)
    wait_job(service, service.improve(OPERATOR, "k1", max_runs=2, comment=CANARY)["job_id"])
    wait_job(service, service.improve(OPERATOR, "k2", comment=CANARY + "b")["job_id"])
    wait_job(service, service.improve(OPERATOR, "k3", max_runs=2)["job_id"])
    text = ops_env.activity_path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if "improve_requested" in line]
    assert len(lines) == 2
    assert "repeat=yes comment=yes" in lines[0] and "repeat=no comment=yes" in lines[1]
    assert "CANARY" not in text


# ---- comment の不在 (直接経路) ----

def _dump_db(path):
    conn = sqlite3.connect(path)
    out = []
    try:
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for row in conn.execute(f'SELECT * FROM "{name}"'):
                out.append(repr(row))
    finally:
        conn.close()
    return "\n".join(out)


def _reaches(root, needle, limit=300000):
    seen, stack, n = set(), [root], 0
    while stack:
        o = stack.pop()
        if id(o) in seen:
            continue
        seen.add(id(o))
        n += 1
        assert n < limit, "graph too large"
        if isinstance(o, str):
            if needle in o:
                return True
            continue
        if isinstance(o, bytes):
            if needle.encode() in o:
                return True
            continue
        if isinstance(o, (int, float, bool, type(None), type, types.ModuleType)):
            continue
        if isinstance(o, dict):
            stack.extend(o.keys())
            stack.extend(o.values())
        elif isinstance(o, (list, tuple, set, frozenset)):
            stack.extend(o)
        elif callable(o) and getattr(o, "__closure__", None):
            stack.extend(c.cell_contents for c in o.__closure__ if _has(c))
        elif isinstance(o, types.MethodType):
            stack.append(o.__self__)
        elif hasattr(o, "__dict__"):
            stack.append(vars(o))
        elif hasattr(o, "__slots__"):
            stack.extend(getattr(o, s, None) for s in o.__slots__)
    return False


def _has(cell):
    try:
        cell.cell_contents
    except ValueError:
        return False
    return True


@pytest.mark.parametrize("repeat", [True, False])
def test_the_comment_is_nowhere_in_afx_records_before_the_runner_starts(
        ops_env, caplog, repeat):
    caplog.set_level(logging.DEBUG)
    gate = threading.Event()

    class Stop(FakeSupervisor):
        def submit_manual(self, **kwargs):
            gate.wait(10)
            raise RuntimeError("stopped before the runner")

        def submit_repeat(self, **kwargs):
            gate.wait(10)
            raise RuntimeError("stopped before the runner")

    service = _service(ops_env, Stop())
    kwargs = {"max_runs": 2} if repeat else {}
    reply = service.improve(OPERATOR, "k1", comment=CANARY, **kwargs)
    during = service.get_job(OPERATOR, reply["job_id"])
    gate.set()
    final = wait_job(service, reply["job_id"])
    assert final["state"] == "failed"
    for view in (during, final):
        assert CANARY not in json.dumps(view)
    assert CANARY not in _dump_db(ops_env.db_path)
    assert CANARY not in ops_env.activity_path.read_text(encoding="utf-8")
    assert CANARY not in caplog.text
    assert CANARY not in "".join(r.getMessage() for r in caplog.records)
    assert CANARY not in json.dumps(ops_env.conn.execute(
        "SELECT fields_json FROM ops_events").fetchall(), default=str)


@pytest.mark.parametrize("repeat", [True, False])
def test_no_job_reaches_the_comment_after_it_ends(ops_env, repeat):
    kind = "controller error"

    class Fail(FakeSupervisor):
        def submit_manual(self, **kwargs):
            raise RuntimeError(kind)

        def submit_repeat(self, **kwargs):
            raise RuntimeError(kind)

    service = _service(ops_env, Fail())
    kwargs = {"max_runs": 2} if repeat else {}
    comment = "".join(["unique-", "payload-", str(time.monotonic_ns())])
    reply = service.improve(OPERATOR, "k1", comment=comment, **kwargs)
    wait_job(service, reply["job_id"])
    job = service.jobs.lookup(reply["job_id"])
    holder = _holder(service, reply["job_id"])
    assert holder is not None and holder.get() is None
    for cell in job.action.__closure__:
        value = cell.cell_contents
        assert not (isinstance(value, str) and comment in value)
    del comment
    gc.collect()
    assert not _reaches(job, "unique-payload-")


def test_a_successful_end_and_a_cancel_clear_the_holder_too(ops_env):
    def repeat(*, cancel_event, **kw):
        cancel_event.wait(10)
        return RepeatResult("stopped", 0, None)

    sup = FakeSupervisor(repeat)
    service = _service(ops_env, sup)
    ok = service.improve(OPERATOR, "k0", comment=CANARY)
    wait_job(service, ok["job_id"])
    assert _holder(service, ok["job_id"]).get() is None
    reply = service.improve(OPERATOR, "k1", max_runs=2, comment=CANARY)
    assert sup.entered.wait(5)
    assert _holder(service, reply["job_id"]).get() == CANARY
    service.cancel_job(OPERATOR, reply["job_id"], asserted_actor=None)
    wait_job(service, reply["job_id"])
    assert _holder(service, reply["job_id"]).get() is None


def test_a_refused_request_clears_the_holder(ops_env):
    release = threading.Event()
    sup = FakeSupervisor(lambda **kw: release.wait(10) and RepeatResult("max_runs", 1, 1))
    service = _service(ops_env, sup)
    service.improve(OPERATOR, "k1", max_runs=2)
    assert sup.entered.wait(5)
    created = []
    import agentic_fx.core.improve_repeat as ir
    real = ir.CommentHolder

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            created.append(self)

    ir.CommentHolder = Spy
    try:
        with pytest.raises(OpsError):
            service.improve(OPERATOR, "k2", comment=CANARY)
    finally:
        ir.CommentHolder = real
        release.set()
    assert created and all(h.get() is None for h in created)
