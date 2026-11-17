"""開場 tick の約定停止 (ラッチ中・DD 閾値以上・state 読取不能) の挙動。"""
import fcntl
import json
import os
import time
from datetime import timedelta

import pytest

import agentic_fx.store.state as state_module
from agentic_fx.core import accounting
from agentic_fx.core.contracts import Bar, BrokerResult, OrderStatus as S
from agentic_fx.store import orders, snapshots
from agentic_fx.store.state import StateLockTimeout, StateStore
from tests.core.test_scheduler import SETTINGS, SAT, WED, Env

REACH = dict(o=148.30, h=148.35, l=148.15, c=148.25)


def _bar(ts, **kw):
    v = {**REACH, **kw}
    return Bar("USDJPY", "1m", ts, v["o"], v["h"], v["l"], v["c"], 100)


def _events(env):
    path = env.tmp_path / "a.log"
    if not path.exists():
        return []
    return [line.split("\t")[2] for line in
            path.read_text(encoding="utf-8").splitlines() if line]


def _latch(env):
    env.state.update(kill_switch_latched=True)


def _state_bytes(env):
    p = env.tmp_path / "state.json"
    return p.read_bytes() if p.exists() else None


def _with_threshold(env, pct):
    risk = SETTINGS.risk.model_copy(update={"drawdown_kill_pct": pct})
    env.sched.settings = SETTINGS.model_copy(update={"risk": risk})


def _set_equity(env, equity):
    env.executor.broker.equity = lambda: (equity, equity)


def _tick(env, minutes=1):
    env.bars["USDJPY"] = _bar(WED + timedelta(minutes=minutes - 1))
    env.sched.tick(WED + timedelta(minutes=minutes))


def _swing_open(env):
    return orders.insert(
        env.conn, pair="USDJPY", direction="long", entry_type="market",
        horizon="swing", status="open", now=WED, quantity=0.1,
        avg_fill_price=148.20, stop_loss=147.80, take_profit=149.00)


def _status(env, oid):
    return orders.get(env.conn, oid)["status"]


def test_latched_state_cancels_every_pending_and_blocks_the_fill(tmp_path):
    env = Env(tmp_path)
    first = env.place_limit(price=148.20)
    second = env.place_limit(price=148.10)
    _latch(env)
    _tick(env)
    for oid in (first, second):
        row = orders.get(env.conn, oid)
        assert row["status"] == "cancelled"
        assert row["close_reason"] == "kill_switch"
    names = _events(env)
    assert names.count("kill_switch_pending_cancel") == 1
    assert "2 件" in (tmp_path / "a.log").read_text(encoding="utf-8")


def test_latched_state_without_pending_writes_no_cancel_activity(tmp_path):
    env = Env(tmp_path)
    _latch(env)
    _tick(env)
    assert "kill_switch_pending_cancel" not in _events(env)
    assert "kill_switch_fill_guard_unavailable" not in _events(env)


def test_cancel_activity_is_written_only_when_there_was_something_to_cancel(
        tmp_path):
    env = Env(tmp_path)
    env.place_limit(price=148.20)
    _latch(env)
    _tick(env, 1)
    _tick(env, 2)
    assert _events(env).count("kill_switch_pending_cancel") == 1


def test_pending_ahead_of_the_latch_is_treated_as_before_the_latch(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    _tick(env, 1)
    assert _status(env, oid) == "open"
    _latch(env)
    _tick(env, 2)
    assert _status(env, oid) == "open"


@pytest.mark.parametrize("raw_latched", [False, True])
def test_reset_marker_projects_latched_without_touching_the_state(
        tmp_path, raw_latched):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    if raw_latched:
        _latch(env)
    else:
        env.state.update(initialized=True)
    marker = tmp_path / ("state.json" + state_module._MARKER_SUFFIX)
    marker.write_text(json.dumps({"kind": "kill_switch_reset",
                                  "requested_generation": 1}), encoding="utf-8")
    before = _state_bytes(env)
    _tick(env)
    row = orders.get(env.conn, oid)
    assert (row["status"], row["close_reason"]) == ("cancelled", "kill_switch")
    assert _state_bytes(env) == before
    assert marker.exists()
    raw = json.loads(before)
    assert raw["kill_switch_latched"] is raw_latched


def test_scheduler_does_not_write_state_or_latch_events(tmp_path):
    env = Env(tmp_path)
    env.place_limit()
    _latch(env)
    before = _state_bytes(env)
    _tick(env)
    assert _state_bytes(env) == before
    assert not [n for n in _events(env) if "kill_switch_latched" in n]


@pytest.mark.parametrize("case", ["below", "equal", "over"])
def test_drawdown_at_or_over_threshold_blocks_the_fill_without_cancelling(
        tmp_path, case):
    env = Env(tmp_path)
    equity = 980_000.0
    dd = accounting.drawdown_pct(equity, 1_000_000.0)
    _with_threshold(env, {"below": dd + 0.5, "equal": dd, "over": dd - 0.5}[case])
    _set_equity(env, equity)
    oid = env.place_limit(price=148.20)
    before = _state_bytes(env)
    _tick(env)
    if case == "below":
        assert _status(env, oid) == "open"
    else:
        row = orders.get(env.conn, oid)
        assert row["status"] == "pending_fill"
        assert row["close_reason"] is None
        assert "kill_switch_pending_cancel" not in _events(env)
        assert "limit_cancelled" not in _events(env)
    assert _state_bytes(env) == before


def test_drawdown_stop_uses_the_risk_gate_threshold_setting(tmp_path):
    env = Env(tmp_path)
    _set_equity(env, 980_000.0)
    assert SETTINGS.risk.drawdown_kill_pct == 2.0
    oid = env.place_limit(price=148.20)
    _tick(env)
    assert _status(env, oid) == "pending_fill"


def test_drawdown_stop_ends_when_the_drawdown_recovers(tmp_path):
    env = Env(tmp_path)
    _set_equity(env, 970_000.0)
    oid = env.place_limit(price=148.20)
    _tick(env, 1)
    assert _status(env, oid) == "pending_fill"
    _set_equity(env, 1_000_000.0)
    _tick(env, 2)
    assert _status(env, oid) == "open"


def test_drawdown_hold_and_resume_are_each_recorded_once(tmp_path):
    env = Env(tmp_path)
    _set_equity(env, 970_000.0)
    env.place_limit(price=148.20)
    _tick(env, 1)
    _tick(env, 2)
    assert _events(env).count("drawdown_fill_hold") == 1
    text = (tmp_path / "a.log").read_text(encoding="utf-8")
    assert "新規の約定を止める (注文は維持)" in text
    assert "3.00%" in text and "2.0%" in text
    assert "drawdown_fill_resumed" not in _events(env)
    _set_equity(env, 1_000_000.0)
    _tick(env, 3)
    _tick(env, 4)
    assert _events(env).count("drawdown_fill_resumed") == 1
    assert _events(env).count("drawdown_fill_hold") == 1
    _set_equity(env, 970_000.0)
    _tick(env, 5)
    assert _events(env).count("drawdown_fill_hold") == 2


def test_no_drawdown_activity_when_below_threshold_from_the_start(tmp_path):
    env = Env(tmp_path)
    env.place_limit(price=148.20)
    _tick(env)
    assert not [e for e in _events(env) if e.startswith("drawdown_fill")]


def test_drawdown_activity_failure_does_not_stop_the_hold(tmp_path, monkeypatch):
    env = Env(tmp_path)
    _set_equity(env, 970_000.0)
    oid = env.place_limit(price=148.20)
    real_write = env.sched.activity.write

    def write(category, event, text, **kw):
        if event == "drawdown_fill_hold":
            raise OSError("log full")
        return real_write(category, event, text, **kw)

    monkeypatch.setattr(env.sched.activity, "write", write)
    _tick(env)
    assert _status(env, oid) == "pending_fill"
    _tick(env, 2)
    assert _status(env, oid) == "pending_fill"


def test_drawdown_flag_is_kept_when_the_account_is_unknown(tmp_path, monkeypatch):
    env = Env(tmp_path)
    _set_equity(env, 970_000.0)
    env.place_limit(price=148.20)
    _tick(env, 1)
    assert env.sched._drawdown_fill_hold is True
    real_account = accounting.current_account
    monkeypatch.setattr(accounting, "current_account", lambda conn, now: None)
    _tick(env, 2)
    assert env.sched._drawdown_fill_hold is True
    assert "drawdown_fill_resumed" not in _events(env)
    monkeypatch.setattr(accounting, "current_account", real_account)
    _tick(env, 3)
    assert _events(env).count("drawdown_fill_hold") == 1


def test_drawdown_stop_still_runs_exits_and_does_not_write_state(tmp_path):
    env = Env(tmp_path)
    _set_equity(env, 970_000.0)
    open_id = _swing_open(env)
    pending = env.place_limit(price=148.20)
    env.bars["USDJPY"] = _bar(WED, o=148.0, h=148.05, l=147.5, c=147.6)
    env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(env.conn, open_id)["close_reason"] == "sl"
    assert _status(env, pending) == "pending_fill"


def _contend(env):
    lock = tmp_lock_path(env)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def tmp_lock_path(env):
    return str(env.tmp_path / "state.json") + ".lock"


def _release(fd):
    os.close(fd)


def test_lock_contention_stops_only_the_fill_and_never_waits(
        tmp_path, monkeypatch):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    open_id = _swing_open(env)
    fd = _contend(env)
    try:
        calls = []
        real_flock = fcntl.flock
        sleeps = []

        def counting(f, op):
            calls.append(op)
            return real_flock(f, op)

        monkeypatch.setattr(state_module.fcntl, "flock", counting)
        monkeypatch.setattr(state_module.time, "sleep",
                            lambda s: sleeps.append(s))
        env.bars["USDJPY"] = _bar(WED, o=148.0, h=148.05, l=147.5, c=147.6)
        started = time.monotonic()
        env.sched.tick(WED + timedelta(minutes=1))
        assert time.monotonic() - started < 5
    finally:
        _release(fd)
    nb = [op for op in calls if op & fcntl.LOCK_NB]
    assert len(nb) == 1 and nb[0] & fcntl.LOCK_SH
    assert sleeps == []
    assert _status(env, oid) == "pending_fill"
    assert orders.get(env.conn, oid)["close_reason"] is None
    assert orders.get(env.conn, open_id)["close_reason"] == "sl"
    assert _events(env).count("kill_switch_fill_guard_unavailable") == 1


def test_unavailable_activity_is_recorded_once_per_outage_and_recovers(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    fd = _contend(env)
    try:
        _tick(env, 1)
        _tick(env, 2)
        assert _events(env).count("kill_switch_fill_guard_unavailable") == 1
        assert _status(env, oid) == "pending_fill"
    finally:
        _release(fd)
    _tick(env, 3)
    assert _status(env, oid) == "open"
    assert _events(env).count("kill_switch_fill_guard_unavailable") == 1
    second = env.place_limit(price=148.10)
    fd = _contend(env)
    try:
        _tick(env, 4)
    finally:
        _release(fd)
    assert _status(env, second) == "pending_fill"
    assert _events(env).count("kill_switch_fill_guard_unavailable") == 2


def test_restored_activity_is_written_once_when_state_is_readable_again(
        tmp_path):
    env = Env(tmp_path)
    env.place_limit(price=148.20)
    _tick(env, 1)
    assert "kill_switch_fill_guard_restored" not in _events(env)
    fd = _contend(env)
    try:
        _tick(env, 2)
    finally:
        _release(fd)
    _tick(env, 3)
    _tick(env, 4)
    names = _events(env)
    assert names.count("kill_switch_fill_guard_unavailable") == 1
    assert names.count("kill_switch_fill_guard_restored") == 1
    assert "state を読めた — 約定判定を再開" in (
        tmp_path / "a.log").read_text(encoding="utf-8")


def test_restored_activity_failure_does_not_stop_the_fill(tmp_path, monkeypatch):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    fd = _contend(env)
    try:
        _tick(env, 1)
    finally:
        _release(fd)
    real_write = env.sched.activity.write

    def write(category, event, text, **kw):
        if event == "kill_switch_fill_guard_restored":
            raise OSError("log full")
        return real_write(category, event, text, **kw)

    monkeypatch.setattr(env.sched.activity, "write", write)
    _tick(env, 2)
    assert _status(env, oid) == "open"


@pytest.mark.parametrize("exc", [
    OSError("disk"), ValueError("parse"), RuntimeError("boom"),
    StateLockTimeout("deadline")])
def test_state_read_exception_stops_only_the_fill(tmp_path, monkeypatch, exc):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)

    def broken(**kwargs):
        raise exc

    monkeypatch.setattr(env.state, "shared_guard", broken)
    _tick(env)
    assert _status(env, oid) == "pending_fill"
    assert orders.get(env.conn, oid)["close_reason"] is None
    assert _events(env).count("kill_switch_fill_guard_unavailable") == 1


@pytest.mark.parametrize("content", ["{not json", "[]", '{"initialized": true}'])
def test_unreadable_state_file_stops_only_the_fill(tmp_path, content):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    (tmp_path / "state.json").write_text(content, encoding="utf-8")
    _tick(env, 1)
    assert _status(env, oid) == "pending_fill"
    assert _events(env).count("kill_switch_fill_guard_unavailable") == 1
    (tmp_path / "state.json").unlink()
    _tick(env, 2)
    assert _status(env, oid) == "open"


def test_unavailable_state_does_not_cancel_even_when_reads_keep_failing(
        tmp_path, monkeypatch):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    monkeypatch.setattr(env.state, "shared_guard",
                        lambda **kw: (_ for _ in ()).throw(OSError("x")))
    for minute in (1, 2, 3):
        _tick(env, minute)
    assert _status(env, oid) == "pending_fill"
    assert "kill_switch_pending_cancel" not in _events(env)


def test_state_guard_false_is_not_restored_by_a_valid_account(
        tmp_path, monkeypatch):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    monkeypatch.setattr(env.sched, "_fills_allowed_by_state", lambda now: False)
    _tick(env)
    assert _status(env, oid) == "pending_fill"


def _stale_account_env(tmp_path, *, dd_equity=980_000.0):
    env = Env(tmp_path)
    env.pending = env.place_limit(price=148.20)
    orders.insert(env.conn, pair="EURUSD", direction="long",
                  entry_type="market", horizon="swing", status="open",
                  now=WED, quantity=0.1, avg_fill_price=1.1000,
                  stop_loss=1.0960, take_profit=1.1120)
    snapshots.add(env.conn, ts=WED, balance=dd_equity,
                  equity=dd_equity, hwm=1_000_000.0, cashflow=0, source="paper")
    return env


@pytest.mark.parametrize(("minutes", "outcome"), [
    (9, "dd_stop"), (10, "dd_stop"), (11, "account_unknown")])
def test_snapshot_age_decides_between_drawdown_stop_and_account_unknown(
        tmp_path, minutes, outcome):
    env = _stale_account_env(tmp_path)
    oid = env.pending
    env.bars["USDJPY"] = _bar(WED + timedelta(minutes=minutes - 1))
    env.sched.tick(WED + timedelta(minutes=minutes))
    row = orders.get(env.conn, oid)
    if outcome == "dd_stop":
        assert row["status"] == "pending_fill"
        assert row["close_reason"] is None
    else:
        assert (row["status"], row["close_reason"]) == (
            "cancelled", "account_unknown")


def test_stale_mark_to_market_uses_the_latest_snapshot_drawdown_and_exits_run(
        tmp_path):
    env = _stale_account_env(tmp_path)
    other = orders.insert(
        env.conn, pair="USDJPY", direction="long", entry_type="market",
        horizon="swing", status="open", now=WED, quantity=0.1,
        avg_fill_price=148.20, stop_loss=147.80, take_profit=149.00)
    oid = env.pending
    env.bars["USDJPY"] = _bar(WED + timedelta(minutes=2), o=148.0, h=148.05,
                              l=147.5, c=147.6)
    env.sched.tick(WED + timedelta(minutes=5))
    assert orders.get(env.conn, other)["close_reason"] == "sl"
    assert _status(env, oid) == "pending_fill"


def _rebased_env(tmp_path):
    env = _stale_account_env(tmp_path)
    snapshots.add(env.conn, ts=WED, balance=980_000.0,
                  equity=980_000.0, hwm=980_000.0, cashflow=0,
                  source="manual_rebase")
    return env


def test_rebased_snapshot_with_the_same_timestamp_gives_zero_drawdown(tmp_path):
    env = _rebased_env(tmp_path)
    oid = env.pending
    env.bars["USDJPY"] = _bar(WED + timedelta(minutes=2))
    env.sched.tick(WED + timedelta(minutes=3))
    assert _status(env, oid) == "open"


def test_latched_state_blocks_even_when_rebase_made_the_drawdown_zero(tmp_path):
    env = _rebased_env(tmp_path)
    oid = env.pending
    _latch(env)
    env.bars["USDJPY"] = _bar(WED + timedelta(minutes=2))
    env.sched.tick(WED + timedelta(minutes=3))
    row = orders.get(env.conn, oid)
    assert (row["status"], row["close_reason"]) == ("cancelled", "kill_switch")


def test_next_mark_to_market_inherits_the_rebased_hwm(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    snapshots.add(env.conn, ts=WED, balance=980_000.0,
                  equity=980_000.0, hwm=980_000.0, cashflow=0,
                  source="manual_rebase")
    _set_equity(env, 960_000.0)
    _tick(env)
    latest = snapshots.latest(env.conn)
    assert latest["hwm"] == 980_000.0
    assert accounting.drawdown_pct(latest["equity"], latest["hwm"]) >= 2.0
    assert _status(env, oid) == "pending_fill"


def test_pending_that_failed_to_cancel_is_retried_while_latched(
        tmp_path, monkeypatch):
    env = Env(tmp_path)
    flaky = env.place_limit(price=148.20)
    fine = env.place_limit(price=148.10)
    open_id = _swing_open(env)
    _latch(env)
    real_cancel = env.executor.cancel_order

    def cancel(row, reason):
        if row["id"] == flaky:
            raise RuntimeError("db down")
        return real_cancel(row, reason)

    monkeypatch.setattr(env.executor, "cancel_order", cancel)
    env.bars["USDJPY"] = _bar(WED, o=148.0, h=148.05, l=147.5, c=147.6)
    env.sched.tick(WED + timedelta(minutes=1))
    assert _status(env, flaky) == "pending_fill"
    assert orders.get(env.conn, fine)["close_reason"] == "kill_switch"
    assert orders.get(env.conn, open_id)["close_reason"] == "sl"
    assert "cancel_all_pending_error" in _events(env)
    monkeypatch.setattr(env.executor, "cancel_order", real_cancel)
    _tick(env, 2)
    row = orders.get(env.conn, flaky)
    assert (row["status"], row["close_reason"]) == ("cancelled", "kill_switch")


def test_cancel_error_activity_failure_does_not_stop_later_cancels(
        tmp_path, monkeypatch):
    env = Env(tmp_path)
    first = env.place_limit(price=148.20)
    later = env.place_limit(price=148.10)
    open_id = _swing_open(env)
    _latch(env)
    real_cancel = env.executor.cancel_order
    real_write = env.sched.activity.write

    def cancel(row, reason):
        if row["id"] == first:
            raise RuntimeError("db down")
        return real_cancel(row, reason)

    def write(category, event, text, **kw):
        if event == "cancel_all_pending_error":
            raise OSError("log full")
        return real_write(category, event, text, **kw)

    monkeypatch.setattr(env.executor, "cancel_order", cancel)
    monkeypatch.setattr(env.sched.activity, "write", write)
    env.bars["USDJPY"] = _bar(WED, o=148.0, h=148.05, l=147.5, c=147.6)
    env.sched.tick(WED + timedelta(minutes=1))
    assert _status(env, first) == "pending_fill"
    assert orders.get(env.conn, later)["close_reason"] == "kill_switch"
    assert orders.get(env.conn, open_id)["close_reason"] == "sl"


def test_broker_unknown_cancel_is_not_counted_as_released_and_not_finalised(
        tmp_path, monkeypatch):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    _latch(env)
    monkeypatch.setattr(env.executor.broker, "cancel",
                        lambda row: BrokerResult(status="unknown"))
    monkeypatch.setattr(env.executor.broker, "reconcile",
                        lambda row: BrokerResult(status="ok", message="exists"))
    _tick(env, 1)
    assert _status(env, oid) == "cancel_unknown"
    _tick(env, 2)
    assert _status(env, oid) == "cancel_unknown"
    assert "limit_cancelled" not in _events(env)


def test_broker_exception_during_cancel_leaves_cancel_unknown(
        tmp_path, monkeypatch):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    _latch(env)

    def boom(row):
        raise RuntimeError("broker down")

    monkeypatch.setattr(env.executor.broker, "cancel", boom)
    open_id = _swing_open(env)
    env.bars["USDJPY"] = _bar(WED, o=148.0, h=148.05, l=147.5, c=147.6)
    env.sched.tick(WED + timedelta(minutes=1))
    assert _status(env, oid) == "cancel_unknown"
    assert orders.get(env.conn, open_id)["close_reason"] == "sl"


def test_rejected_cancel_stays_protection_pending(tmp_path, monkeypatch):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    _latch(env)
    monkeypatch.setattr(env.executor.broker, "cancel",
                        lambda row: BrokerResult(status="rejected"))
    _tick(env, 1)
    assert _status(env, oid) == "protection_pending"
    _tick(env, 2)
    assert _status(env, oid) == "protection_pending"


def test_activity_failure_does_not_stop_the_cancel_or_exits(
        tmp_path, monkeypatch):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    open_id = _swing_open(env)
    _latch(env)
    real_write = env.sched.activity.write

    def write(category, event, text, **kw):
        if event == "kill_switch_pending_cancel":
            raise OSError("log full")
        return real_write(category, event, text, **kw)

    monkeypatch.setattr(env.sched.activity, "write", write)
    env.bars["USDJPY"] = _bar(WED, o=148.0, h=148.05, l=147.5, c=147.6)
    env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(env.conn, oid)["close_reason"] == "kill_switch"
    assert orders.get(env.conn, open_id)["close_reason"] == "sl"


def test_cancel_path_exception_does_not_stop_exits_and_keeps_the_fill_stopped(
        tmp_path, monkeypatch):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    open_id = _swing_open(env)
    _latch(env)

    def broken(*args, **kwargs):
        raise RuntimeError("db read")

    monkeypatch.setattr(env.sched, "_cancel_all_pending", broken)
    env.bars["USDJPY"] = _bar(WED, o=148.0, h=148.05, l=147.5, c=147.6)
    env.sched.tick(WED + timedelta(minutes=1))
    assert orders.get(env.conn, open_id)["close_reason"] == "sl"
    assert _status(env, oid) == "pending_fill"


def test_cancelled_plan_does_not_come_back_after_the_latch_is_cleared(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    _latch(env)
    _tick(env, 1)
    env.state.update(kill_switch_latched=False)
    _tick(env, 2)
    _tick(env, 3)
    assert _status(env, oid) == "cancelled"
    assert orders.get(env.conn, oid)["close_reason"] == "kill_switch"


def test_expiry_wins_over_the_latch_cancel_in_the_same_tick(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20, hours=1)
    _latch(env)
    env.sched.tick(WED + timedelta(hours=2))
    row = orders.get(env.conn, oid)
    assert row["status"] == "expired"
    assert row["close_reason"] != "kill_switch"


def test_degraded_state_still_performs_the_latch_cancel(tmp_path):
    mode = {"state": "ready"}
    env = Env(tmp_path, state_fn=lambda: mode["state"])
    oid = env.place_limit(price=148.20)
    mode["state"] = "degraded"
    _latch(env)
    _tick(env)
    row = orders.get(env.conn, oid)
    assert (row["status"], row["close_reason"]) == ("cancelled", "kill_switch")


def test_closed_market_tick_does_not_read_state_or_add_the_cancel(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    _latch(env)
    env.sched.tick(SAT)
    assert _status(env, oid) == "pending_fill"
    assert "kill_switch_pending_cancel" not in _events(env)
    assert "kill_switch_fill_guard_unavailable" not in _events(env)


def test_existing_cancel_reasons_are_kept_when_the_account_is_unknown(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    orders.insert(env.conn, pair="EURUSD", direction="long",
                  entry_type="market", horizon="swing", status="open",
                  now=WED, quantity=0.1, avg_fill_price=1.1000,
                  stop_loss=1.0960, take_profit=1.1120)
    env.bars["USDJPY"] = _bar(WED + timedelta(minutes=11))
    env.sched.tick(WED + timedelta(minutes=11))
    assert orders.get(env.conn, oid)["close_reason"] == "account_unknown"


def test_clear_state_and_low_drawdown_leave_the_fill_unchanged(tmp_path):
    env = Env(tmp_path)
    oid = env.place_limit(price=148.20)
    _tick(env)
    assert _status(env, oid) == "open"
    assert _events(env) == [e for e in _events(env)
                            if not e.startswith("kill_switch")]


def test_state_store_is_the_real_store_with_the_clock_used_for_the_deadline(
        tmp_path):
    env = Env(tmp_path)
    assert isinstance(env.sched.state, StateStore)
    assert callable(env.sched.state._monotonic)


def _break_enumeration_in_cancel_all(monkeypatch, *, from_call=1):
    """_cancel_all_pending からの列挙を from_call 回目から落とす。"""
    import sys
    real = orders.list_by_status
    seen = []

    def list_by_status(conn, *args, **kwargs):
        if sys._getframe(1).f_code.co_name == "_cancel_all_pending":
            seen.append(1)
            if len(seen) >= from_call:
                raise RuntimeError("db read")
        return real(conn, *args, **kwargs)

    monkeypatch.setattr(orders, "list_by_status", list_by_status)
    return seen


def _sl_fixture(env, monkeypatch):
    open_id = _swing_open(env)
    pending = env.place_limit(price=148.20)
    env.bars["USDJPY"] = _bar(WED + timedelta(minutes=1), o=148.0, h=148.05,
                              l=147.5, c=147.6)
    monkeypatch.setattr(accounting, "current_account", lambda *a, **k: None)
    return open_id, pending


def test_enumeration_failure_while_latched_and_account_unknown_still_runs_exits(
        tmp_path, monkeypatch):
    env = Env(tmp_path)
    open_id, pending = _sl_fixture(env, monkeypatch)
    _latch(env)
    calls = _break_enumeration_in_cancel_all(monkeypatch, from_call=2)
    env.sched.tick(WED + timedelta(minutes=2))
    assert len(calls) == 2
    assert orders.get(env.conn, open_id)["close_reason"] == "sl"
    assert _status(env, pending) == "cancelled"


def test_enumeration_failure_with_account_unknown_alone_still_runs_exits(
        tmp_path, monkeypatch):
    env = Env(tmp_path)
    open_id, pending = _sl_fixture(env, monkeypatch)
    calls = _break_enumeration_in_cancel_all(monkeypatch)
    env.sched.tick(WED + timedelta(minutes=2))
    assert calls
    assert orders.get(env.conn, open_id)["close_reason"] == "sl"
    assert _status(env, pending) == "pending_fill"
