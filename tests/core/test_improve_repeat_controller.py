"""繰り返し controller (`submit_repeat`) と 1 mission executor。"""
from __future__ import annotations

import gc
import json
import logging
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from agentic_fx.core import improve_repeat as ir
from agentic_fx.store import db as db_mod
from agentic_fx.store import missions
from tests.core._improve_repeat_helpers import (
    NOW, Activity, Clock, Mono, Repeat, build, fake_waits, init_db, result)

OK = {}
FAIL = {"result": result("failed")}
EXHAUSTED = {"result": result("failed", failure_class="resource_exhausted")}
FAULT = {"result": result("failed", failure_class="runner_fault")}


@pytest.fixture
def env(tmp_path):
    db = init_db(tmp_path)
    return db, tmp_path


def make(env, script=(), *, default=None, **kw):
    db, tmp = env
    sup, loop = build(db, tmp, script, default=default, **kw)
    mono = Mono()
    waits = fake_waits(sup, mono)
    sup._poll_sec = 30.0
    return sup, loop, mono, waits


def statuses(env):
    conn = db_mod.connect(env[0])
    try:
        return [r["status"] for r in conn.execute(
            "SELECT status FROM missions ORDER BY id")]
    finally:
        conn.close()


def advancing(mono, seconds):
    return {"run": lambda ctx: mono.advance(seconds)}


# ---- AC1: 手動 1 回は従来の経路 ----

def test_submit_manual_without_arguments_calls_prepare_as_before(env):
    sup, loop, *_ = make(env, [OK])
    calls = []
    real = loop.prepare

    def spy(**kw):
        calls.append(sorted(kw))
        return real(**kw)

    loop.prepare = spy
    mission_id = sup.submit_manual()
    assert calls == [["now", "slot_key"]]
    assert isinstance(mission_id, int)
    assert loop.calls == [("commit", mission_id)]


def test_submit_manual_with_a_comment_passes_it_to_prepare(env):
    sup, loop, *_ = make(env, [OK])
    sup.submit_manual(comment="今回だけ")
    assert loop.comments == ["今回だけ"]


def test_submit_manual_does_not_use_the_admission_gate(env):
    sup, loop, *_ = make(env, [OK])
    sup._manual_active = True
    sup._weekly_waiting = 3
    sup.submit_manual()
    assert sup._manual_active is True


def test_submit_manual_still_reraises(env):
    sup, loop, *_ = make(env, [{"run_error": RuntimeError("boom")}])
    with pytest.raises(RuntimeError):
        sup.submit_manual()
    assert statuses(env) == ["failed"]


# ---- AC4: 最低保証 ----

def test_first_run_starts_even_if_the_deadline_has_already_passed(env):
    sup, loop, mono, _ = make(env, [OK])
    rep = Repeat(sup, mono=mono, duration_sec=60)
    calls = []

    def autopilot():
        calls.append(1)
        mono.advance(500)
        return False

    rep.autopilot = autopilot
    res = rep.run()
    assert (res.runs, res.stop_reason) == (1, "deadline")
    assert len(loop.prepared) == 1


def test_long_first_mission_completes_and_nothing_follows(env):
    sup, loop, mono, _ = make(env, [])
    loop.default = advancing(mono, 600)
    res = Repeat(sup, mono=mono, duration_sec=60).run()
    assert (res.runs, res.stop_reason) == (1, "deadline")
    assert statuses(env) == ["completed"]


@pytest.mark.parametrize("which,reason", [
    ("cancel", "stopped"), ("stop", "shutdown"), ("autopilot", "autopilot")])
def test_stop_signals_prevent_even_the_first_run(env, which, reason):
    stop = threading.Event()
    sup, loop, mono, _ = make(env, [OK], stop=stop)
    rep = Repeat(sup, mono=mono, max_runs=3)
    if which == "cancel":
        rep.cancel.set()
    elif which == "stop":
        stop.set()
    else:
        rep.autopilot = lambda: True
    res = rep.run()
    assert (res.runs, res.stop_reason) == (0, reason)
    assert loop.prepared == []
    assert rep.patches[-1] == {"phase": "finished", "stop_reason": reason}


def test_autopilot_is_checked_just_before_every_start_including_the_first(env):
    sup, loop, mono, _ = make(env, [], default=OK)
    count = []
    rep = Repeat(sup, mono=mono, max_runs=2)
    rep.autopilot = lambda: count.append(1) or False
    rep.run()
    assert len(count) >= 4  # 2 回の開始ごとに、gate の前と後で 1 回ずつ


def test_autopilot_turning_true_while_waiting_stops_before_the_start(env):
    sup, loop, mono, _ = make(env, [OK])
    seq = iter([False, True])
    rep = Repeat(sup, mono=mono, max_runs=2)
    rep.autopilot = lambda: next(seq)
    res = rep.run()
    assert (res.runs, res.stop_reason) == (0, "autopilot")
    assert loop.prepared == []
    assert sup._manual_active is False


# ---- AC5: 期限の判定点 ----

def test_one_second_before_the_deadline_starts_and_equality_does_not(env):
    sup, loop, mono, _ = make(env, [])
    loop.script = [advancing(mono, 99), advancing(mono, 1)]
    res = Repeat(sup, mono=mono, duration_sec=100).run()
    assert (res.runs, res.stop_reason) == (2, "deadline")
    assert statuses(env) == ["completed", "completed"]


def test_the_deadline_is_not_checked_in_the_middle_of_a_mission(env):
    sup, loop, mono, _ = make(env, [])
    loop.script = [advancing(mono, 1000)]
    res = Repeat(sup, mono=mono, duration_sec=100).run()
    assert res.runs == 1
    assert statuses(env) == ["completed"]


def test_the_wall_clock_does_not_change_the_start_decision(env):
    clock = Clock()
    sup, loop, mono, _ = make(env, [], clock=clock)
    jump = {"run": lambda ctx: setattr(
        clock, "value", clock.value.replace(year=2035))}
    loop.script = [jump, {"run": lambda ctx: setattr(
        clock, "value", clock.value.replace(year=2001))}, OK]
    res = Repeat(sup, mono=mono, duration_sec=3600, max_runs=3).run()
    assert (res.runs, res.stop_reason) == (3, "max_runs")


def test_deadline_at_is_computed_once_from_the_wall_clock_at_acceptance(env):
    sup, loop, mono, _ = make(env, [], default=OK)
    rep = Repeat(sup, mono=mono, duration_sec=5400, max_runs=3)
    rep.run()
    at = [p["deadline_at"] for p in rep.patches if "deadline_at" in p]
    assert at == ["2026-10-25T04:30:00+00:00"]


def test_explicit_deadline_from_acceptance_is_used_and_not_overwritten(env):
    sup, loop, mono, _ = make(env, [], default=OK)
    rep = Repeat(sup, mono=mono, max_runs=5)
    patches = []
    res = sup.submit_repeat(
        duration_sec=None, max_runs=5, comment_holder=None,
        on_progress=lambda p: patches.append(dict(p)),
        cancel_event=threading.Event(), autopilot_check=lambda: False,
        monotonic=mono, deadline_mono=mono() + 0.0,
        deadline_at="2026-10-25T03:00:00+00:00")
    assert (res.runs, res.stop_reason) == (1, "deadline")
    assert [p["deadline_at"] for p in patches if "deadline_at" in p] == [
        "2026-10-25T03:00:00+00:00"]


def test_real_monotonic_clock_integration(env):
    sup, loop, _mono, _ = make(env, [])
    loop.default = {"run": lambda ctx: time.sleep(0.5)}
    sup._wait_fn = lambda ev, t: ev.wait(t)
    rep = Repeat(sup, mono=time.monotonic, duration_sec=1.2)
    res = rep.run()
    assert res.stop_reason == "deadline"
    assert 2 <= res.runs <= 4


# ---- AC6: 回数 ----

def test_max_runs_and_deadline_whichever_comes_first(env):
    sup, loop, mono, _ = make(env, [])
    loop.default = advancing(mono, 10)
    res = Repeat(sup, mono=mono, duration_sec=3600, max_runs=2).run()
    assert (res.runs, res.stop_reason) == (2, "max_runs")
    sup, loop, mono, _ = make(env, [])
    loop.default = advancing(mono, 1500)
    res = Repeat(sup, mono=mono, duration_sec=3600, max_runs=24).run()
    assert res.stop_reason == "deadline" and res.runs == 3


def test_max_runs_only_has_a_24_hour_deadline(env):
    sup, loop, mono, _ = make(env, [])
    loop.default = advancing(mono, 20000)
    rep = Repeat(sup, mono=mono, max_runs=24)
    res = rep.run()
    assert res.stop_reason == "deadline"
    assert res.runs == 5  # 5 * 20000 > 86400
    assert [p["deadline_at"] for p in rep.patches if "deadline_at" in p] == [
        "2026-10-26T03:00:00+00:00"]


def test_without_duration_the_run_cap_is_24(env):
    sup, loop, mono, _ = make(env, [], default=OK)
    res = Repeat(sup, mono=mono, duration_sec=86400).run()
    assert (res.runs, res.stop_reason) == (24, "max_runs")


def test_failed_missions_count_as_runs(env):
    sup, loop, mono, _ = make(env, [FAIL, OK, FAIL, OK])
    res = Repeat(sup, mono=mono, max_runs=4).run()
    assert (res.runs, res.stop_reason) == (4, "max_runs")


def test_both_none_is_a_value_error(env):
    sup, *_ = make(env)
    with pytest.raises(ValueError):
        Repeat(sup).run()


# ---- AC7: 失敗 ----

def test_two_consecutive_failures_stop(env):
    sup, loop, mono, waits = make(env, [FAIL, FAIL, OK])
    res = Repeat(sup, mono=mono, max_runs=10).run()
    assert (res.runs, res.stop_reason) == (2, "consecutive_failures")
    assert len(loop.prepared) == 2


def test_a_success_resets_the_failure_count(env):
    sup, loop, mono, _ = make(env, [FAIL, OK, FAIL, OK, FAIL, FAIL])
    res = Repeat(sup, mono=mono, max_runs=24).run()
    assert (res.runs, res.stop_reason) == (6, "consecutive_failures")


def test_resource_exhausted_stops_after_one(env):
    sup, loop, mono, waits = make(env, [EXHAUSTED, OK])
    res = Repeat(sup, mono=mono, max_runs=10).run()
    assert (res.runs, res.stop_reason) == (1, "resource_exhausted")
    assert waits == []


def test_runner_fault_is_a_general_failure(env):
    sup, loop, mono, _ = make(env, [FAULT, FAULT, OK])
    res = Repeat(sup, mono=mono, max_runs=10).run()
    assert (res.runs, res.stop_reason) == (2, "consecutive_failures")


def test_resource_exhausted_on_a_timeout_stops_after_one(env):
    step = {"result": result("timeout", failure_class="resource_exhausted"),
            "db_status": "timeout"}
    sup, loop, mono, waits = make(env, [step, OK])
    res = Repeat(sup, mono=mono, max_runs=10).run()
    assert (res.runs, res.stop_reason) == (1, "resource_exhausted")
    assert waits == []


def test_a_class_on_a_completed_mission_does_not_stop(env):
    step = {"result": result("completed", failure_class="resource_exhausted")}
    sup, loop, mono, _ = make(env, [step, step, step])
    res = Repeat(sup, mono=mono, max_runs=3).run()
    assert (res.runs, res.stop_reason) == (3, "max_runs")


def test_max_turns_with_a_class_is_a_general_failure(env):
    step = {"result": result("max_turns", failure_class="resource_exhausted"),
            "db_status": "max_turns"}
    sup, loop, mono, _ = make(env, [step, step])
    res = Repeat(sup, mono=mono, max_runs=10).run()
    assert (res.runs, res.stop_reason) == (2, "consecutive_failures")


@pytest.mark.parametrize("status", ["timeout", "max_turns"])
def test_timeout_and_max_turns_are_general_failures(env, status):
    sup, loop, mono, _ = make(env, [{"result": result(status)}] * 2)
    res = Repeat(sup, mono=mono, max_runs=10).run()
    assert (res.runs, res.stop_reason) == (2, "consecutive_failures")


def test_outcome_status_is_the_db_status_after_commit(env):
    # runner は completed でも commit の判定で failed になり得る
    sup, loop, mono, _ = make(env, [{"db_status": "failed"}] * 2)
    res = Repeat(sup, mono=mono, max_runs=10).run()
    assert (res.runs, res.stop_reason) == (2, "consecutive_failures")
    assert statuses(env) == ["failed", "failed"]


def test_first_run_exception_is_compensated_and_counted(env):
    sup, loop, mono, _ = make(env, [{"run_error": RuntimeError("x")}, OK])
    res = Repeat(sup, mono=mono, max_runs=2).run()
    assert res.runs == 2 and res.stop_reason == "max_runs"
    assert statuses(env) == ["failed", "completed"]
    assert loop.calls[0][0] == "compensate_launch"


def test_prepare_failure_after_tx0_is_a_failed_run(env):
    sup, loop, mono, _ = make(env, [
        {"prepare_fails": "exception"}, {"prepare_fails": "exception"}, OK])
    rep = Repeat(sup, mono=mono, max_runs=5)
    res = rep.run()
    assert (res.runs, res.stop_reason) == (2, "consecutive_failures")
    assert res.last_mission_id is not None
    runs_patches = [p for p in rep.patches if "runs" in p and p["runs"]]
    assert runs_patches[0]["mission_id"] is not None


def test_prepare_exception_without_a_mission_is_a_failure(env):
    sup, loop, mono, _ = make(env, [])
    loop.prepare = lambda **kw: (_ for _ in ()).throw(OSError("no tx0"))
    res = Repeat(sup, mono=mono, max_runs=5).run()
    assert (res.runs, res.stop_reason) == (2, "consecutive_failures")
    assert res.last_mission_id is None


def test_commit_exception_is_compensated_and_counted(env):
    sup, loop, mono, _ = make(env, [{"commit_error": RuntimeError("c")}] * 2)
    res = Repeat(sup, mono=mono, max_runs=5).run()
    assert (res.runs, res.stop_reason) == (2, "consecutive_failures")
    assert statuses(env) == ["failed", "failed"]


# ---- backoff ----

def test_backoff_follows_a_general_failure_and_is_300_seconds(env):
    sup, loop, mono, waits = make(env, [FAIL, OK])
    t0 = mono()
    res = Repeat(sup, mono=mono, max_runs=2).run()
    assert res.runs == 2
    assert waits == [30.0] * 10
    assert mono() - t0 == 300


def test_no_backoff_after_success_or_after_the_stop_decision(env):
    sup, loop, mono, waits = make(env, [OK, OK])
    Repeat(sup, mono=mono, max_runs=2).run()
    assert waits == []
    sup, loop, mono, waits = make(env, [FAIL, FAIL])
    Repeat(sup, mono=mono, max_runs=5).run()
    assert waits == [30.0] * 10  # 1 回目の失敗の後だけ


def test_cancel_wakes_the_backoff(env):
    sup, loop, mono, waits = make(env, [FAIL, OK])
    rep = Repeat(sup, mono=mono, max_runs=5)

    def wait(event, timeout):
        waits.append(timeout)
        mono.advance(timeout)
        if len(waits) == 3:
            rep.cancel.set()

    sup._wait_fn = wait
    res = rep.run()
    assert (res.runs, res.stop_reason) == (1, "stopped")
    assert len(waits) == 3


def test_daemon_stop_wakes_the_backoff(env):
    stop = threading.Event()
    sup, loop, mono, waits = make(env, [FAIL, OK], stop=stop)
    rep = Repeat(sup, mono=mono, max_runs=5)

    def wait(event, timeout):
        waits.append(timeout)
        mono.advance(timeout)
        stop.set()

    sup._wait_fn = wait
    res = rep.run()
    assert (res.runs, res.stop_reason) == (1, "shutdown")


def test_backoff_rechecks_the_deadline(env):
    sup, loop, mono, waits = make(env, [FAIL, OK])
    res = Repeat(sup, mono=mono, duration_sec=100, max_runs=5).run()
    assert (res.runs, res.stop_reason) == (1, "deadline")
    assert 100 <= sum(waits) <= 130


def test_backoff_constant(env):
    assert ir.BACKOFF_SEC == 300


# ---- AC13: shutdown ----

def test_shutdown_before_the_first_run(env):
    stop = threading.Event()
    stop.set()
    sup, loop, mono, _ = make(env, [OK], stop=stop)
    res = Repeat(sup, mono=mono, max_runs=3).run()
    assert (res.runs, res.stop_reason) == (0, "shutdown")
    assert loop.prepared == []


def test_shutdown_while_a_mission_runs_returns_shutdown_not_a_failure(env):
    stop = threading.Event()
    sup, loop, mono, _ = make(env, [
        {"run": lambda ctx: stop.set()}, FAIL], stop=stop)
    res = Repeat(sup, mono=mono, max_runs=5).run()
    assert (res.runs, res.stop_reason) == (1, "shutdown")
    assert len(loop.prepared) == 1
    # DB の mission は既存の終端のまま (ここでは commit された completed)
    assert statuses(env) == ["completed"]


def test_shutdown_before_the_worker_is_ready_keeps_the_existing_compensation(env):
    stop = threading.Event()

    def stop_then_fail(ctx):
        stop.set()

    sup, loop, mono, _ = make(env, [
        {"run": stop_then_fail, "run_error": RuntimeError("worker gone")}],
        stop=stop)
    res = Repeat(sup, mono=mono, max_runs=5).run()
    assert (res.runs, res.stop_reason) == (1, "shutdown")
    assert statuses(env) == ["failed"]


def test_shutdown_during_backoff(env):
    stop = threading.Event()
    sup, loop, mono, waits = make(env, [FAIL, OK], stop=stop)

    def wait(event, timeout):
        waits.append(timeout)
        stop.set()

    sup._wait_fn = wait
    res = Repeat(sup, mono=mono, max_runs=5).run()
    assert (res.runs, res.stop_reason) == (1, "shutdown")
    assert len(waits) == 1


def test_crash_restart_does_not_resume_a_repeat(env):
    db, tmp = env
    sup, loop, mono, _ = make(env, [])
    started = threading.Event()
    release = threading.Event()

    def hold(ctx):
        started.set()
        release.wait(5)
        raise SystemExit  # 異常終了の代わり。補償はされない

    loop.default = {"run": hold}
    t = threading.Thread(target=lambda: _swallow(Repeat(sup, mono=mono, max_runs=5)))
    t.start()
    started.wait(5)
    conn = db_mod.connect(db)
    missions.recover_interrupted(conn, now=NOW, max_requeue=0)
    conn.close()
    assert statuses(env) == ["interrupted"]
    sup2, loop2, *_ = build(db, tmp, [])
    assert loop2.prepared == []
    release.set()
    t.join(5)
    assert loop2.prepared == []


def _swallow(rep):
    try:
        rep.run()
    except BaseException:  # noqa: BLE001
        pass


# ---- callback interface ----

def test_progress_patch_sequence(env):
    sup, loop, mono, _ = make(env, [OK, OK])
    rep = Repeat(sup, mono=mono, max_runs=2)
    res = rep.run()
    p = rep.patches
    assert p[0] == {"phase": "waiting_slot", "runs": 0,
                    "deadline_at": "2026-10-26T03:00:00+00:00"}
    assert p[1] == {"phase": "running", "runs": 1,
                    "mission_id": res.last_mission_id - 1}
    assert p[2] == {"phase": "waiting_slot"}
    assert p[3] == {"phase": "running", "runs": 2,
                    "mission_id": res.last_mission_id}
    assert p[4] == {"phase": "finished", "stop_reason": "max_runs"}
    assert len(p) == 5
    allowed = {"phase", "runs", "mission_id", "deadline_at", "stop_reason"}
    assert all(set(x) <= allowed for x in p)


def test_progress_shows_backoff_phase(env):
    sup, loop, mono, _ = make(env, [FAIL, OK])
    rep = Repeat(sup, mono=mono, max_runs=2)
    rep.run()
    phases = [x.get("phase") for x in rep.patches]
    assert phases == ["waiting_slot", "running", "backoff", "waiting_slot",
                      "running", "finished"]


def test_a_failing_progress_callback_does_not_stop_the_mission(env, caplog):
    sup, loop, mono, _ = make(env, [], default=OK)

    def boom(patch):
        raise RuntimeError("SECRET-IN-PATCH")

    with caplog.at_level(logging.DEBUG):
        res = sup.submit_repeat(
            duration_sec=None, max_runs=2, comment_holder=None, on_progress=boom,
            cancel_event=threading.Event(), autopilot_check=lambda: False,
            monotonic=mono)
    assert res.runs == 2
    assert "SECRET-IN-PATCH" not in caplog.text


def test_cancel_does_not_interrupt_the_running_mission_and_is_not_a_daemon_stop(env):
    stop = threading.Event()
    sup, loop, mono, _ = make(env, [], stop=stop)
    rep = Repeat(sup, mono=mono, max_runs=5)
    loop.default = {"run": lambda ctx: rep.cancel.set()}
    res = rep.run()
    assert (res.runs, res.stop_reason) == (1, "stopped")
    assert statuses(env) == ["completed"]
    assert not stop.is_set()


def test_the_controller_exception_is_reraised_after_cleanup(env):
    sup, loop, mono, _ = make(env, [OK])
    holder = ir.CommentHolder("x")
    rep = Repeat(sup, mono=mono, max_runs=5, holder=holder)

    def boom():
        raise RuntimeError("autopilot read failed")

    rep.autopilot = boom
    with pytest.raises(RuntimeError):
        rep.run()
    assert holder.get() is None
    assert rep.patches[-1] == {"phase": "finished"}
    assert sup._manual_active is False


def test_manual_active_is_released_when_the_mission_machinery_raises(env):
    sup, loop, mono, _ = make(env, [])
    loop.prepare = lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        Repeat(sup, mono=mono, max_runs=2).run()
    assert sup._manual_active is False


def test_finish_writes_a_fixed_activity_line(env):
    act = Activity()
    sup, loop, mono, _ = make(env, [OK], activity=act)
    Repeat(sup, mono=mono, max_runs=1).run()
    assert ("improve_repeat_finished", "reason=max_runs runs=1") in act.rows


# ---- AC14: 通知 ----

def _events(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [(r["ref"], json.loads(r["fields_json"])["state"])
                for r in conn.execute("SELECT ref,fields_json FROM ops_events "
                                      "ORDER BY id")]
    finally:
        conn.close()


def test_each_mission_emits_one_started_and_one_finished_pair(env):
    sup, loop, mono, _ = make(env, [OK, FAIL, OK], emit=True)
    res = Repeat(sup, mono=mono, max_runs=3).run()
    ev = _events(env[0])
    assert len(ev) == 6
    by_ref = {}
    for ref, state in ev:
        by_ref.setdefault(ref, []).append(state)
    assert len(by_ref) == 3
    assert all(v == ["started", "finished"] for v in by_ref.values())
    conn = sqlite3.connect(env[0])
    codes = {r[0] for r in conn.execute("SELECT DISTINCT code FROM ops_events")}
    conn.close()
    assert codes == {"improve_run_changed"}


# ---- AC12: comment ----

CANARY = "".join(["CANARY", "-ONEOFF-", "9d41"])


def _dump_all(db):
    conn = sqlite3.connect(db)
    out = []
    try:
        for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for row in conn.execute(f'SELECT * FROM "{name}"'):
                out.append((name, repr(row)))
    finally:
        conn.close()
    return out


def _reaches(root, needle, limit=200000):
    seen = set()
    stack = [root]
    n = 0
    while stack:
        o = stack.pop()
        if id(o) in seen:
            continue
        seen.add(id(o))
        n += 1
        if n > limit:
            raise AssertionError("graph too large")
        if isinstance(o, (str, bytes)):
            if (isinstance(o, str) and needle in o) or (
                    isinstance(o, bytes) and needle.encode() in o):
                return True
            continue
        if isinstance(o, (int, float, bool, type(None), type, threading.Lock.__class__)):
            continue
        if isinstance(o, dict):
            stack.extend(o.keys()); stack.extend(o.values())
        elif isinstance(o, (list, tuple, set, frozenset)):
            stack.extend(o)
        elif hasattr(o, "__closure__") and getattr(o, "__closure__", None):
            stack.extend(c.cell_contents for c in o.__closure__
                         if _cell_ok(c))
        elif hasattr(o, "__self__") and hasattr(o, "__func__"):
            stack.append(o.__self__)
        elif hasattr(o, "__dict__") and not callable(o) or hasattr(o, "__dict__"):
            stack.append(vars(o))
    return False


def _cell_ok(cell):
    try:
        cell.cell_contents
        return True
    except ValueError:
        return False


def test_comment_is_in_prompts_only_never_in_afx_records(env, caplog):
    act = Activity()
    sup, loop, mono, _ = make(env, [OK, FAIL, OK], activity=act, emit=True)
    holder = ir.CommentHolder(CANARY)
    rep = Repeat(sup, mono=mono, max_runs=3, holder=holder)
    with caplog.at_level(logging.DEBUG):
        rep.run()
    assert loop.comments == [CANARY] * 3  # 全 mission に同じ文
    assert not any(CANARY in repr(r) for _, r in _dump_all(env[0]))
    assert not any(CANARY in d or CANARY in e for e, d in act.rows)
    assert CANARY not in caplog.text
    assert not any(CANARY in repr(p) for p in rep.patches)
    for _, d in act.rows:
        assert "comment" not in d.replace("comment=yes", "")


def test_nothing_reachable_from_the_supervisor_holds_the_comment_afterwards(env):
    sup, loop, mono, _ = make(env, [OK, OK])
    holder = ir.CommentHolder("".join(["CANARY", "-MEM-", "5521"]))
    rep = Repeat(sup, mono=mono, max_runs=2, holder=holder)
    rep.run()
    loop.comments.clear()
    gc.collect()
    assert holder.get() is None
    assert not _reaches(sup, "CANARY-MEM-5521")
    assert not _reaches(rep, "CANARY-MEM-5521")


def test_the_holder_is_cleared_on_every_end(env):
    for kind in ("done", "cancel", "stop"):
        db, tmp = env
        stop = threading.Event()
        sup, loop, mono, _ = make(env, [OK], default=OK, stop=stop)
        holder = ir.CommentHolder("x")
        rep = Repeat(sup, mono=mono, max_runs=2, holder=holder)
        if kind == "cancel":
            rep.cancel.set()
        if kind == "stop":
            stop.set()
        rep.run()
        assert holder.get() is None


def test_a_comment_echoed_by_the_agent_stays_in_the_output_path_only(env):
    """afx が追加で書く場所が無いことの確認。復唱は agent 出力の保存経路に残る。"""
    sup, loop, mono, _ = make(env, [])
    db = env[0]

    loop.default = {"output": lambda: json.dumps({"summary": loop.comments[-1]})}
    holder = ir.CommentHolder(CANARY)
    Repeat(sup, mono=mono, max_runs=2, holder=holder).run()
    hits = [t for t, r in _dump_all(db) if CANARY in r]
    assert hits and set(hits) == {"missions"}


def test_reachability_scan_finds_a_planted_value(env):
    sup, loop, mono, _ = make(env, [OK])
    loop.hidden = {"deep": ["x", ("y", "PLANTED-VALUE-1")]}
    assert _reaches(sup, "PLANTED-VALUE-1")
    planted = "PLANTED-VALUE-2"
    sup._wait_fn = lambda ev, t: planted
    assert _reaches(sup, "PLANTED-VALUE-2")
