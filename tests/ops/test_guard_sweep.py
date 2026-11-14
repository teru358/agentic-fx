"""kill switch の遷移 event: 単一の guard sweep、lane 再入の拒否、例外境界、source scan。

すべて実 sqlite と実 StateStore で動かす。"""
from __future__ import annotations

import ast
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_fx.ops import service as service_mod
from agentic_fx.ops.contracts import ErrorCode, OpsError, Principal
from agentic_fx.ops.events import EventStore
from agentic_fx.ops.service import (GuardLaneNotHeld, GuardLaneReentry, OpsService,
                                    plan_kill_switch_notice)
from agentic_fx.store import db
from agentic_fx.store.state import StateStore

APPROVER = Principal.APPROVER
UTC = timezone.utc
T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[2]


class StateClock:
    def __init__(self, now: datetime) -> None:
        self.t = now

    def now(self) -> datetime:
        return self.t


@pytest.fixture(autouse=True)
def _fresh_backfill_flag(monkeypatch):
    monkeypatch.setattr(service_mod, "_backfill_skip_logged", False)


@pytest.fixture
def rig(ops_env):
    clock = StateClock(T0)
    state = StateStore(ops_env.state._path, clock=clock)
    service = ops_env.service(state_store=state)
    return SimpleNamespace(env=ops_env, clock=clock, state=state, service=service,
                           conn=ops_env.conn)


def _events(conn, guard_kind="kill_switch"):
    rows = conn.execute("SELECT ts,fields_json FROM ops_events "
                        "WHERE code='guard_state_changed' ORDER BY id").fetchall()
    out = []
    for row in rows:
        fields = json.loads(row["fields_json"])
        if fields["guard_kind"] == guard_kind:
            out.append((fields["state"], fields["generation"]))
    return out


def _seed(conn, state, generation, ts):
    conn.execute(
        "INSERT INTO ops_events(ts,code,ref,fields_json) VALUES (?,?,?,?)",
        (ts.isoformat() if isinstance(ts, datetime) else ts, "guard_state_changed", "instance",
         json.dumps({"guard_kind": "kill_switch", "state": state, "generation": generation},
                    sort_keys=True, separators=(",", ":"))))
    conn.commit()


def _sweep(rig, after=None):
    return rig.service.emit_pending_guard_notices(after=after)


def _latch(rig):
    return rig.state.update(kill_switch_latched=True)


def _clear(rig):
    generation = rig.state.load().kill_switch_generation
    return rig.state.reset_kill_switch(generation)


def _warnings(caplog, text):
    return [r.getMessage() for r in caplog.records if r.getMessage() == text]


# ------------------------------------------------------------------ 三規則

def test_latched_without_event_emits_one_latched_and_stays_one(rig):
    _latch(rig)
    assert _sweep(rig) == 1
    assert _sweep(rig) == 0
    assert _events(rig.conn) == [("latched", 1)]


def test_latched_with_latched_event_adds_nothing(rig):
    _latch(rig)
    _seed(rig.conn, "latched", 1, T0)
    assert _sweep(rig) == 0
    assert _events(rig.conn) == [("latched", 1)]


def test_clear_with_released_only_adds_nothing_and_logs_skip_once(rig, caplog):
    """本番と同じ形: released 世代 1 だけ + clear・世代 1・latched_at あり。"""
    _latch(rig)
    _clear(rig)
    _seed(rig.conn, "released", 1, T0 + timedelta(minutes=5))
    with caplog.at_level(logging.WARNING):
        assert _sweep(rig) == 0
        assert _sweep(rig) == 0
    assert _events(rig.conn) == [("released", 1)]
    assert len(_warnings(caplog, "guard_notice_backfill_skipped")) == 1
    shown = rig.state.load()
    assert shown.kill_switch_generation == 1 and shown.kill_switch_latched_at
    ro = db.connect_readonly(rig.env.db_path)
    try:
        assert plan_kill_switch_notice(ro, shown) == (None, 1, True)
    finally:
        ro.close()


def test_clear_with_latched_only_adds_released_only(rig):
    _latch(rig)
    _clear(rig)
    _seed(rig.conn, "latched", 1, T0)
    assert _sweep(rig) == 1
    assert _sweep(rig) == 0
    assert _events(rig.conn) == [("latched", 1), ("released", 1)]


def test_clear_without_any_event_adds_nothing(rig):
    _latch(rig)
    _clear(rig)
    assert _sweep(rig) == 0
    assert _events(rig.conn) == []


def test_released_and_latched_both_present_is_closed_without_skip_log(rig, caplog):
    _latch(rig)
    _clear(rig)
    _seed(rig.conn, "latched", 1, T0)
    _seed(rig.conn, "released", 1, T0)
    with caplog.at_level(logging.WARNING):
        assert _sweep(rig) == 0
    assert _warnings(caplog, "guard_notice_backfill_skipped") == []


def test_never_latched_state_is_ignored(rig):
    assert _sweep(rig) == 0
    assert _events(rig.conn) == []


# ------------------------------------------------------------------ 重複しない

def test_startup_tick_and_reset_never_duplicate_one_state_generation(rig):
    _latch(rig)
    rig.service.emit_pending_notices(improve_enabled=False)  # 起動
    rig.service.emit_pending_notices(improve_enabled=False)  # tick
    rig.service.reset_kill_switch(APPROVER, 1)
    rig.service.emit_pending_notices(improve_enabled=True)
    rig.service.emit_pending_notices(improve_enabled=True)
    assert _events(rig.conn) == [("latched", 1), ("released", 1)]


def test_reset_before_any_tick_emits_latched_then_released(rig):
    _latch(rig)
    rig.service.reset_kill_switch(APPROVER, 1)
    assert _events(rig.conn) == [("latched", 1), ("released", 1)]
    rig.service.emit_pending_notices(improve_enabled=False)
    assert _events(rig.conn) == [("latched", 1), ("released", 1)]


def test_relatch_right_after_reset_emits_released_n_then_latched_n_plus_1(rig):
    _latch(rig)
    rig.service.emit_pending_notices(improve_enabled=False)
    real = rig.state.reset_kill_switch

    def reset_then_relatch(*args, **kwargs):
        result = real(*args, **kwargs)
        rig.clock.t = T0 + timedelta(minutes=1)
        rig.state.update(kill_switch_latched=True)
        return result

    rig.state.reset_kill_switch = reset_then_relatch
    rig.service.reset_kill_switch(APPROVER, 1)
    assert _events(rig.conn) == [("latched", 1), ("released", 1), ("latched", 2)]
    rig.service.emit_pending_notices(improve_enabled=False)
    assert _events(rig.conn) == [("latched", 1), ("released", 1), ("latched", 2)]


def test_wrapper_after_state_is_swept_before_the_current_state(rig):
    """shell の経路: 解除後の state (after) を先に、次世代の現 state を後に処理する。"""
    _latch(rig)
    _seed(rig.conn, "latched", 1, T0)
    cleared = _clear(rig)
    rig.clock.t = T0 + timedelta(minutes=1)
    rig.state.update(kill_switch_latched=True)  # 世代 2
    assert _sweep(rig, after=cleared) == 2
    assert _events(rig.conn) == [("latched", 1), ("released", 1), ("latched", 2)]
    assert _sweep(rig, after=cleared) == 0


def _write_marker(rig, generation=1):
    marker = Path(str(rig.state._path) + ".reset-in-progress")
    marker.write_text(json.dumps({"kind": "kill_switch_reset",
                                  "requested_generation": generation,
                                  "started_at": T0.isoformat()}))
    return marker


def test_marker_projects_latched_and_reconcile_does_not_duplicate(rig):
    _latch(rig)
    _write_marker(rig)
    assert rig.state.load().kill_switch_latched is True
    assert rig.state.file_value().kill_switch_latched is True
    assert _sweep(rig) == 1
    rig.service.reconcile_kill_switch(APPROVER)
    rig.service.emit_pending_notices(improve_enabled=False)
    assert _events(rig.conn) == [("latched", 1)]


def test_marker_over_cleared_file_does_not_emit_released(rig):
    _latch(rig)
    _seed(rig.conn, "latched", 1, T0)
    _clear(rig)
    _write_marker(rig)
    assert rig.state.file_value().kill_switch_latched is False
    assert rig.state.load().kill_switch_latched is True
    assert _sweep(rig) == 0
    assert _events(rig.conn) == [("latched", 1)]


def test_reconcile_with_unemitted_latch_emits_one_latched(rig):
    _latch(rig)
    _write_marker(rig)
    rig.service.reconcile_kill_switch(APPROVER)
    assert _events(rig.conn) == [("latched", 1)]


# ------------------------------------------------------------------ 時刻

def test_ts_is_clamped_to_latched_at_when_wall_clock_goes_back(rig):
    _latch(rig)
    assert rig.env.wall() < T0  # wall clock は latched_at より前
    for _ in range(3):
        _sweep(rig)
        rig.service.emit_pending_notices(improve_enabled=False)
    rows = rig.conn.execute("SELECT ts FROM ops_events WHERE code='guard_state_changed'").fetchall()
    assert [r["ts"] for r in rows] == [T0.isoformat()]


def test_ts_follows_wall_clock_when_it_is_after_latched_at(rig):
    _latch(rig)
    rig.env.wall.now = T0 + timedelta(hours=2)
    _sweep(rig)
    row = rig.conn.execute("SELECT ts FROM ops_events").fetchone()
    assert row["ts"] == (T0 + timedelta(hours=2)).isoformat()


def test_event_timestamps_in_other_zones_are_compared_as_datetimes(rig):
    _latch(rig)
    _seed(rig.conn, "latched", 1, "2026-11-01T09:00:00+09:00")  # == latched_at
    assert _sweep(rig) == 0
    assert _events(rig.conn) == [("latched", 1)]


def test_event_just_before_latched_at_belongs_to_an_older_incarnation(rig):
    _latch(rig)
    _seed(rig.conn, "latched", 1, "2026-11-01T08:59:59+09:00")
    assert _sweep(rig) == 1
    assert _events(rig.conn) == [("latched", 1), ("latched", 1)]
    assert _sweep(rig) == 0


def test_unparseable_event_ts_never_counts_as_a_match(rig):
    _latch(rig)
    _seed(rig.conn, "latched", 1, "not-a-time")
    assert _sweep(rig) == 1


def test_emit_not_before_rejects_naive_and_non_utc(ops_env):
    store = EventStore(ops_env.conn, wall_clock=ops_env.wall)
    fields = {"guard_kind": "kill_switch", "state": "latched", "generation": 1}
    for bad in (datetime(2026, 11, 1), datetime(2026, 11, 1, tzinfo=timezone(timedelta(hours=9))),
                "2026-11-01T00:00:00+00:00"):
        with pytest.raises(ValueError):
            store.emit("guard_state_changed", "instance", fields, not_before=bad)
        with pytest.raises(ValueError):
            store.emit_best_effort("guard_state_changed", "instance", fields, not_before=bad)
    assert ops_env.conn.execute("SELECT COUNT(*) FROM ops_events").fetchone()[0] == 0


def test_emit_not_before_takes_the_later_of_wall_clock_and_floor(ops_env):
    store = EventStore(ops_env.conn, wall_clock=ops_env.wall)
    fields = {"guard_kind": "kill_switch", "state": "latched", "generation": 1}
    floor = ops_env.wall() + timedelta(days=1)
    store.emit("guard_state_changed", "instance", fields, not_before=floor)
    store.emit("guard_state_changed", "instance", fields,
               not_before=ops_env.wall() - timedelta(days=1))
    store.emit("guard_state_changed", "instance", fields)
    stamps = [r["ts"] for r in ops_env.conn.execute("SELECT ts FROM ops_events ORDER BY id")]
    assert stamps == [floor.isoformat(), ops_env.wall().isoformat(), ops_env.wall().isoformat()]


# ------------------------------------------------------------------ 例外境界

def _terminal(env):
    row = env.conn.execute(
        "SELECT phase,result_code,response_json FROM ops_requests "
        "WHERE endpoint LIKE '%killswitch/reset' ORDER BY id DESC LIMIT 1").fetchone()
    return (row["phase"], row["result_code"], row["response_json"])


@pytest.mark.parametrize("failing_call", [0, 1, 2])
def test_select_busy_at_any_step_does_not_change_reset(rig, caplog, monkeypatch, failing_call):
    _latch(rig)
    calls = {"n": 0}
    real = service_mod.plan_kill_switch_notice

    def flaky(conn, state):
        n = calls["n"]
        calls["n"] += 1
        if n == failing_call:
            raise sqlite3.OperationalError("database is locked secret-detail")
        return real(conn, state)

    monkeypatch.setattr(service_mod, "plan_kill_switch_notice", flaky)
    with caplog.at_level(logging.WARNING):
        response = rig.service.reset_kill_switch(APPROVER, 1)
    assert response == {"latched": False, "generation": 1}
    assert rig.state.load().kill_switch_latched is False
    assert _terminal(rig.env)[:2] == ("succeeded", "ok")
    assert calls["n"] == 3  # 失敗した段のあとも残りの段は回る
    assert len(_warnings(caplog, "guard_notice_deferred")) == 1
    assert all("secret-detail" not in r.getMessage() for r in caplog.records)
    assert all(r.exc_info is None for r in caplog.records)


def test_insert_failure_does_not_change_reset_and_logs_one_line(rig, caplog, monkeypatch):
    _latch(rig)

    def broken(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error secret-detail")

    monkeypatch.setattr(EventStore, "emit", broken)
    with caplog.at_level(logging.WARNING):
        response = rig.service.reset_kill_switch(APPROVER, 1)
    assert response == {"latched": False, "generation": 1}
    assert _terminal(rig.env)[:2] == ("succeeded", "ok")
    assert _events(rig.conn) == []
    assert len(_warnings(caplog, "guard_notice_deferred")) == 1


def test_state_read_failure_keeps_the_returned_state_sweep(rig, caplog, monkeypatch):
    _latch(rig)
    _seed(rig.conn, "latched", 1, T0)

    def unreadable(self):
        raise OSError("cannot read secret-detail")

    monkeypatch.setattr(OpsService, "_current_state", unreadable)
    with caplog.at_level(logging.WARNING):
        response = rig.service.reset_kill_switch(APPROVER, 1)
    assert response == {"latched": False, "generation": 1}
    assert _terminal(rig.env)[:2] == ("succeeded", "ok")
    assert _events(rig.conn) == [("latched", 1), ("released", 1)]  # 戻り値の段は動く
    assert len(_warnings(caplog, "guard_notice_deferred")) == 1


def test_failures_keep_error_mapping_and_audit_of_a_refused_reset(rig, caplog, monkeypatch):
    _latch(rig)

    def busy(conn, state):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service_mod, "plan_kill_switch_notice", busy)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(OpsError) as raised:
            rig.service.reset_kill_switch(APPROVER, 99)
    assert raised.value.code is ErrorCode.GENERATION_MISMATCH
    assert _terminal(rig.env)[:2] == ("failed", "generation_mismatch")
    assert len(_warnings(caplog, "guard_notice_deferred")) == 1
    assert rig.state.load().kill_switch_latched is True


def test_failed_pre_sweep_still_lets_the_mutation_run_and_loses_clear_history(rig, monkeypatch):
    """前後の sweep が共に失敗すると、clear + event 無しは補完されない (受容済みの欠落)。"""
    _latch(rig)

    def busy(conn, state):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service_mod, "plan_kill_switch_notice", busy)
    rig.service.reset_kill_switch(APPROVER, 1)
    assert rig.state.load().kill_switch_latched is False
    assert _events(rig.conn) == []


def test_reconcile_failures_do_not_change_result(rig, caplog, monkeypatch):
    _latch(rig)
    _write_marker(rig)

    def busy(conn, state):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(service_mod, "plan_kill_switch_notice", busy)
    with caplog.at_level(logging.WARNING):
        response = rig.service.reconcile_kill_switch(APPROVER)
    assert response == {"latched": True, "generation": 1}
    assert not Path(str(rig.state._path) + ".reset-in-progress").exists()
    row = rig.conn.execute("SELECT phase,result_code FROM ops_requests "
                           "WHERE endpoint LIKE '%killswitch/reconcile'").fetchall()[-1]
    assert (row["phase"], row["result_code"]) == ("succeeded", "ok")
    assert len(_warnings(caplog, "guard_notice_deferred")) == 1


def test_failure_leaves_no_open_transaction_and_next_sweep_recovers(rig, monkeypatch):
    _latch(rig)
    state = {"fail": True}
    real = EventStore.emit

    def flaky(self, *args, **kwargs):
        if state["fail"]:
            raise sqlite3.OperationalError("disk I/O error")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(EventStore, "emit", flaky)
    assert _sweep(rig) == 0
    assert not rig.conn.in_transaction
    state["fail"] = False
    assert _sweep(rig) == 1
    assert _events(rig.conn) == [("latched", 1)]


# ------------------------------------------------------------------ 再入

class _Spy:
    def __init__(self, service):
        self.acquire = self.sleep = 0
        real_acquire, real_sleep = service._acquire, service._sleep_until

        def acquire(*args, **kwargs):
            self.acquire += 1
            return real_acquire(*args, **kwargs)

        def sleep(*args, **kwargs):
            self.sleep += 1
            return real_sleep(*args, **kwargs)

        service._acquire, service._sleep_until = acquire, sleep


def _reenter_from_reset(rig, call):
    """reset の action の中 (lane 保持中) から `call` を呼び、結果を返す。"""
    seen = {}
    real = rig.state.reset_kill_switch

    def hook(*args, **kwargs):
        spy = _Spy(rig.service)
        before = (spy.acquire, spy.sleep)
        try:
            call()
        except BaseException as exc:  # noqa: BLE001
            seen["exc"] = exc
        seen["calls"] = (spy.acquire - before[0], spy.sleep - before[1])
        return real(*args, **kwargs)

    rig.state.reset_kill_switch = hook
    response = rig.service.reset_kill_switch(APPROVER, 1)
    return seen, response


def test_wrapper_called_from_a_lane_holder_is_refused_at_once(rig):
    _latch(rig)
    seen, response = _reenter_from_reset(rig, rig.service.emit_pending_guard_notices)
    assert isinstance(seen["exc"], GuardLaneReentry)
    assert seen["exc"].code == "guard_lane_reentry" and str(seen["exc"]) == "guard_lane_reentry"
    assert seen["calls"] == (0, 0)  # 取得も sleep も deadline 待ちもしない
    assert response == {"latched": False, "generation": 1}
    assert _terminal(rig.env)[:2] == ("succeeded", "ok")
    assert _events(rig.conn) == [("latched", 1), ("released", 1)]  # 二重 INSERT なし


@pytest.mark.parametrize("call", [
    lambda svc: svc.events_after(APPROVER, after=0, limit=5),
    lambda svc: svc.emit_pending_notices(improve_enabled=True),
    lambda svc: svc.emit_pending_improve_notices(enabled=True),
], ids=["events_after", "tick_sweep", "improve_sweep"])
def test_other_lane_entries_are_refused_by_the_shared_check(rig, call):
    _latch(rig)
    seen, response = _reenter_from_reset(rig, lambda: call(rig.service))
    assert isinstance(seen["exc"], GuardLaneReentry)
    assert seen["calls"] == (0, 0)
    assert response == {"latched": False, "generation": 1}


def test_ownership_clears_after_an_exception_and_a_later_wrapper_works(rig):
    _latch(rig)
    real = rig.state.reset_kill_switch

    def boom(*args, **kwargs):
        raise RuntimeError("injected")

    rig.state.reset_kill_switch = boom
    with pytest.raises(RuntimeError):
        rig.service.reset_kill_switch(APPROVER, 1)
    assert rig.service._lane_owned() is False
    rig.state.reset_kill_switch = real
    assert _sweep(rig) == 0  # pre-sweep が既に latched を出している
    assert _events(rig.conn) == [("latched", 1)]
    assert rig.service._lane_owned() is False


def test_private_helpers_refuse_to_run_without_the_lane(rig):
    _latch(rig)
    with pytest.raises(GuardLaneNotHeld):
        rig.service._sweep_kill_switch(rig.state.load())
    with pytest.raises(GuardLaneNotHeld):
        rig.service._guard_notices_in_lane()
    assert _events(rig.conn) == []


def test_private_helper_works_inside_the_lane(rig):
    _latch(rig)
    with rig.service._ops_lane(rig.service._deadline(None)):
        assert rig.service._sweep_kill_switch(rig.state.load()) == 1
    assert _events(rig.conn) == [("latched", 1)]


def test_other_thread_contention_still_waits_and_times_out_normally(rig):
    _latch(rig)
    held, release = threading.Event(), threading.Event()

    def holder():
        with rig.service._ops_lane(rig.service._deadline(None)):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(5)
    try:
        spy = _Spy(rig.service)
        with pytest.raises(OpsError) as raised:
            rig.service.emit_pending_guard_notices(deadline=rig.service._monotonic() + 0.2)
        assert raised.value.code is ErrorCode.REQUEST_TIMEOUT
        assert spy.acquire == 1 and spy.sleep >= 1  # 従来どおり待った
    finally:
        release.set()
        thread.join(5)
    assert _sweep(rig) == 1


# ------------------------------------------------------------------ improve との分離

def test_guard_runs_when_improve_raises_and_when_it_is_disabled(rig, monkeypatch, caplog):
    _latch(rig)
    spy = _Spy(rig.service)

    def broken(self):
        raise RuntimeError("improve blew up")

    monkeypatch.setattr(OpsService, "_emit_pending_improve_notices", broken)
    with caplog.at_level(logging.WARNING):
        rig.service.emit_pending_notices(improve_enabled=True)
    assert spy.acquire == 1  # lane は 1 回だけ
    assert _events(rig.conn) == [("latched", 1)]
    assert "improve_notice_skipped reason=outbox_failed" in [r.getMessage() for r in caplog.records]
    _clear(rig)
    calls = []
    monkeypatch.setattr(OpsService, "_emit_pending_improve_notices",
                        lambda self: calls.append(1) or 0)
    rig.service.emit_pending_notices(improve_enabled=False)
    assert calls == []
    assert _events(rig.conn) == [("latched", 1), ("released", 1)]


def test_improve_runs_even_when_the_guard_sweep_raises(rig, monkeypatch):
    calls = []
    monkeypatch.setattr(OpsService, "_emit_pending_improve_notices",
                        lambda self: calls.append(1) or 0)

    def broken(self, after=None):
        raise RuntimeError("guard blew up")

    monkeypatch.setattr(OpsService, "_guard_notices_in_lane", broken)
    rig.service.emit_pending_notices(improve_enabled=True)
    assert calls == [1]
    assert not rig.service._lane_owned()


def test_tick_hook_runs_the_guard_sweep_regardless_of_improve_setting(rig):
    from agentic_fx import service as svc
    _latch(rig)
    app = SimpleNamespace(ops=rig.service, settings=SimpleNamespace(
        notify=SimpleNamespace(improve_events=False)))
    svc._sweep_improve_notices(app)
    svc._sweep_improve_notices(app)
    assert _events(rig.conn) == [("latched", 1)]


def test_lane_timeout_defers_the_tick_sweep_without_error(rig):
    _latch(rig)
    held, release = threading.Event(), threading.Event()

    def holder():
        with rig.service._ops_lane(rig.service._deadline(None)):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(5)
    try:
        real_deadline = rig.service._deadline
        rig.service._deadline = lambda d=None: rig.service._monotonic() + 0.2
        rig.service.emit_pending_notices(improve_enabled=True)  # 例外を出さない
        rig.service._deadline = real_deadline
    finally:
        release.set()
        thread.join(5)
    assert _events(rig.conn) == []
    rig.service.emit_pending_notices(improve_enabled=True)
    assert _events(rig.conn) == [("latched", 1)]


# ------------------------------------------------------------------ 形

def test_producer_emits_exactly_three_fields(rig):
    _latch(rig)
    rig.service.reset_kill_switch(APPROVER, 1)
    rows = rig.conn.execute("SELECT code,ref,fields_json FROM ops_events "
                            "WHERE code='guard_state_changed'").fetchall()
    assert len(rows) == 2
    for row in rows:
        assert (row["code"], row["ref"]) == ("guard_state_changed", "instance")
        assert set(json.loads(row["fields_json"])) == {"guard_kind", "state", "generation"}


def test_pending_view_uses_a_read_only_connection_and_closed_generation_is_false(rig):
    _latch(rig)
    ro = db.connect_readonly(rig.env.db_path)
    try:
        assert plan_kill_switch_notice(ro, rig.state.load()) == ("latched", 1, False)
        _seed(rig.conn, "latched", 1, T0)
        assert plan_kill_switch_notice(ro, rig.state.load()) == (None, 1, False)
        _clear(rig)
        assert plan_kill_switch_notice(ro, rig.state.load()) == ("released", 1, False)
        _seed(rig.conn, "released", 1, T0)
        assert plan_kill_switch_notice(ro, rig.state.load()) == (None, 1, False)
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO ops_events(ts,code,ref,fields_json) VALUES ('a','b','c','{}')")
    finally:
        ro.close()


# ------------------------------------------------------------------ source scan

_SRC = REPO / "src" / "agentic_fx"
# trade event の sink は別接続で trade の code だけを書く。kill switch は書かない。
_RAW_INSERT_OWNERS = {"events.py", "trade_event_sink.py"}
_PRODUCERS = {"emit", "emit_best_effort", "_guard_event"}


def _enclosing_functions(tree):
    """node -> それを含む関数名 (無ければ None) の対応。"""
    owner = {}

    def walk(node, current):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                else current
            owner[child] = name
            walk(child, name)

    walk(tree, None)
    return owner


def _kill_switch_producers(root: Path):
    """kill switch の guard event を作りうる呼び出し (file, 関数名)。"""
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name in _PRODUCERS and _mentions_kill_switch(node):
                    found.append((path.name, owner.get(node)))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and "INSERT INTO ops_events" in node.value \
                    and path.name not in _RAW_INSERT_OWNERS:
                found.append((path.name, owner.get(node)))
    return found


def _mentions_kill_switch(call: ast.Call) -> bool:
    for sub in ast.walk(call):
        if isinstance(sub, ast.Constant) and sub.value == "kill_switch":
            return True
    return False


def test_only_the_sweep_helper_inserts_kill_switch_events():
    found = _kill_switch_producers(_SRC)
    assert found == [("service.py", "_sweep_kill_switch")]


def test_scan_flags_other_connections_and_improve_supervisor_but_not_data_feed(tmp_path):
    sources = {
        "decide.py": "def f(self):\n    self._decide_events.emit_best_effort("
                     "'guard_state_changed','instance',{'guard_kind':'kill_switch'})\n",
        "supervisor.py": "def g(events):\n    events.emit('guard_state_changed','instance',"
                         "{'guard_kind': 'kill_switch', 'state': 'latched'})\n",
        "feed.py": "def h(self):\n    self._guard_event('data_feed','resume_requested',None)\n",
        "raw.py": "def k(conn):\n    conn.execute(\"INSERT INTO ops_events(ts) VALUES (1)\")\n",
    }
    for name, text in sources.items():
        (tmp_path / name).write_text(text)
    assert _kill_switch_producers(tmp_path) == [
        ("decide.py", "f"), ("raw.py", "k"), ("supervisor.py", "g")]


def test_every_guard_event_literal_lives_in_events_or_service():
    hits = set()
    for path in sorted((REPO / "src").rglob("*.py")):
        if "guard_state_changed" in path.read_text(encoding="utf-8"):
            hits.add(path.name)
    assert hits <= {"events.py", "service.py"}


# ------------------------------------------------------------------ shell の前 sweep

class _Recorder:
    """公開 wrapper を実 service へ委譲しつつ、呼出し時点の state と event を記録する。"""

    def __init__(self, rig, error=None) -> None:
        self.rig = rig
        self.error = error
        self.calls = []

    def emit_pending_guard_notices(self, after=None, **kwargs):
        self.calls.append((after, self.rig.state.load().kill_switch_latched,
                           _events(self.rig.conn)))
        if self.error is not None:
            raise self.error
        return self.rig.service.emit_pending_guard_notices(after=after, **kwargs)


def _shell(rig, recorder=None):
    from unittest.mock import MagicMock
    from agentic_fx.activity import ActivityLog
    from agentic_fx.commands import Commands
    cmds = Commands(conn=rig.conn, state_store=rig.state, broker=MagicMock(),
                    trade_loop=MagicMock(), activity=ActivityLog(rig.env.activity_path),
                    log_dir=rig.env.root / "logs", clock=rig.clock)
    cmds.ops_service = recorder if recorder is not None else rig.service
    return cmds


def test_shell_reset_before_the_first_tick_emits_latched_then_released(rig):
    _latch(rig)
    rec = _Recorder(rig)
    out = _shell(rig, rec).dispatch("killswitch reset 1")
    assert out == "kill switch ラッチを解除しました"
    assert _events(rig.conn) == [("latched", 1), ("released", 1)]
    (before_after, before_latched, before_events), (post_after, _, _) = rec.calls
    assert before_after is None and before_latched is True and before_events == []
    assert post_after is not None and post_after.kill_switch_latched is False


def test_shell_reconcile_confirm_emits_the_unsent_latched_before_the_new_generation(rig):
    _latch(rig)
    _clear(rig)
    (rig.env.root / "data" / "app_state.json.reset-in-progress").write_text(
        json.dumps({"kind": "kill_switch_reset", "requested_generation": 1,
                    "started_at": "T"}))
    rec = _Recorder(rig)
    out = _shell(rig, rec).dispatch("killswitch reconcile confirm")
    assert "ラッチ中として確定しました" in out
    assert _events(rig.conn) == [("latched", 1), ("latched", 2)]
    assert rec.calls[0][0] is None and rec.calls[0][2] == []
    assert rec.calls[1][0].kill_switch_generation == 2


def test_shell_reset_survives_a_failing_before_call(rig, caplog):
    plain = _shell(rig)
    _latch(rig)
    base_out = plain.dispatch("killswitch reset 1")
    base_acts = rig.env.activity_path.read_text()
    rig.env.activity_path.write_text("")
    _latch(rig)
    rec = _Recorder(rig, OpsError(ErrorCode.REQUEST_TIMEOUT))
    with caplog.at_level(logging.WARNING):
        out = _shell(rig, rec).dispatch("killswitch reset 2")
    assert out == base_out == "kill switch ラッチを解除しました"
    assert rig.state.load().kill_switch_latched is False
    new_acts = rig.env.activity_path.read_text()
    def _reset_lines(text):
        return [l.split(" ", 2)[-1].replace("gen=2", "gen=1") for l in text.splitlines()
                if "kill_switch_reset" in l]
    assert _reset_lines(new_acts) == _reset_lines(base_acts) != []
    assert len(rec.calls) == 2
    assert _warnings(caplog, "guard_notice_deferred") == ["guard_notice_deferred"] * 2
