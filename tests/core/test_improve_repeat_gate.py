"""admission gate: 繰り返しの mission と週次 slot の claim の線形化。"""
from __future__ import annotations

import logging
import sqlite3
import threading

import pytest

from agentic_fx.core import improve_repeat as ir
from agentic_fx.store import db as db_mod
from agentic_fx.store import improve_waves
from tests.core._improve_repeat_helpers import (
    NOW, PERIOD, Mono, Repeat, build, init_db, result)

T = 10.0


class Log:
    def __init__(self):
        self.items = []
        self._lock = threading.Lock()

    def add(self, item):
        with self._lock:
            self.items.append(item)


def wait_until(sup, predicate):
    with sup._gate:
        assert sup._gate.wait_for(predicate, T), "condition not reached"


def slot_status(db, k=0):
    conn = db_mod.connect(db)
    try:
        return conn.execute(
            "SELECT status FROM improve_wave_slots WHERE wave_period_key=? AND k=?",
            (PERIOD, k)).fetchone()["status"]
    finally:
        conn.close()


def own(sup, k=0):
    with sup._gate:
        sup._owned_slots.add((PERIOD, k))


def spawn_weekly(sup, k=0):
    t = threading.Thread(target=sup._run_slot_thread, args=(PERIOD, k), daemon=True)
    t.start()
    return t


def fresh(tmp_path, *, slots=1, script=(), default=None, **kw):
    db = init_db(tmp_path, slots=slots)
    sup, loop = build(db, tmp_path, script, default=default, **kw)
    sup._poll_sec = 0.02
    return db, sup, loop


def overlapped(log_items):
    """('start', who) / ('end', who) の列で、mission が重なったか。"""
    running = 0
    for kind, _who in log_items:
        running += 1 if kind == "start" else -1
        if running > 1:
            return True
    return False


def logged(log, who, extra=None):
    def run(ctx):
        log.add(("start", who))
        if extra is not None:
            extra(ctx)
        log.add(("end", who))
    return {"run": run}


def busy_spy(sup):
    """`_weekly_busy_locked` が真を返したら立つ Event。"""
    busy = threading.Event()
    real = sup._weekly_busy_locked

    def spy(warned):
        r = real(warned)
        if r:
            busy.set()
        return r

    sup._weekly_busy_locked = spy
    return busy


# ---- すべての mission が gate を通る / 保持範囲 ----

def test_every_repeat_mission_goes_through_the_gate_including_the_first(tmp_path):
    db, sup, loop = fresh(tmp_path, slots=0, default={})
    calls = []
    real = sup._admit_manual
    sup._admit_manual = lambda check: calls.append(1) or real(check)
    res = Repeat(sup, max_runs=3).run()
    assert res.runs == 3 and len(calls) == 3


def test_the_gate_lock_is_free_while_the_runner_runs(tmp_path):
    db, sup, loop = fresh(tmp_path, slots=0)
    seen = {}

    def hook(ctx):
        got = []
        t = threading.Thread(target=lambda: got.append(
            sup._gate.acquire(timeout=T)) or sup._gate.release())
        t.start()
        t.join(T)
        seen["acquired"] = got == [True]
        seen["active"] = sup._manual_active

    loop.default = {"run": hook}
    Repeat(sup, max_runs=1).run()
    assert seen == {"acquired": True, "active": True}
    assert sup._manual_active is False


# ---- 週次との排他 ----

def test_repeat_waits_for_an_owned_reserved_slot_then_runs_after_it(tmp_path):
    log = Log()
    db, sup, loop = fresh(tmp_path, default=logged(log, "repeat"))
    loop.script = [logged(log, "weekly")]
    own(sup)
    busy = threading.Event()
    real = sup._weekly_busy_locked

    def spy(warned):
        r = real(warned)
        if r:
            busy.set()
        return r

    sup._weekly_busy_locked = spy
    rep = Repeat(sup, max_runs=1)
    rt = threading.Thread(target=rep.run, daemon=True)
    rt.start()
    assert busy.wait(T)
    assert loop.prepared == []
    wt = spawn_weekly(sup)
    wt.join(T)
    rt.join(T)
    assert [w for _, w in log.items] == ["weekly", "weekly", "repeat", "repeat"]
    assert slot_status(db) == "done"


@pytest.mark.parametrize("point", ["before_claim", "before_repeat_gate"])
def test_no_overlap_when_the_race_is_forced_at_each_point(tmp_path, point):
    for trial in range(8):
        sub = tmp_path / f"t{trial}"
        sub.mkdir()
        log = Log()
        db, sup, loop = fresh(sub, default=logged(log, "repeat"))
        loop.script = [logged(log, "weekly")]
        own(sup)
        barrier = threading.Barrier(2)
        if point == "before_claim":
            def pre_claim(slot_key):
                if slot_key is not None:
                    barrier.wait(T)
            loop.prepare_hook = pre_claim
            rep = Repeat(sup, max_runs=1)
            rt = threading.Thread(target=rep.run, daemon=True)
            wt = spawn_weekly(sup)
            # 週次が gate を保持して claim の直前で待っている間に繰り返しを始める
            rt.start()
            barrier.wait(T)
        else:
            real = sup._admit_manual

            def gated(check):
                barrier.wait(T)
                return real(check)

            sup._admit_manual = gated
            rep = Repeat(sup, max_runs=1)
            rt = threading.Thread(target=rep.run, daemon=True)
            wt = threading.Thread(
                target=lambda: (barrier.wait(T), sup._run_slot_thread(PERIOD, 0)),
                daemon=True)
            rt.start()
            wt.start()
        wt.join(T)
        rt.join(T)
        assert not wt.is_alive() and not rt.is_alive()
        assert not overlapped(log.items), (trial, log.items)
        assert sorted(w for _, w in log.items) == [
            "repeat", "repeat", "weekly", "weekly"]


def test_the_weekly_claim_transaction_completes_while_the_gate_is_held(tmp_path):
    db, sup, loop = fresh(tmp_path, default={})
    own(sup)
    seen = {}

    def pre_claim(slot_key):
        if slot_key is None:
            return
        free = sup._gate.acquire(blocking=False)
        if free:
            sup._gate.release()
        seen["held_before_claim"] = not free

    def post_claim(slot_key):
        if slot_key is None:
            return
        free = sup._gate.acquire(blocking=False)
        if free:
            sup._gate.release()
        seen["free_after_claim"] = free
        seen["status_after_claim"] = slot_status(db)

    loop.prepare_hook = pre_claim
    loop.claimed_hook = post_claim
    spawn_weekly(sup).join(T)
    assert seen == {"held_before_claim": True, "free_after_claim": True,
                    "status_after_claim": "claimed"}


def test_weekly_waits_while_a_repeat_mission_is_active(tmp_path):
    log = Log()
    db, sup, loop = fresh(tmp_path)
    release = threading.Event()
    started = threading.Event()

    def hold(ctx):
        started.set()
        release.wait(T)

    loop.script = [{"run": hold}, {}]
    rep = Repeat(sup, max_runs=1)
    rt = threading.Thread(target=rep.run, daemon=True)
    rt.start()
    assert started.wait(T)
    own(sup)
    wt = spawn_weekly(sup)
    wait_until(sup, lambda: sup._weekly_waiting == 1)
    assert [p.slot_key for p in loop.prepared] == [None]  # claim も start もまだ
    assert slot_status(db) == "reserved"
    release.set()
    rt.join(T)
    wt.join(T)
    assert slot_status(db) == "done"


def test_a_waiting_weekly_start_has_priority_over_the_next_repeat_mission(tmp_path):
    log = Log()
    db, sup, loop = fresh(tmp_path)
    release = threading.Event()
    started = threading.Event()

    def first(ctx):
        log.add(("start", "r1"))
        started.set()
        release.wait(T)
        log.add(("end", "r1"))

    loop.script = [{"run": first}, logged(log, "weekly"), logged(log, "r2")]
    rep = Repeat(sup, max_runs=2)
    rt = threading.Thread(target=rep.run, daemon=True)
    rt.start()
    assert started.wait(T)
    own(sup)
    wt = spawn_weekly(sup)
    wait_until(sup, lambda: sup._weekly_waiting == 1)
    release.set()
    rt.join(T)
    wt.join(T)
    assert [w for k, w in log.items if k == "start"] == ["r1", "weekly", "r2"]
    assert not overlapped(log.items)


def test_tick_capacity_is_not_affected_by_the_gate_or_repeat_missions(tmp_path, monkeypatch):
    db, sup, loop = fresh(tmp_path, slots=0)
    started = []
    monkeypatch.setattr(sup, "_spawn_slot_thread", lambda pk, k: started.append((pk, k)))
    sup._manual_active = True
    conn = db_mod.connect(db)
    assert sup._running_slot_count(conn) == 0
    conn.close()
    sup.tick(NOW)
    assert len(started) == 1


def test_tick_registers_ownership_inside_the_gate_lock_with_slot_creation(
        tmp_path, monkeypatch):
    db, sup, loop = fresh(tmp_path, slots=0)
    monkeypatch.setattr(sup, "_spawn_slot_thread", lambda pk, k: None)
    seen = {}
    real = improve_waves.create_wave_and_slots

    def spy(conn, **kw):
        free = sup._gate.acquire(blocking=False)
        if free:
            sup._gate.release()
        seen["locked"] = not free
        return real(conn, **kw)

    monkeypatch.setattr(improve_waves, "create_wave_and_slots", spy)
    sup.tick(NOW)
    assert seen["locked"] is True
    assert len(sup._owned_slots) == 1


# ---- 待ちの間の停止・期限 ----

def test_cancel_and_daemon_stop_work_while_waiting_for_the_gate(tmp_path):
    for kind in ("cancel", "stop"):
        sub = tmp_path / kind
        sub.mkdir()
        stop = threading.Event()
        db, sup, loop = fresh(sub, stop=stop)
        own(sup)
        busy = busy_spy(sup)
        rep = Repeat(sup, max_runs=3)
        out = []
        rt = threading.Thread(target=lambda: out.append(rep.run()), daemon=True)
        rt.start()
        assert busy.wait(T)
        if kind == "cancel":
            rep.cancel.set()
        else:
            stop.set()
            sup.shutdown()
        rt.join(T)
        assert out[0].runs == 0
        assert out[0].stop_reason == ("stopped" if kind == "cancel" else "shutdown")
        assert sup._manual_active is False


def test_the_deadline_applies_while_waiting_after_the_first_run(tmp_path):
    db, sup, loop = fresh(tmp_path, default={})
    mono = Mono()
    busy = busy_spy(sup)
    loop.script = [{"run": lambda ctx: own(sup)}]
    rep = Repeat(sup, mono=mono, duration_sec=100, max_runs=5)
    out = []
    rt = threading.Thread(target=lambda: out.append(rep.run()), daemon=True)
    rt.start()
    assert busy.wait(T)       # 2 回目は所有中の slot を待っている
    mono.advance(100)         # 待つ間に期限が来る
    rt.join(T)
    assert out[0].runs == 1 and out[0].stop_reason == "deadline"
    assert len(loop.prepared) == 1


def test_the_first_run_ignores_the_deadline_and_waits_for_the_slot(tmp_path):
    db, sup, loop = fresh(tmp_path, default={})
    mono = Mono()
    own(sup)
    busy = busy_spy(sup)
    rep = Repeat(sup, mono=mono, duration_sec=60)
    out = []
    rt = threading.Thread(target=lambda: out.append(rep.run()), daemon=True)
    rt.start()
    assert busy.wait(T)
    mono.advance(10000)       # 待つ間に期限を大きく過ぎる
    assert rt.is_alive() and loop.prepared == []
    spawn_weekly(sup).join(T)
    rt.join(T)
    assert out[0].runs == 1
    assert slot_status(db) == "done"


# ---- 孤児収束 ----

def test_spawn_failure_fails_the_slot_and_releases_ownership(tmp_path, monkeypatch):
    db, sup, loop = fresh(tmp_path, slots=0, default={})
    real_start = threading.Thread.start

    def start(self):
        if self.name.startswith("afx-improve-slot-"):
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    sup.tick(NOW)
    conn = db_mod.connect(db)
    rows = conn.execute("SELECT status FROM improve_wave_slots").fetchall()
    conn.close()
    assert [r["status"] for r in rows] == ["failed"]
    assert sup._owned_slots == set()
    assert sup._active_threads == []
    res = Repeat(sup, max_runs=1).run()
    assert res.runs == 1


def test_exception_before_the_claim_fails_the_slot(tmp_path):
    db, sup, loop = fresh(tmp_path, default={})
    own(sup)

    def boom(slot_key):
        raise RuntimeError("before claim")

    loop.prepare_hook = boom
    spawn_weekly(sup).join(T)
    assert slot_status(db) == "failed"
    assert sup._owned_slots == set()
    res = Repeat(sup, max_runs=1).run()
    assert res.runs == 1


def test_pre_ready_compensation_failure_still_fails_the_slot(tmp_path):
    db, sup, loop = fresh(tmp_path, default={})
    own(sup)
    loop.script = [{"ready": False, "result": result("failed"),
                    "commit_error": RuntimeError("c"),
                    "compensation_error": RuntimeError("comp")}]
    spawn_weekly(sup).join(T)
    assert slot_status(db) == "failed"
    assert sup._owned_slots == set()
    assert Repeat(sup, max_runs=1).run().runs == 1


def test_a_normal_slot_thread_leaves_a_terminal_slot_and_no_ownership(tmp_path):
    db, sup, loop = fresh(tmp_path, default={})
    own(sup)
    spawn_weekly(sup).join(T)
    assert slot_status(db) == "done"
    assert sup._owned_slots == set()


def test_unowned_open_slot_is_not_waited_for_and_warns_with_a_fixed_word(
        tmp_path, caplog):
    db, sup, loop = fresh(tmp_path, default={})
    with caplog.at_level(logging.WARNING):
        res = Repeat(sup, max_runs=1).run()
    assert res.runs == 1
    assert "improve_repeat_unowned_open_slot" in caplog.text
    assert PERIOD not in caplog.text


def test_shutdown_wakes_a_weekly_start_waiting_for_the_gate(tmp_path):
    stop = threading.Event()
    db, sup, loop = fresh(tmp_path, default={}, stop=stop)
    own(sup)
    sup._manual_active = True
    wt = spawn_weekly(sup)
    wait_until(sup, lambda: sup._weekly_waiting == 1)
    stop.set()
    sup.shutdown()
    wt.join(T)
    assert not wt.is_alive()
    assert slot_status(db) == "failed"
    assert loop.prepared == []


def test_manual_active_is_lowered_in_a_finally(tmp_path):
    db, sup, loop = fresh(tmp_path, slots=0)
    loop.default = {"run_error": RuntimeError("x")}
    Repeat(sup, max_runs=1).run()
    assert sup._manual_active is False


@pytest.mark.parametrize("status", ["reserved", "claimed", "running"])
def test_repeat_waits_for_an_owned_slot_in_each_open_state(tmp_path, status):
    db, sup, loop = fresh(tmp_path, default={})
    conn = db_mod.connect(db)
    conn.execute("UPDATE improve_wave_slots SET status=? WHERE wave_period_key=?",
                 (status, PERIOD))
    conn.commit()
    conn.close()
    own(sup)
    busy = busy_spy(sup)
    rep = Repeat(sup, max_runs=1)
    out = []
    rt = threading.Thread(target=lambda: out.append(rep.run()), daemon=True)
    rt.start()
    assert busy.wait(T)
    assert loop.prepared == [] and sup._manual_active is False
    conn = db_mod.connect(db)
    conn.execute("UPDATE improve_wave_slots SET status='failed' "
                 "WHERE wave_period_key=?", (PERIOD,))
    conn.commit()
    conn.close()
    with sup._gate:
        sup._owned_slots.discard((PERIOD, 0))
        sup._gate.notify_all()
    rt.join(T)
    assert out[0].runs == 1


def test_a_terminal_owned_slot_does_not_block_the_repeat(tmp_path):
    db, sup, loop = fresh(tmp_path, default={})
    conn = db_mod.connect(db)
    conn.execute("UPDATE improve_wave_slots SET status='done'")
    conn.commit()
    conn.close()
    own(sup)
    assert Repeat(sup, max_runs=1).run().runs == 1


def test_a_waiting_weekly_start_blocks_admission_even_without_an_open_slot(tmp_path):
    db, sup, loop = fresh(tmp_path, slots=0)
    calls = []
    admitted = []
    stop_after = threading.Event()

    def check():
        calls.append(1)
        return "stopped" if stop_after.is_set() else None

    with sup._gate:
        sup._weekly_waiting = 1
    t = threading.Thread(target=lambda: admitted.append(sup._admit_manual(check)),
                         daemon=True)
    t.start()
    with sup._gate:
        spun = sup._gate.wait_for(lambda: len(calls) >= 5 or admitted, 2.0)
    assert admitted == [] and sup._manual_active is False and len(calls) >= 5
    assert spun
    with sup._gate:
        sup._weekly_waiting = 0
        sup._gate.notify_all()
    t.join(T)
    assert admitted == [None] and sup._manual_active is True
