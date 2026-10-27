"""実 JobRegistry + 実 OpsService + 実 ImproveSupervisor (mission は FakeLoop) の統合。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from agentic_fx.ops.jobs import JobState
from agentic_fx.store import db as db_mod
from agentic_fx.store import improve_waves
from tests.core._improve_repeat_helpers import (
    NOW, PERIOD, Mono, build, fake_waits, result)

from .conftest import OPERATOR, wait_job

T = 10.0
OK = {}
FAIL = {"result": result("failed")}


class Rig:
    def __init__(self, env, script=(), *, default=None, stop=None, slots=0, emit=False):
        if slots:
            conn = db_mod.connect(env.db_path)
            try:
                improve_waves.create_wave_and_slots(
                    conn, period_key=PERIOD, now=NOW, expected=slots, commit=True)
            finally:
                conn.close()
        self.env = env
        self.stop = stop or threading.Event()
        self.sup, self.loop = build(env.db_path, env.root, script, default=default,
                                    stop=self.stop, emit=emit)
        self.sup._poll_sec = 0.02
        self.service = env.service(improve_supervisor=self.sup, stop_event=self.stop)

    def fast_waits(self):
        """待ちを (実時間ではなく) 注入の単調時計の前進に置き換える。"""
        return fake_waits(self.sup, self.env.mono)

    def statuses(self):
        conn = db_mod.connect(self.env.db_path)
        try:
            return [r["status"] for r in conn.execute(
                "SELECT status FROM missions ORDER BY id")]
        finally:
            conn.close()

    def job(self, job_id):
        return self.service.get_job(OPERATOR, job_id)


@pytest.fixture
def rig(ops_env):
    return lambda *a, **k: Rig(ops_env, *a, **k)


def busy_spy(sup):
    """週次が空くのを待っている (繰り返しが gate の前で止まっている) と立つ Event。"""
    busy = threading.Event()
    real = sup._weekly_busy_locked

    def spy(warned):
        answer = real(warned)
        if answer:
            busy.set()
        return answer

    sup._weekly_busy_locked = spy
    return busy


def wait_for(predicate, what="condition"):
    deadline = time.monotonic() + T
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(what)


# ---- 開始と終端 ----

def test_a_repeat_runs_to_max_runs_and_publishes_a_complete_final_result(rig):
    r = rig([OK, OK])
    reply = r.service.improve(OPERATOR, "k1", max_runs=2)
    job = wait_job(r.service, reply["job_id"])
    assert job["state"] == "done"
    assert job["result"]["stop_reason"] == "max_runs"
    assert job["result"]["runs"] == 2 and job["result"]["phase"] == "finished"
    assert job["result"]["mission_id"] == r.loop.prepared[-1].mission_id
    assert job["result"]["max_runs"] == 2 and job["result"]["stop_requested"] is False
    assert r.statuses() == ["completed", "completed"]
    assert [c for c in r.loop.comments] == [None, None]


def test_a_comment_reaches_every_mission_prompt_input(rig):
    r = rig([OK, OK, OK])
    reply = r.service.improve(OPERATOR, "k1", max_runs=3, comment=" 慎重に  進める ")
    wait_job(r.service, reply["job_id"])
    assert r.loop.comments == ["慎重に 進める"] * 3


def test_a_comment_only_request_passes_it_to_one_mission_without_the_gate(rig):
    r = rig([OK])
    with r.sup._gate:
        reply = r.service.improve(OPERATOR, "k1", comment="一回だけ")
        job = wait_job(r.service, reply["job_id"])
    assert job["state"] == "done"
    assert r.loop.comments == ["一回だけ"]
    assert job["result"] == {"mission_id": r.loop.prepared[0].mission_id}


def test_the_progress_is_visible_while_a_mission_runs(rig):
    inside, release = threading.Event(), threading.Event()

    def hold(ctx):
        inside.set()
        release.wait(T)

    r = rig([{"run": hold}, OK])
    reply = r.service.improve(OPERATOR, "k1", max_runs=2)
    assert inside.wait(T)
    wait_for(lambda: r.job(reply["job_id"])["result"]["runs"] == 1, "runs")
    view = r.job(reply["job_id"])
    assert view["state"] == "running"
    assert view["result"]["phase"] == "running"
    assert view["result"]["mission_id"] == r.loop.prepared[0].mission_id
    release.set()
    assert wait_job(r.service, reply["job_id"])["result"]["runs"] == 2


# ---- cancel ----

def test_cancel_before_the_first_mission_ends_stopped_with_zero_runs(rig):
    r = rig([OK])
    started = []
    r.service._start_thread = lambda job: started.append(job)
    reply = r.service.improve(OPERATOR, "k1", max_runs=3)
    answer = r.service.cancel_job(OPERATOR, reply["job_id"], asserted_actor=None)
    assert answer["state"] == "running" and answer["stop_requested"] is True
    real = type(r.service)._start_thread
    real(r.service, started[0])
    job = wait_job(r.service, reply["job_id"])
    assert (job["state"], job["result"]["stop_reason"], job["result"]["runs"]) == (
        "done", "stopped", 0)
    assert r.loop.prepared == []


def test_cancel_during_a_mission_lets_it_finish_and_stops_the_next(rig):
    inside, release = threading.Event(), threading.Event()

    def hold(ctx):
        inside.set()
        release.wait(T)

    r = rig([{"run": hold}, OK])
    reply = r.service.improve(OPERATOR, "k1", max_runs=3)
    assert inside.wait(T)
    answer = r.service.cancel_job(OPERATOR, reply["job_id"], asserted_actor=None)
    assert answer == {"id": reply["job_id"], "state": "running", "stop_requested": True}
    assert r.service.cancel_job(OPERATOR, reply["job_id"], asserted_actor=None) == answer
    view = r.job(reply["job_id"])
    assert view["state"] == "running" and view["result"]["stop_requested"] is True
    job = r.service.jobs.lookup(reply["job_id"])
    # 利用者の停止要求と daemon の停止は別の Event。cancel は走行中の mission を止めない。
    assert job.cancel_event is not r.stop and job.cancel_event is not r.service.stop_event
    assert not r.stop.is_set()
    time.sleep(0.2)
    assert r.job(reply["job_id"])["state"] == "running"
    assert r.statuses() == ["running"]
    release.set()
    done = wait_job(r.service, reply["job_id"])
    assert (done["state"], done["result"]["stop_reason"], done["result"]["runs"]) == (
        "done", "stopped", 1)
    assert r.statuses() == ["completed"]
    assert len(r.loop.prepared) == 1


def test_cancel_is_effective_while_waiting_for_a_weekly_slot(rig):
    r = rig(slots=1)
    busy = busy_spy(r.sup)
    with r.sup._gate:
        r.sup._owned_slots.add((PERIOD, 0))
    reply = r.service.improve(OPERATOR, "k1", max_runs=3)
    assert busy.wait(T)
    assert r.job(reply["job_id"])["result"]["phase"] == "waiting_slot"
    r.service.cancel_job(OPERATOR, reply["job_id"], asserted_actor=None)
    job = wait_job(r.service, reply["job_id"])
    assert (job["result"]["stop_reason"], job["result"]["runs"]) == ("stopped", 0)
    assert r.sup._manual_active is False
    assert r.loop.prepared == []


def test_a_repeat_and_a_weekly_slot_never_overlap(rig):
    running, spans = [0], []
    lock = threading.Lock()

    def span(ctx):
        with lock:
            running[0] += 1
            spans.append(running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1

    r = rig(default={"run": span}, slots=1)
    with r.sup._gate:
        r.sup._owned_slots.add((PERIOD, 0))
    reply = r.service.improve(OPERATOR, "k1", max_runs=3)
    time.sleep(0.1)
    weekly = threading.Thread(target=r.sup._run_slot_thread, args=(PERIOD, 0), daemon=True)
    weekly.start()
    wait_job(r.service, reply["job_id"])
    weekly.join(T)
    assert max(spans) == 1 and len(spans) == 4


# ---- 停止 (daemon) ----

def test_shutdown_before_the_first_mission_ends_the_controller_with_shutdown(
        rig, monkeypatch):
    stop = threading.Event()
    r = rig([OK], stop=stop)
    monkeypatch.setattr(r.service, "_precheck_improve", lambda job: None)
    stop.set()
    reply = r.service.improve(OPERATOR, "k1", max_runs=2)
    job = wait_job(r.service, reply["job_id"])
    assert (job["state"], job["result"]["stop_reason"], job["result"]["runs"]) == (
        "done", "shutdown", 0)
    assert r.loop.prepared == []


def test_graceful_shutdown_while_a_job_waits_to_start_ends_with_shutdown(rig):
    stop = threading.Event()
    r = rig([OK], stop=stop)
    started = []
    r.service._start_thread = lambda job: started.append(job)
    reply = r.service.improve(OPERATOR, "k1", max_runs=2)
    stop.set()
    type(r.service)._start_thread(r.service, started[0])
    job = wait_job(r.service, reply["job_id"])
    # 事前確認の段階で停止を見るので、controller には入らない。
    assert job["state"] == JobState.SHUTDOWN.value
    assert r.loop.prepared == []


def test_graceful_shutdown_with_the_controller_waiting_ends_done_shutdown(rig):
    stop = threading.Event()
    r = rig(slots=1, stop=stop)
    busy = busy_spy(r.sup)
    with r.sup._gate:
        r.sup._owned_slots.add((PERIOD, 0))
    reply = r.service.improve(OPERATOR, "k1", max_runs=2)
    assert busy.wait(T)
    stop.set()
    with r.sup._gate:
        r.sup._gate.notify_all()
    job = wait_job(r.service, reply["job_id"])
    assert (job["state"], job["result"]["stop_reason"], job["result"]["runs"]) == (
        "done", "shutdown", 0)


def test_graceful_shutdown_during_a_mission_lets_it_finish_then_stops(rig):
    stop = threading.Event()
    inside, release = threading.Event(), threading.Event()

    def hold(ctx):
        inside.set()
        release.wait(T)

    r = rig([{"run": hold}, OK], stop=stop)
    reply = r.service.improve(OPERATOR, "k1", max_runs=3)
    assert inside.wait(T)
    stop.set()
    time.sleep(0.1)
    assert r.job(reply["job_id"])["state"] == "running"
    release.set()
    job = wait_job(r.service, reply["job_id"])
    assert (job["state"], job["result"]["stop_reason"], job["result"]["runs"]) == (
        "done", "shutdown", 1)
    assert len(r.loop.prepared) == 1


def test_graceful_shutdown_during_backoff_ends_with_shutdown_not_a_failure(rig):
    stop = threading.Event()
    r = rig([FAIL, OK], stop=stop)

    def wait(event, timeout):
        stop.set()

    r.sup._wait_fn = wait
    reply = r.service.improve(OPERATOR, "k1", max_runs=5)
    job = wait_job(r.service, reply["job_id"])
    assert (job["state"], job["result"]["stop_reason"], job["result"]["runs"]) == (
        "done", "shutdown", 1)
    assert len(r.loop.prepared) == 1


# ---- 失敗系の終端 ----

def test_two_consecutive_failures_end_the_job_as_done_with_the_reason(rig):
    r = rig([FAIL, FAIL, OK])
    r.fast_waits()
    reply = r.service.improve(OPERATOR, "k1", max_runs=5)
    job = wait_job(r.service, reply["job_id"])
    assert (job["state"], job["result"]["stop_reason"], job["result"]["runs"]) == (
        "done", "consecutive_failures", 2)


def test_a_controller_exception_fails_the_job_and_keeps_the_last_progress(rig, monkeypatch):
    r = rig([OK])

    def broken(*a, **k):
        raise RuntimeError("controller broke")

    monkeypatch.setattr(r.sup, "_admit_manual", broken)
    reply = r.service.improve(OPERATOR, "k1", max_runs=3, comment="x-comment")
    job = wait_job(r.service, reply["job_id"])
    assert (job["state"], job["result_code"]) == ("failed", "internal")
    assert job["result"]["phase"] == "finished" and "stop_reason" not in job["result"]
    assert r.sup._manual_active is False


# ---- 通知 ----

def test_each_mission_emits_one_started_finished_pair_and_no_repeat_event(rig, ops_env):
    r = rig([OK, FAIL, OK], emit=True)
    r.fast_waits()
    reply = r.service.improve(OPERATOR, "k1", max_runs=3)
    wait_job(r.service, reply["job_id"])
    conn = sqlite3.connect(ops_env.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [(x["code"], x["ref"], x["fields_json"]) for x in conn.execute(
            "SELECT code,ref,fields_json FROM ops_events ORDER BY id")]
    finally:
        conn.close()
    improve = [(ref, json.loads(f)["state"]) for code, ref, f in rows
               if code == "improve_run_changed"]
    by_ref = {}
    for ref, state in improve:
        by_ref.setdefault(ref, []).append(state)
    assert len(by_ref) == 3 and all(v == ["started", "finished"] for v in by_ref.values())
    assert {code for code, *_ in rows} <= {"improve_run_changed", "job_state_changed",
                                          "ops_request_terminal"}
    mine = [json.loads(f) for code, ref, f in rows
            if code == "job_state_changed" and ref == f"job:{reply['job_id']}"]
    assert [m["state"] for m in mine] == ["done"]


def test_cancel_emits_no_job_event_while_the_job_keeps_running(rig, ops_env):
    inside, release = threading.Event(), threading.Event()

    def hold(ctx):
        inside.set()
        release.wait(T)

    r = rig([{"run": hold}])
    reply = r.service.improve(OPERATOR, "k1", max_runs=2)
    assert inside.wait(T)
    r.service.cancel_job(OPERATOR, reply["job_id"], asserted_actor=None)
    count = ops_env.conn.execute(
        "SELECT count(*) FROM ops_events WHERE code='job_state_changed' AND ref=?",
        (f"job:{reply['job_id']}",)).fetchone()[0]
    assert count == 0
    release.set()
    wait_job(r.service, reply["job_id"])


# ---- 受理時の期限 ----

def test_the_deadline_is_judged_on_the_monotonic_clock_fixed_at_acceptance(rig, ops_env):
    r = rig(default=OK)
    r.fast_waits()
    reply = r.service.improve(OPERATOR, "k1", duration_sec=600, max_runs=24)
    ops_env.wall.advance(days=3)
    job = wait_job(r.service, reply["job_id"])
    assert job["state"] == "done"
    assert job["result"]["stop_reason"] == "max_runs"
    assert job["result"]["runs"] == 24


def test_a_deadline_in_the_monotonic_past_stops_after_the_first_mission(rig, ops_env):
    def advance(ctx):
        ops_env.mono.advance(601)

    r = rig(default={"run": advance})
    reply = r.service.improve(OPERATOR, "k1", duration_sec=600)
    job = wait_job(r.service, reply["job_id"])
    assert (job["result"]["stop_reason"], job["result"]["runs"]) == ("deadline", 1)
