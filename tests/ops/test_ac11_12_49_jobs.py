"""AC-11: ask job。AC-12: 手動 improve job と停止。AC-49: 開始時の autopilot 再確認。"""
from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.ops.contracts import ShellCaller
from agentic_fx.ops.jobs import JobRegistry, JobState
from agentic_fx.ops.service import OpsLimits

from .conftest import APPROVER, OPERATOR, digest_of, hold_plugin_lock, wait_job


class _Slot:
    """mission slot の原子的受理を模す。busy の間は None を返す。"""

    def __init__(self):
        self.busy = False
        self.futures: list[concurrent.futures.Future] = []

    def __call__(self, question):
        if self.busy:
            return None
        future = concurrent.futures.Future()
        self.futures.append(future)
        return future


def test_ac11_ask_is_202_then_running_then_done(ops_env):
    slot = _Slot()
    service = ops_env.service(ask_submitter=slot)
    reply = service.ask(OPERATOR, "k", "相場は？")
    assert reply["state"] == "running"
    assert service.get_job(OPERATOR, reply["job_id"])["state"] == "running"
    slot.futures[0].set_result("回答")
    job = wait_job(service, reply["job_id"])
    assert (job["state"], job["result"]) == ("done", {"answer": "回答"})


def test_ac11_full_slot_creates_no_job_and_is_409(ops_env):
    slot = _Slot()
    slot.busy = True
    service = ops_env.service(ask_submitter=slot)
    with pytest.raises(OpsError) as raised:
        service.ask(OPERATOR, "k", "相場は？")
    assert raised.value.code is ErrorCode.MISSION_BUSY
    assert service.jobs._jobs == {}
    slot.busy = False
    running = service.ask(OPERATOR, "k2", "一つ目")
    with pytest.raises(OpsError) as raised:
        service.ask(OPERATOR, "k3", "二つ目")
    assert raised.value.code is ErrorCode.MISSION_BUSY
    assert list(service.jobs._jobs) == [running["job_id"]]
    assert len(slot.futures) == 1


def test_ac11_ask_past_monotonic_deadline_times_out_and_wall_clock_does_not(ops_env):
    slot = _Slot()
    service = ops_env.service(ask_submitter=slot,
                              limits=OpsLimits(ask_deadline_seconds=60))
    reply = service.ask(OPERATOR, "k", "相場は？")
    ops_env.wall.advance(days=2)
    time.sleep(0.3)
    assert service.get_job(OPERATOR, reply["job_id"])["state"] == "running"
    ops_env.mono.advance(61)
    job = wait_job(service, reply["job_id"])
    assert (job["state"], job["result_code"]) == ("failed", "request_timeout")


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


def test_ac12_manual_improve_returns_job_with_mission_id_and_second_is_409(ops_env):
    improve = _Improve()
    service = ops_env.service(improve_supervisor=improve)
    reply = service.improve(OPERATOR, "k1")
    assert improve.started.wait(5)
    assert service.get_job(OPERATOR, reply["job_id"])["result"] == {"mission_id": 17}
    with pytest.raises(OpsError) as raised:
        service.improve(OPERATOR, "k2")
    assert raised.value.code is ErrorCode.IMPROVE_RUNNING
    improve.release.set()
    job = wait_job(service, reply["job_id"])
    assert (job["state"], job["result"]) == ("done", {"mission_id": 17})
    assert improve.calls == 1


def test_ac12_shutdown_stops_intake_ends_queued_and_joins_converged_running(ops_env):
    improve = _Improve()
    service = ops_env.service(improve_supervisor=improve)
    running = service.improve(OPERATOR, "k1")
    assert improve.started.wait(5)
    approval_id = ops_env.make_plugin_approval()
    release = hold_plugin_lock(ops_env.plugins_root, "sma")
    other = ops_env.make_plugin_approval(name="ema", mission_id=2)
    try:
        lock_wait = service.reject(APPROVER, approval_id, "x")
        time.sleep(0.2)
        queued = service.reject(APPROVER, other, "x")

        def finish_later():
            time.sleep(0.3)
            improve.release.set()

        threading.Thread(target=finish_later, daemon=True).start()
        service.shutdown(join_timeout=5)
    finally:
        release()
    with pytest.raises(OpsError) as raised:
        service.add_backlog(OPERATOR, "late", "idea")
    assert raised.value.code is ErrorCode.UNAVAILABLE
    assert service.jobs.lookup(queued["job_id"]).state is JobState.SHUTDOWN
    assert service.jobs.lookup(lock_wait["job_id"]).state is JobState.SHUTDOWN
    # commit 点を越えていた improve は終端まで収束してから join された。
    assert service.jobs.lookup(running["job_id"]).state is JobState.DONE
    assert not any(t.is_alive() for t in service._operation_threads)
    assert ops_env.approval(approval_id)["status"] == "pending"
    assert ops_env.approval(other)["status"] == "pending"


# ------------------------------------------------------------------ AC-49


class _BarrierState:
    """autopilot_at_start の flock 取得直前で止められる StateStore 包み。"""

    def __init__(self, inner):
        self._inner = inner
        self.before_check = threading.Event()
        self.proceed = threading.Event()
        self.proceed.set()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def autopilot_at_start(self, **kwargs):
        self.before_check.set()
        assert self.proceed.wait(10)
        return self._inner.autopilot_at_start(**kwargs)


@pytest.mark.parametrize("kind", ["approve", "retry", "improve"])
def test_ac49_switch_before_start_check_ends_job_without_changes(ops_env, kind):
    state = _BarrierState(ops_env.state)
    improve = _Improve()
    service = ops_env.service(state_store=state, improve_supervisor=improve)
    approval_id = ops_env.make_plugin_approval()
    state.proceed.clear()
    if kind == "improve":
        reply = service.improve(OPERATOR, "k")
    else:
        call = service.approve if kind == "approve" else service.retry
        reply = call(APPROVER, approval_id, digest_of(ops_env, approval_id))
    assert state.before_check.wait(5)
    ops_env.state.update(autopilot=True)
    state.proceed.set()
    job = wait_job(service, reply["job_id"])
    assert (job["state"], job["result_code"]) == ("failed", "autopilot_restricted")
    assert ops_env.approval(approval_id)["status"] == "pending"
    assert ops_env.conn.execute("SELECT COUNT(*) AS n FROM plugin_switch_journal").fetchone()["n"] == 0
    assert not (ops_env.plugins_root / "sma").exists()
    assert improve.calls == 0


def test_ac49_start_check_before_switch_converges_and_starts_nothing_new(ops_env):
    """開始判定が先に通った決定は、切替後も定義済みの終端まで進む。"""
    service = ops_env.service()
    approval_id = ops_env.make_plugin_approval()
    second = ops_env.make_plugin_approval(name="ema", mission_id=2)
    release = hold_plugin_lock(ops_env.plugins_root, "sma")
    try:
        first = service.approve(APPROVER, approval_id, digest_of(ops_env, approval_id))
        queued = service.approve(APPROVER, second, digest_of(ops_env, second))
        time.sleep(0.3)  # 1 件目は開始判定を通って plugin flock 待ち
        ops_env.state.update(autopilot=True)
    finally:
        release()
    assert wait_job(service, first["job_id"])["state"] == "done"
    assert ops_env.approval(approval_id)["status"] == "approved"
    job = wait_job(service, queued["job_id"])
    assert (job["state"], job["result_code"]) == ("failed", "autopilot_restricted")
    assert ops_env.approval(second)["status"] == "pending"


def test_ac49_reject_is_not_rechecked(ops_env):
    service = ops_env.service()
    approval_id = ops_env.make_plugin_approval()
    release = hold_plugin_lock(ops_env.plugins_root, "zzz")
    blocker = ops_env.raw_approval(payload={"name": "zzz"})
    ops_env.conn.execute("UPDATE approval_requests SET kind='plugin' WHERE id=?", (blocker,))
    ops_env.conn.commit()
    try:
        service.reject(APPROVER, blocker, "x")
        reply = service.reject(APPROVER, approval_id, "x")
        ops_env.state.update(autopilot=True)
    finally:
        release()
    assert wait_job(service, reply["job_id"])["state"] == "done"
    assert ops_env.approval(approval_id)["status"] == "rejected"


def test_ac12_manual_improve_is_one_across_principals(ops_env):
    improve = _Improve()
    service = ops_env.service(improve_supervisor=improve)
    reply = service.improve(OPERATOR, "k1")
    assert improve.started.wait(5)
    with pytest.raises(OpsError) as raised:
        service.improve(APPROVER, "k2")
    assert raised.value.code is ErrorCode.IMPROVE_RUNNING
    improve.release.set()
    wait_job(service, reply["job_id"])
    assert improve.calls == 1


def test_terminal_jobs_are_kept_up_to_64_and_for_one_hour():
    now = [0.0]
    registry = JobRegistry(monotonic=lambda: now[0])

    def finished(n):
        job = registry.enqueue_single(OPERATOR, None, f"kind{n}", lambda j: {},
                                      deadline_seconds=60, busy_code=ErrorCode.MISSION_BUSY)
        registry.finish(job, state=JobState.DONE)
        now[0] += 1.0
        return job

    jobs = [finished(n) for n in range(65)]
    assert registry.lookup(jobs[0].id) is None
    assert all(registry.lookup(job.id) is job for job in jobs[1:])
    now[0] += 3601.0
    finished(65)
    assert all(registry.lookup(job.id) is None for job in jobs)


def test_queued_decision_can_be_cancelled_and_never_runs(ops_env):
    service = ops_env.service()
    blocker = ops_env.make_plugin_approval()
    other = ops_env.make_plugin_approval(name="ema", mission_id=2)
    release = hold_plugin_lock(ops_env.plugins_root, "sma")
    try:
        running = service.reject(APPROVER, blocker, "x")
        for _ in range(500):
            if service.jobs.lookup(running["job_id"]).state is JobState.RUNNING:
                break
            time.sleep(0.01)
        queued = service.reject(APPROVER, other, "x")
        assert service.cancel_job(APPROVER, queued["job_id"])["state"] == "cancelled"
    finally:
        release()
    assert wait_job(service, running["job_id"])["state"] == "done"
    assert service.get_job(APPROVER, queued["job_id"])["state"] == "cancelled"
    assert ops_env.approval(other)["status"] == "pending"
    accepted = [r for r in ops_env.ops_rows() if r["phase"] == "accepted"
                and r["job_id"] == queued["job_id"]]
    assert len(accepted) == 1
    ends = [r["result_code"] for r in ops_env.ops_rows()
            if r["target_ref"] == f"audit:{accepted[0]['id']}"]
    assert ends == ["cancelled"]


class _GatedState:
    """autopilot の読み取りで止まり、テストが合図するまで進まない StateStore。"""

    def __init__(self, inner):
        self.inner, self.entered, self.go = inner, threading.Event(), threading.Event()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def load(self, **kwargs):
        self.entered.set()
        assert self.go.wait(10)
        return self.inner.load(**kwargs)


def test_ac12_decision_accepted_while_shutting_down_is_not_left_queued(ops_env):
    gated = _GatedState(ops_env.state)
    service = ops_env.service(state_store=gated)
    approval_id = ops_env.make_plugin_approval()
    outcome = {}

    def submit():
        try:
            outcome["reply"] = service.approve(APPROVER, approval_id,
                                               digest_of(ops_env, approval_id))
        except OpsError as exc:
            outcome["code"] = exc.code

    thread = threading.Thread(target=submit)
    thread.start()
    assert gated.entered.wait(5)
    service.shutdown(join_timeout=1.0)
    gated.go.set()
    thread.join(5)
    assert not thread.is_alive()
    assert outcome == {"code": ErrorCode.UNAVAILABLE}
    assert [j for j in service.jobs._jobs.values() if j.state is JobState.QUEUED] == []
    rows = ops_env.ops_rows()
    assert [(r["phase"], r["result_code"]) for r in rows] == [
        ("accepted", None), ("failed", "unavailable")]
    assert ops_env.approval(approval_id)["status"] == "pending"


@pytest.mark.parametrize("kind", ["ask", "improve"])
def test_ac12_single_job_accepted_while_shutting_down_is_refused(ops_env, kind):
    class _Improve:
        def submit_manual(self, *, on_prepared=None):
            return 1

    gated = _GatedState(ops_env.state)
    service = ops_env.service(state_store=gated, ask_submitter=_Slot(),
                              improve_supervisor=_Improve())
    entered = threading.Event()
    go = threading.Event()
    original = service.jobs.has_active

    def has_active(job_kind):
        entered.set()
        assert go.wait(10)
        return original(job_kind)

    outcome = {}

    def submit():
        try:
            if kind == "ask":
                outcome["reply"] = service.ask(OPERATOR, "k", "q")
            else:
                outcome["reply"] = service.improve(OPERATOR, "k")
        except OpsError as exc:
            outcome["code"] = exc.code

    if kind == "ask":
        service.jobs.has_active = has_active
        wait_entered, release = entered, go
    else:
        wait_entered, release = gated.entered, gated.go
    thread = threading.Thread(target=submit)
    thread.start()
    assert wait_entered.wait(5)
    service.shutdown(join_timeout=1.0)
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert outcome == {"code": ErrorCode.UNAVAILABLE}
    assert [j for j in service.jobs._jobs.values() if j.kind == kind] == []
    rows = ops_env.ops_rows()
    assert [(r["phase"], r["result_code"]) for r in rows] == [
        ("accepted", None), ("failed", "unavailable")]


def test_ac12_request_waiting_for_the_ops_lane_is_refused_after_shutdown(ops_env):
    service = ops_env.service()
    outcome = {}
    service._ops_lock.acquire()

    def submit():
        try:
            service.add_backlog(OPERATOR, "k", "idea", deadline=ops_env.mono() + 5)
        except OpsError as exc:
            outcome["code"] = exc.code

    thread = threading.Thread(target=submit)
    try:
        thread.start()
        time.sleep(0.1)
        service._accepting = False
    finally:
        service._ops_lock.release()
    thread.join(5)
    assert outcome == {"code": ErrorCode.UNAVAILABLE}
    assert ops_env.ops_rows() == []


# 対話シェルは信頼境界内の人間の操作であり、autopilot の制限は API の principal にだけ掛かる。


@pytest.mark.parametrize("kind", ["approve", "retry", "improve"])
def test_shell_job_is_not_stopped_by_autopilot_switched_on_before_its_start(ops_env, kind):
    state = _BarrierState(ops_env.state)
    improve = _Improve()
    improve.release.set()
    service = ops_env.service(state_store=state, improve_supervisor=improve)
    approval_id = ops_env.make_plugin_approval()
    state.proceed.clear()
    if kind == "improve":
        reply = service.improve(ShellCaller.SHELL, "k")
    else:
        call = service.approve if kind == "approve" else service.retry
        reply = call(ShellCaller.SHELL, approval_id, digest_of(ops_env, approval_id))
    ops_env.state.update(autopilot=True)
    state.proceed.set()
    job = wait_job(service, reply["job_id"])
    assert job["result_code"] != "autopilot_restricted", job
    assert not state.before_check.is_set()
    if kind == "improve":
        assert job["state"] == "done" and improve.calls == 1
    if kind == "approve":
        assert job["state"] == "done"
        assert ops_env.approval(approval_id)["status"] == "approved"


def test_shell_requests_are_accepted_while_autopilot_is_on(ops_env):
    improve = _Improve()
    improve.release.set()
    service = ops_env.service(improve_supervisor=improve, data_resume=lambda ack: {"ok": 1})
    approval_id = ops_env.make_plugin_approval()
    ops_env.state.update(autopilot=True, kill_switch_latched=True)
    generation = ops_env.state.load().kill_switch_generation
    shell = ShellCaller.SHELL
    service.add_backlog(shell, "k-bl", "idea")
    service.add_policy(shell, "k-pol", "方針")
    service.resume_data(shell, acknowledge=True)
    service.reset_kill_switch(shell, generation)
    reply = service.approve(shell, approval_id, digest_of(ops_env, approval_id))
    assert wait_job(service, reply["job_id"])["state"] == "done"
    reply = service.improve(shell, "k-imp")
    assert wait_job(service, reply["job_id"])["state"] == "done"
    # job の終端行は done の合図の後に書かれるので、揃うまで待つ。
    deadline = time.monotonic() + 5
    while True:
        codes = [r["result_code"] for r in ops_env.ops_rows() if r["phase"] != "accepted"]
        if len(codes) == 6 or time.monotonic() > deadline:
            break
        time.sleep(0.01)
    assert codes == ["ok"] * 6
