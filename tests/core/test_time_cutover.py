"""Cutover from the 21:00 UTC rule to the New York 17:00 rule.

Winter reference: T_c = 2026-11-03 (Tue) 21:30Z, T_b = 2026-11-03 22:00Z.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.core import accounting, market_hours, time_cutover
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import Bar, Mode
from agentic_fx.setup.preflight import PreflightFailure
from agentic_fx.store import missions, orders
from agentic_fx.store.db import connect, init_db

from tests.core.test_scheduler import SETTINGS, Env

UTC = timezone.utc
T_C = datetime(2026, 11, 3, 21, 30, tzinfo=UTC)
T_B = datetime(2026, 11, 3, 22, 0, tzinfo=UTC)
SRC = Path(__file__).resolve().parents[2] / "src"


def _dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


class Crash(BaseException):
    """Stands in for a process death at a chosen point."""


class FakeActivity:
    """Activity log whose write reports nothing, like the real one."""

    def __init__(self, *, drop_writes=False, crash_before_write=False,
                 crash_on_contains_call=None, contains_error=None,
                 error_on_contains_call=None):
        self.lines: list[tuple[str, str | None, str]] = []
        self.drop_writes = drop_writes
        self.crash_before_write = crash_before_write
        self.crash_on_contains_call = crash_on_contains_call
        self.error_on_contains_call = error_on_contains_call
        self.contains_error = contains_error
        self.contains_calls = 0

    def write(self, category, event, summary, ref_id=None):
        if self.crash_before_write:
            raise Crash("before append")
        if not self.drop_writes:
            self.lines.append((event, ref_id, summary))

    def contains(self, event, ref_id):
        self.contains_calls += 1
        if self.crash_on_contains_call == self.contains_calls:
            raise Crash("after append, before readback")
        if self.error_on_contains_call == self.contains_calls:
            raise OSError("readback failed")
        return any(e == event and r == ref_id for e, r, _ in self.lines)

    def end_partial_line(self):
        pass

    def cutover_lines(self):
        return [line for line in self.lines if line[0] == time_cutover.ACTIVITY_EVENT]


def _conn(tmp_path, name="t.db"):
    c = connect(tmp_path / name)
    init_db(c)
    return c


def _marker(conn):
    return time_cutover.cutover_instant(conn)


def _capture(conn):
    raw = time_cutover._read_capture(conn)
    return None if raw is None else json.loads(raw)


class FaultConn:
    """Delegates to a real connection and fails at a chosen statement."""

    def __init__(self, real, *, fail_sql=None, fail_commit=False):
        self._real = real
        self.fail_sql = fail_sql
        self.fail_commit = fail_commit

    def execute(self, sql, *args):
        if self.fail_sql is not None and self.fail_sql in sql:
            raise sqlite3.OperationalError("injected failure")
        return self._real.execute(sql, *args)

    def commit(self):
        if self.fail_commit:
            raise sqlite3.OperationalError("injected commit failure")
        return self._real.commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


# ---- legacy copies and the window ------------------------------------------

def test_legacy_boundaries_are_the_fixed_2100_utc_rule():
    assert time_cutover.legacy_trading_day_start(_dt(2026, 11, 3, 21, 30)) == _dt(2026, 11, 3, 21, 0)
    assert time_cutover.legacy_trading_day_start(_dt(2026, 11, 3, 20, 59)) == _dt(2026, 11, 2, 21, 0)
    assert time_cutover.legacy_next_rollover(_dt(2026, 11, 3, 21, 0)) == _dt(2026, 11, 4, 21, 0)
    assert time_cutover.legacy_next_rollover(_dt(2026, 11, 3, 20, 59)) == _dt(2026, 11, 3, 21, 0)
    assert time_cutover.legacy_trading_date(_dt(2026, 11, 3, 21, 30)).isoformat() == "2026-11-04"
    assert time_cutover.legacy_trading_date(_dt(2026, 11, 3, 20, 59)).isoformat() == "2026-11-03"


def test_window_is_closed_open_between_cutover_and_the_first_new_boundary(tmp_path):
    c = _conn(tmp_path)
    assert time_cutover.active_window(c, T_C) is None  # no marker yet
    time_cutover.ensure_cutover(c, now=T_C, activity=FakeActivity())
    assert time_cutover.active_window(c, T_C - timedelta(seconds=1)) is None
    assert time_cutover.active_window(c, T_C) == (T_C, T_B)
    assert time_cutover.active_window(c, T_B - timedelta(seconds=1)) == (T_C, T_B)
    assert time_cutover.active_window(c, T_B) is None


def test_summer_cutover_has_equal_boundaries_so_the_rules_change_nothing(tmp_path):
    c = _conn(tmp_path)
    now = _dt(2026, 7, 22, 20, 30)
    time_cutover.ensure_cutover(c, now=now, activity=FakeActivity())
    anchor = _dt(2026, 7, 22, 10, 0)
    assert time_cutover.day_close_rollover(c, anchor, now) == _dt(2026, 7, 22, 21, 0)
    assert time_cutover.signal_day_start(c, now) == _dt(2026, 7, 21, 21, 0)
    assert time_cutover.daily_label(c, now).isoformat() == "2026-07-22"


# ---- transitional rules, one by one ----------------------------------------

def test_day_deadline_before_cutover_takes_the_earlier_boundary(tmp_path):
    c = _conn(tmp_path)
    time_cutover.ensure_cutover(c, now=T_C, activity=FakeActivity())
    # opened 10:00Z: old boundary 21:00Z, new boundary 22:00Z -> the old one
    assert time_cutover.day_close_rollover(c, _dt(2026, 11, 3, 10, 0), T_C) == _dt(2026, 11, 3, 21, 0)
    # opened 21:10Z: old boundary is tomorrow 21:00Z, new is 22:00Z today -> the new one
    assert time_cutover.day_close_rollover(c, _dt(2026, 11, 3, 21, 10), T_C) == _dt(2026, 11, 3, 22, 0)
    # opened at/after T_c: new rule only
    assert time_cutover.day_close_rollover(c, _dt(2026, 11, 3, 21, 40), T_C) == _dt(2026, 11, 3, 22, 0)
    assert time_cutover.day_close_rollover(c, T_C, T_C) == _dt(2026, 11, 3, 22, 0)


def test_day_deadline_for_a_position_opened_exactly_at_the_cutover_is_the_new_rule(tmp_path):
    c = _conn(tmp_path)
    early = _dt(2026, 11, 3, 20, 30)  # old boundary 21:00Z is earlier than the new 22:00Z
    time_cutover.ensure_cutover(c, now=early, activity=FakeActivity())
    assert time_cutover.day_close_rollover(c, early, early) == _dt(2026, 11, 3, 22, 0)
    assert time_cutover.day_close_rollover(
        c, early - timedelta(seconds=1), early) == _dt(2026, 11, 3, 21, 0)


def test_day_deadline_after_the_window_is_the_new_rule_alone(tmp_path):
    c = _conn(tmp_path)
    time_cutover.ensure_cutover(c, now=T_C, activity=FakeActivity())
    after = T_B + timedelta(minutes=1)
    assert time_cutover.day_close_rollover(c, _dt(2026, 11, 3, 10, 0), after) == _dt(2026, 11, 3, 22, 0)


def test_daily_base_is_the_larger_of_old_and_new_and_none_fails_closed(tmp_path):
    c = _conn(tmp_path)
    time_cutover.ensure_cutover(c, now=T_C, activity=FakeActivity())
    now = _dt(2026, 11, 3, 21, 45)
    new_start = market_hours.trading_day_start(now)
    old_start = time_cutover.legacy_trading_day_start(now)
    values = {new_start: 100.0, old_start: 105.0}
    assert time_cutover.daily_base(c, now, values.get) == 105.0
    values = {new_start: 107.0, old_start: 105.0}
    assert time_cutover.daily_base(c, now, values.get) == 107.0
    assert time_cutover.daily_base(c, now, {new_start: 100.0}.get) is None
    assert time_cutover.daily_base(c, now, {old_start: 100.0}.get) is None
    after = T_B + timedelta(minutes=5)
    assert time_cutover.daily_base(c, after, lambda s: 1.0) == 1.0


def test_signal_day_start_is_the_earlier_start_inside_the_window(tmp_path):
    c = _conn(tmp_path)
    time_cutover.ensure_cutover(c, now=T_C, activity=FakeActivity())
    # new start 11-02 22:00Z, old start 11-03 21:00Z -> the earlier (new) one wins
    assert time_cutover.signal_day_start(c, T_C) == _dt(2026, 11, 2, 22, 0)
    # with T_c just before the old boundary the old start is the earlier one
    c2 = _conn(tmp_path, "u.db")
    now = _dt(2026, 11, 4, 21, 30)
    time_cutover.ensure_cutover(c2, now=now, activity=FakeActivity())
    assert time_cutover.signal_day_start(c2, now) == _dt(2026, 11, 3, 22, 0)
    assert time_cutover.legacy_trading_day_start(now) == _dt(2026, 11, 4, 21, 0)
    after = _dt(2026, 11, 4, 22, 1)
    assert time_cutover.signal_day_start(c2, after) == _dt(2026, 11, 4, 22, 0)


def test_daily_label_is_the_later_label_inside_the_window(tmp_path):
    c = _conn(tmp_path)
    time_cutover.ensure_cutover(c, now=T_C, activity=FakeActivity())
    # old label 2026-11-04 (21:30Z + 3h), new label 2026-11-03 -> the later (old) one
    assert time_cutover.daily_label(c, T_C).isoformat() == "2026-11-04"
    assert time_cutover.daily_label(c, T_B).isoformat() == "2026-11-04"
    # earlier in the day the new label is the later one
    c2 = _conn(tmp_path, "u.db")
    now = _dt(2026, 11, 4, 12, 0)
    time_cutover.ensure_cutover(c2, now=now, activity=FakeActivity())
    assert time_cutover.daily_label(c2, now).isoformat() == "2026-11-04"
    # outside the window: the new label alone
    assert time_cutover.daily_label(c, T_B + timedelta(minutes=1)).isoformat() == "2026-11-04"
    assert time_cutover.daily_label(c, _dt(2026, 11, 3, 10, 0)).isoformat() == "2026-11-03"


# ---- first start: marker and capture are written together -------------------------

def _open_day_order(conn, filled_at, *, status="open", horizon="day"):
    conn.execute(
        "INSERT INTO orders (pair, direction, entry_type, horizon, status, "
        "quantity, created_at, updated_at, filled_at) VALUES "
        "('USDJPY','long','limit',?,?,1000,?,?,?)",
        (horizon, status, filled_at.isoformat(), filled_at.isoformat(),
         filled_at.isoformat() if status == "open" else None))
    conn.commit()


def test_first_start_writes_marker_capture_and_one_activity_line(tmp_path):
    c = _conn(tmp_path)
    record_snapshot(c, now=_dt(2026, 11, 2, 22, 0), balance=100_000, equity=100_000)
    record_snapshot(c, now=_dt(2026, 11, 3, 21, 0), balance=100_600, equity=100_600)
    _open_day_order(c, _dt(2026, 11, 3, 10, 0))
    _open_day_order(c, _dt(2026, 11, 3, 21, 10))
    _open_day_order(c, _dt(2026, 11, 3, 12, 0), horizon="swing")
    activity = FakeActivity()

    time_cutover.ensure_cutover(c, now=T_C, activity=activity)

    assert _marker(c) == T_C
    capture = _capture(c)
    assert capture["t_c"] == T_C.isoformat() and capture["t_b"] == T_B.isoformat()
    assert capture["activity_logged"] is True
    by_id = {row["id"]: row for row in capture["orders"]}
    assert len(by_id) == 3
    assert by_id[1]["day_deadline"] == {
        "legacy": "2026-11-03T20:55:00+00:00", "new": "2026-11-03T21:55:00+00:00",
        "applied": "2026-11-03T20:55:00+00:00"}
    assert by_id[2]["day_deadline"] == {
        "legacy": "2026-11-04T20:55:00+00:00", "new": "2026-11-03T21:55:00+00:00",
        "applied": "2026-11-03T21:55:00+00:00"}
    assert by_id[3]["day_deadline"] is None and by_id[3]["horizon"] == "swing"
    base = capture["daily_base"]
    assert base["legacy"] == {"equity": 100_600.0, "snapshot_ts": "2026-11-03T21:00:00+00:00"}
    assert base["new"] == {"equity": 100_000.0, "snapshot_ts": "2026-11-02T22:00:00+00:00"}
    assert base["applied"] == 100_600.0
    lines = activity.cutover_lines()
    assert len(lines) == 1
    assert lines[0][1] == f"time_rule_cutover:{T_C.isoformat()}"
    body = json.loads(lines[0][2])
    assert body["t_c"] == T_C.isoformat() and body["daily_base"]["applied"] == 100_600.0
    assert len(body["orders"]) == 3


def test_capture_nulls_the_daily_base_that_cannot_be_derived(tmp_path):
    c = _conn(tmp_path)
    time_cutover.ensure_cutover(c, now=T_C, activity=FakeActivity())
    base = _capture(c)["daily_base"]
    assert base == {"legacy": None, "new": None, "applied": None}


def test_marker_and_capture_share_one_transaction_failure_before_commit(tmp_path):
    c = _conn(tmp_path)
    broken = FaultConn(c, fail_commit=True)
    with pytest.raises(sqlite3.OperationalError):
        time_cutover.ensure_cutover(broken, now=T_C, activity=FakeActivity())
    assert _marker(c) is None and _capture(c) is None
    # the next start redoes the cutover from scratch
    later = T_C + timedelta(minutes=3)
    time_cutover.ensure_cutover(c, now=later, activity=FakeActivity())
    assert _marker(c) == later and _capture(c) is not None


def test_failure_after_the_marker_insert_leaves_neither_row(tmp_path):
    c = _conn(tmp_path)
    broken = FaultConn(c, fail_sql="INSERT INTO alert_state")
    with pytest.raises(sqlite3.OperationalError):
        time_cutover.ensure_cutover(broken, now=T_C, activity=FakeActivity())
    assert _marker(c) is None and _capture(c) is None
    time_cutover.ensure_cutover(c, now=T_C, activity=FakeActivity())
    assert _marker(c) == T_C and _capture(c) is not None


def test_failure_while_capturing_leaves_neither_row(tmp_path):
    c = _conn(tmp_path)
    broken = FaultConn(c, fail_sql="FROM orders")
    with pytest.raises(sqlite3.OperationalError):
        time_cutover.ensure_cutover(broken, now=T_C, activity=FakeActivity())
    assert _marker(c) is None and _capture(c) is None


def test_crash_after_commit_before_append_adds_the_line_once_on_restart(tmp_path):
    c = _conn(tmp_path)
    activity = FakeActivity(crash_before_write=True)
    with pytest.raises(Crash):
        time_cutover.ensure_cutover(c, now=T_C, activity=activity)
    assert _marker(c) == T_C
    assert _capture(c)["activity_logged"] is False
    assert activity.cutover_lines() == []

    activity.crash_before_write = False
    time_cutover.ensure_cutover(c, now=T_C + timedelta(minutes=5), activity=activity)
    assert len(activity.cutover_lines()) == 1
    assert activity.cutover_lines()[0][1] == f"time_rule_cutover:{T_C.isoformat()}"
    assert _capture(c)["activity_logged"] is True
    assert _marker(c) == T_C


def test_crash_after_append_before_readback_does_not_add_a_second_line(tmp_path):
    c = _conn(tmp_path)
    # call 1 is the pre-append check, call 2 is the readback
    activity = FakeActivity(crash_on_contains_call=2)
    with pytest.raises(Crash):
        time_cutover.ensure_cutover(c, now=T_C, activity=activity)
    assert len(activity.cutover_lines()) == 1
    assert _capture(c)["activity_logged"] is False

    time_cutover.ensure_cutover(c, now=T_C + timedelta(minutes=5), activity=activity)
    assert len(activity.cutover_lines()) == 1
    assert _capture(c)["activity_logged"] is True


def test_readback_failure_keeps_the_flag_false_and_never_duplicates(tmp_path):
    c = _conn(tmp_path)
    activity = FakeActivity(error_on_contains_call=2)
    time_cutover.ensure_cutover(c, now=T_C, activity=activity)
    assert len(activity.cutover_lines()) == 1
    assert _capture(c)["activity_logged"] is False

    time_cutover.ensure_cutover(c, now=T_C + timedelta(minutes=5), activity=activity)
    assert len(activity.cutover_lines()) == 1
    assert _capture(c)["activity_logged"] is True


def test_a_write_that_silently_fails_never_marks_the_line_logged(tmp_path):
    c = _conn(tmp_path)
    activity = FakeActivity(drop_writes=True)
    time_cutover.ensure_cutover(c, now=T_C, activity=activity)
    time_cutover.ensure_cutover(c, now=T_C + timedelta(minutes=5), activity=activity)
    assert activity.cutover_lines() == []
    assert _capture(c)["activity_logged"] is False


def test_a_check_failure_before_append_writes_nothing(tmp_path):
    c = _conn(tmp_path)
    activity = FakeActivity(error_on_contains_call=1)
    time_cutover.ensure_cutover(c, now=T_C, activity=activity)
    assert activity.cutover_lines() == []
    assert _capture(c)["activity_logged"] is False


def test_later_starts_leave_the_cutover_instant_and_the_log_alone(tmp_path):
    c = _conn(tmp_path)
    activity = FakeActivity()
    time_cutover.ensure_cutover(c, now=T_C, activity=activity)
    before = _capture(c)
    time_cutover.ensure_cutover(c, now=T_C + timedelta(days=1), activity=activity)
    time_cutover.ensure_cutover(c, now=T_C + timedelta(days=2), activity=activity)
    assert _marker(c) == T_C
    assert _capture(c) == before
    assert len(activity.cutover_lines()) == 1


def test_marker_without_capture_gets_one_supplemental_capture(tmp_path):
    c = _conn(tmp_path)
    c.execute("INSERT INTO data_migrations(name, applied_at) VALUES (?, ?)",
              (time_cutover.MARKER_NAME, T_C.isoformat()))
    c.commit()
    later = T_C + timedelta(hours=3)
    activity = FakeActivity()

    time_cutover.ensure_cutover(c, now=later, activity=activity)

    capture = _capture(c)
    assert _marker(c) == T_C
    assert capture["t_c"] == T_C.isoformat()
    assert capture["supplemented_at"] == later.isoformat()
    assert capture["activity_logged"] is True
    assert len(activity.cutover_lines()) == 1
    assert activity.cutover_lines()[0][1] == f"time_rule_cutover:{T_C.isoformat()}"

    time_cutover.ensure_cutover(c, now=later + timedelta(hours=1), activity=activity)
    assert _capture(c)["supplemented_at"] == later.isoformat()
    assert len(activity.cutover_lines()) == 1


def test_supplemental_capture_applies_the_new_rule_to_positions_opened_after_the_cutover(tmp_path):
    c = _conn(tmp_path)
    t_c = _dt(2026, 11, 3, 20, 30)
    c.execute("INSERT INTO data_migrations(name, applied_at) VALUES (?, ?)",
              (time_cutover.MARKER_NAME, t_c.isoformat()))
    c.commit()
    _open_day_order(c, _dt(2026, 11, 3, 10, 0))
    _open_day_order(c, _dt(2026, 11, 3, 20, 45))
    time_cutover.ensure_cutover(c, now=_dt(2026, 11, 3, 21, 0), activity=FakeActivity())
    by_id = {row["id"]: row["day_deadline"] for row in _capture(c)["orders"]}
    assert by_id[1]["applied"] == "2026-11-03T20:55:00+00:00"
    assert by_id[2]["applied"] == "2026-11-03T21:55:00+00:00"
    assert by_id[2]["legacy"] == "2026-11-03T20:55:00+00:00"


def test_capture_without_marker_refuses_to_start(tmp_path):
    c = _conn(tmp_path)
    activity = FakeActivity()
    time_cutover.ensure_cutover(c, now=T_C, activity=activity)
    c.execute("DELETE FROM data_migrations WHERE name=?", (time_cutover.MARKER_NAME,))
    c.commit()
    with pytest.raises(PreflightFailure) as caught:
        time_cutover.ensure_cutover(c, now=T_C + timedelta(days=1), activity=activity)
    assert caught.value.code == "time_rule_marker_missing"
    assert _marker(c) is None  # nothing re-created behind the operator's back
    assert any(line[0] == time_cutover.REFUSED_EVENT for line in activity.lines)


def test_real_activity_log_contains_matches_event_and_ref_exactly(tmp_path):
    from agentic_fx.activity import Category

    log = ActivityLog(tmp_path / "logs" / "activity.log")
    assert log.contains("time_rule_cutover", "x") is False  # no file yet
    log.write(Category.SYSTEM, "time_rule_cutover", "body", ref_id="x")
    log.write(Category.SYSTEM, "other", "time_rule_cutover x", ref_id="y")
    assert log.contains("time_rule_cutover", "x") is True
    assert log.contains("time_rule_cutover", "y") is False
    assert log.contains("other", "x") is False


def test_cutover_with_a_real_activity_log_writes_one_line_and_survives_restart(tmp_path):
    c = _conn(tmp_path)
    log = ActivityLog(tmp_path / "logs" / "activity.log")
    time_cutover.ensure_cutover(c, now=T_C, activity=log)
    time_cutover.ensure_cutover(c, now=T_C + timedelta(minutes=9), activity=log)
    text = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    assert text.count("\ttime_rule_cutover\t") == 1
    assert _capture(c)["activity_logged"] is True


def test_real_activity_log_that_cannot_be_written_stays_unlogged(tmp_path):
    c = _conn(tmp_path)
    log_dir = tmp_path / "logs"
    log = ActivityLog(log_dir / "activity.log")
    (log_dir / "activity.log").mkdir()  # appending to a directory fails
    time_cutover.ensure_cutover(c, now=T_C, activity=log)
    assert _capture(c)["activity_logged"] is False
    (log_dir / "activity.log").rmdir()
    time_cutover.ensure_cutover(c, now=T_C + timedelta(minutes=1), activity=log)
    assert _capture(c)["activity_logged"] is True


# ---- day positions through the scheduler ----------------------------

def _filled_day_env(tmp_path, *, name, placed_at, filled_at, cutover_at=None):
    home = tmp_path / name
    home.mkdir()
    env = Env(home, base=placed_at)
    if cutover_at is not None:
        time_cutover.ensure_cutover(env.conn, now=cutover_at,
                                    activity=ActivityLog(home / "cut.log"))
    oid = env.place_limit(hours=12)
    env.bars["USDJPY"] = Bar("USDJPY", "1m", filled_at - timedelta(minutes=1),
                             148.30, 148.35, 148.15, 148.25, 100)
    env.sched.tick(filled_at)
    row = orders.get(env.conn, oid)
    assert row["status"] == "open"
    assert datetime.fromisoformat(row["filled_at"]) == filled_at
    return env, oid


def test_position_opened_by_the_old_rule_after_2100_closes_at_the_new_boundary(tmp_path):
    env, oid = _filled_day_env(
        tmp_path, name="a", placed_at=_dt(2026, 11, 3, 21, 9),
        filled_at=_dt(2026, 11, 3, 21, 10), cutover_at=T_C)
    env.sched._force_close_day(_dt(2026, 11, 3, 21, 54))
    assert orders.get(env.conn, oid)["status"] == "open"
    env.sched._force_close_day(_dt(2026, 11, 3, 21, 55))
    row = orders.get(env.conn, oid)
    assert (row["status"], row["close_reason"]) == ("closed", "day_rollover")


def test_position_whose_old_deadline_passed_is_retried_right_after_the_cutover(tmp_path):
    from tests.core.test_scheduler import QUOTE

    quote_ok = {"value": False}

    def quote(pair):
        if not quote_ok["value"]:
            raise RuntimeError("quote down")
        return QUOTE

    env, oid = _filled_day_env(
        tmp_path, name="b", placed_at=_dt(2026, 11, 3, 9, 59),
        filled_at=_dt(2026, 11, 3, 10, 0))
    time_cutover.ensure_cutover(env.conn, now=T_C,
                                activity=ActivityLog(tmp_path / "b" / "cut.log"))
    env.executor.quote_fn = quote
    env.sched._force_close_day(T_C)
    assert orders.get(env.conn, oid)["status"] == "open"
    assert "day_close_deferred" in (tmp_path / "b" / "a.log").read_text(encoding="utf-8")
    quote_ok["value"] = True
    env.sched._force_close_day(T_C + timedelta(seconds=30))
    row = orders.get(env.conn, oid)
    assert (row["status"], row["close_reason"]) == ("closed", "day_rollover")


def test_without_a_cutover_the_same_position_would_wait_for_the_new_boundary(tmp_path):
    env, oid = _filled_day_env(
        tmp_path, name="b0", placed_at=_dt(2026, 11, 3, 9, 59),
        filled_at=_dt(2026, 11, 3, 10, 0))
    env.sched._force_close_day(T_C)
    assert orders.get(env.conn, oid)["status"] == "open"


def test_position_opened_after_the_cutover_follows_the_new_rule_only(tmp_path):
    env, oid = _filled_day_env(
        tmp_path, name="c", placed_at=_dt(2026, 11, 3, 21, 39),
        filled_at=_dt(2026, 11, 3, 21, 40), cutover_at=T_C)
    env.sched._force_close_day(_dt(2026, 11, 3, 21, 54))
    assert orders.get(env.conn, oid)["status"] == "open"
    env.sched._force_close_day(_dt(2026, 11, 3, 21, 55))
    assert orders.get(env.conn, oid)["status"] == "closed"


# ---- daily loss base --------------------------------------------------

def test_daily_loss_base_does_not_fail_open_across_the_cutover(tmp_path):
    c = _conn(tmp_path)
    record_snapshot(c, now=_dt(2026, 11, 2, 22, 0), balance=100_000, equity=100_000)
    record_snapshot(c, now=_dt(2026, 11, 3, 21, 0), balance=100_600, equity=100_600)
    time_cutover.ensure_cutover(c, now=T_C, activity=FakeActivity())
    now = _dt(2026, 11, 3, 21, 45)
    record_snapshot(c, now=now, balance=99_500, equity=99_500)

    base = accounting.daily_start_equity(c, now)
    assert base == 100_600
    loss = (base - 99_500) / base * 100
    assert loss >= SETTINGS.risk.daily_loss_limit_pct  # 1.09% >= 1.0%
    # the new rule alone would have let it through
    assert (100_000 - 99_500) / 100_000 * 100 < SETTINGS.risk.daily_loss_limit_pct

    # past T_b only the first snapshot since 22:00Z counts
    record_snapshot(c, now=_dt(2026, 11, 3, 22, 3), balance=99_400, equity=99_400)
    assert accounting.daily_start_equity(c, _dt(2026, 11, 3, 22, 5)) == 99_400


def test_daily_loss_base_is_unknown_when_either_reference_is_missing(tmp_path):
    c = _conn(tmp_path)
    record_snapshot(c, now=_dt(2026, 11, 2, 23, 0), balance=100_000, equity=100_000)
    time_cutover.ensure_cutover(c, now=T_C, activity=FakeActivity())
    now = _dt(2026, 11, 3, 21, 45)
    # old reference: nothing since 21:00Z and the last one is older than 22h
    assert accounting.daily_start_equity(c, now, max_age_hours=22) is None
    # the new reference alone has a value; only the old one is missing
    new = accounting.daily_start_snapshot(
        c, now, market_hours.trading_day_start(now), max_age_hours=22)
    old = accounting.daily_start_snapshot(
        c, now, time_cutover.legacy_trading_day_start(now), max_age_hours=22)
    assert new is not None and new[0] == 100_000
    assert old is None


# ---- signal daily cap --------------------------------------------------

def test_signal_cap_counts_from_the_earlier_start_across_the_cutover(tmp_path):
    c = _conn(tmp_path)
    t_c = _dt(2026, 11, 4, 12, 0)
    rows = [_dt(2026, 11, 3, 21, 10), _dt(2026, 11, 3, 21, 40)]
    rows += [_dt(2026, 11, 3, 22, 10) + timedelta(hours=k) for k in range(10)]
    assert rows[-1] <= t_c - timedelta(minutes=10)
    assert SETTINGS.plugin.signal_daily_max == 12
    for ts in rows:
        missions.start(c, "trade", "local", "m", ts, "signal:p")
    # the cap is judged before the cutover marker exists too (new rule alone)
    assert missions.signals_rate_ok(c, t_c, SETTINGS) is True
    time_cutover.ensure_cutover(c, now=t_c, activity=FakeActivity())
    assert missions.signals_rate_ok(c, t_c, SETTINGS) is False  # 12 from the old start
    assert missions.signals_rate_ok(c, _dt(2026, 11, 4, 22, 1), SETTINGS) is True


# ---- limit expiry is untouched ------------------------------------------

def test_limit_expiry_is_the_same_across_the_cutover(tmp_path):
    env = Env(tmp_path, base=_dt(2026, 11, 3, 21, 0))
    oid = env.place_limit(hours=2)
    time_cutover.ensure_cutover(env.conn, now=T_C, activity=ActivityLog(tmp_path / "cut.log"))
    env.sched._expire_limits(_dt(2026, 11, 3, 22, 59))
    assert orders.get(env.conn, oid)["status"] == "pending_fill"
    env.sched._expire_limits(_dt(2026, 11, 3, 23, 1))
    assert orders.get(env.conn, oid)["status"] == "expired"
    env.sched._expire_limits(_dt(2026, 11, 3, 23, 2))
    assert (tmp_path / "a.log").read_text(encoding="utf-8").count("limit_expired") == 1


# ---- Friday close --------------------------------------------------------

def test_friday_limit_is_cancelled_once_at_the_new_york_close(tmp_path):
    fri = _dt(2026, 11, 6, 20, 30)
    env = Env(tmp_path, base=fri)
    oid = env.place_limit(hours=12)
    env.state.update(mode=Mode.TRADING)
    time_cutover.ensure_cutover(env.conn, now=fri, activity=ActivityLog(tmp_path / "cut.log"))

    env.sched.tick(_dt(2026, 11, 6, 21, 0))
    assert orders.get(env.conn, oid)["status"] == "pending_fill"
    for minute in (0, 30, 55):
        assert market_hours.is_market_open(_dt(2026, 11, 6, 21, minute))
    env.sched.tick(_dt(2026, 11, 6, 22, 0))
    assert orders.get(env.conn, oid)["status"] == "cancelled"
    log = tmp_path / "a.log"
    assert log.read_text(encoding="utf-8").count("limit_cancelled") == 1

    env.sched._was_open = None  # a restart at 22:30Z
    env.sched.tick(_dt(2026, 11, 6, 22, 30))
    assert log.read_text(encoding="utf-8").count("limit_cancelled") == 1


def test_friday_cutover_after_the_old_close_cancel_adds_no_second_cancel(tmp_path):
    fri = _dt(2026, 11, 6, 20, 30)
    env = Env(tmp_path, base=fri)
    oid = env.place_limit(hours=12)
    env.state.update(mode=Mode.TRADING)
    env.executor.cancel_order(orders.get(env.conn, oid), reason="market_close")
    assert orders.get(env.conn, oid)["status"] == "cancelled"
    time_cutover.ensure_cutover(env.conn, now=_dt(2026, 11, 6, 21, 10),
                                activity=ActivityLog(tmp_path / "cut.log"))
    env.sched.tick(_dt(2026, 11, 6, 21, 10))
    env.sched.tick(_dt(2026, 11, 6, 22, 0))
    assert (tmp_path / "a.log").read_text(encoding="utf-8").count("limit_cancelled") == 1


# ---- end of the window and no rollback --------------------------------------

def test_legacy_functions_are_not_called_after_the_window(tmp_path, monkeypatch):
    env, oid = _filled_day_env(
        tmp_path, name="d", placed_at=_dt(2026, 11, 3, 9, 59),
        filled_at=_dt(2026, 11, 3, 10, 0))
    time_cutover.ensure_cutover(env.conn, now=T_C,
                                activity=ActivityLog(tmp_path / "d" / "cut.log"))
    record_snapshot(env.conn, now=_dt(2026, 11, 3, 22, 0), balance=1_000_000, equity=1_000_000)

    def boom(*_args, **_kwargs):
        raise AssertionError("legacy rule called after the window")

    for name in ("legacy_trading_day_start", "legacy_next_rollover", "legacy_trading_date"):
        monkeypatch.setattr(time_cutover, name, boom)
    after = _dt(2026, 11, 3, 22, 1)
    assert accounting.daily_start_equity(env.conn, after) == 1_000_000
    assert missions.signals_rate_ok(env.conn, after, SETTINGS) is True
    assert time_cutover.daily_label(env.conn, after).isoformat() == "2026-11-04"
    env.bars["USDJPY"] = Bar("USDJPY", "1m", after - timedelta(minutes=1),
                             148.30, 148.35, 148.15, 148.25, 100)
    env.sched.tick(after)  # the first tick after the window must not touch the old rule
    # the new rule closes the position at 21:55Z already, so it is closed now
    assert orders.get(env.conn, oid)["status"] == "closed"


def test_legacy_functions_are_the_ones_called_inside_the_window(tmp_path, monkeypatch):
    c = _conn(tmp_path)
    record_snapshot(c, now=_dt(2026, 11, 3, 21, 20), balance=1, equity=100.0)
    missions.start(c, "trade", "local", "m", _dt(2026, 11, 3, 10, 0), "signal")
    time_cutover.ensure_cutover(c, now=T_C, activity=FakeActivity())
    inside = _dt(2026, 11, 3, 21, 45)

    def boom(*_args, **_kwargs):
        raise AssertionError("reached")

    monkeypatch.setattr(time_cutover, "legacy_trading_day_start", boom)
    with pytest.raises(AssertionError, match="reached"):
        accounting.daily_start_equity(c, inside)
    with pytest.raises(AssertionError, match="reached"):
        missions.signals_rate_ok(c, inside, SETTINGS)
    monkeypatch.setattr(time_cutover, "legacy_next_rollover", boom)
    with pytest.raises(AssertionError, match="reached"):
        time_cutover.day_close_rollover(c, _dt(2026, 11, 3, 10, 0), inside)
    monkeypatch.setattr(time_cutover, "legacy_trading_date", boom)
    with pytest.raises(AssertionError, match="reached"):
        time_cutover.daily_label(c, inside)


def test_a_forward_fix_restart_keeps_the_cutover_instant(tmp_path):
    c = _conn(tmp_path)
    activity = FakeActivity()
    time_cutover.ensure_cutover(c, now=T_C, activity=activity)
    restart = _dt(2026, 11, 3, 23, 0)
    time_cutover.ensure_cutover(c, now=restart, activity=activity)
    assert _marker(c) == T_C
    assert time_cutover.active_window(c, restart) is None
    assert time_cutover.day_close_rollover(c, _dt(2026, 11, 3, 10, 0), restart) == T_B


def test_no_source_path_removes_the_marker():
    pattern = re.compile(
        r"delete\s+from\s+data_migrations|drop\s+table[^;\"']*data_migrations"
        r"|update\s+data_migrations|replace\s+into\s+data_migrations"
        r"|insert\s+or\s+replace\s+into\s+data_migrations",
        re.IGNORECASE)
    offenders = [str(path.relative_to(SRC)) for path in SRC.rglob("*.py")
                 if pattern.search(path.read_text(encoding="utf-8"))]
    assert offenders == []
    # only one writer of the marker row exists
    writers = [str(path.relative_to(SRC)) for path in SRC.rglob("*.py")
               if time_cutover.MARKER_NAME in path.read_text(encoding="utf-8")]
    assert writers == ["agentic_fx/core/time_cutover.py"]


# ---- audit data that is damaged must not stop the start -----------------------

def _cutover_lines_in(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    return [l for l in text.splitlines() if "\ttime_rule_cutover\t" in l]


def test_activity_log_with_invalid_utf8_does_not_stop_the_start(tmp_path):
    c = _conn(tmp_path)
    path = tmp_path / "logs" / "activity.log"
    path.parent.mkdir()
    path.write_bytes(b"2026-11-01T00:00:00\tSYSTEM\tolder\tbroken \xff\xfe\t-\n")
    log = ActivityLog(path)
    time_cutover.ensure_cutover(c, now=T_C, activity=log)
    time_cutover.ensure_cutover(c, now=T_C + timedelta(minutes=5), activity=log)
    assert len(_cutover_lines_in(path)) == 1
    assert _capture(c)["activity_logged"] is True


def test_activity_log_without_a_final_newline_still_gets_one_line_only(tmp_path):
    c = _conn(tmp_path)
    path = tmp_path / "logs" / "activity.log"
    path.parent.mkdir()
    path.write_bytes(b"2026-11-01T00:00:00\tSYSTEM\tolder\thalf written")
    log = ActivityLog(path)
    time_cutover.ensure_cutover(c, now=T_C, activity=log)
    assert _capture(c)["activity_logged"] is True
    time_cutover.ensure_cutover(c, now=T_C + timedelta(minutes=5), activity=log)
    assert len(_cutover_lines_in(path)) == 1


@pytest.mark.parametrize("damaged", ["null", "[1]", "not json"])
def test_unusable_capture_keeps_the_start_and_reports_it_once(tmp_path, damaged):
    c = _conn(tmp_path)
    log = ActivityLog(tmp_path / "logs" / "activity.log")
    time_cutover.ensure_cutover(c, now=T_C, activity=FakeActivity())
    c.execute("UPDATE alert_state SET value=? WHERE key=?",
              (damaged, time_cutover.CAPTURE_KEY))
    c.commit()
    time_cutover.ensure_cutover(c, now=T_C + timedelta(minutes=5), activity=log)
    time_cutover.ensure_cutover(c, now=T_C + timedelta(minutes=9), activity=log)
    text = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    notes = [l for l in text.splitlines() if "\ttime_rule_cutover_capture_invalid\t" in l]
    assert len(notes) == 1
    assert _cutover_lines_in(tmp_path / "logs" / "activity.log") == []
    assert _marker(c) == T_C


# ---- the capture row has one writer ------------------------------------------

def test_the_generic_alert_state_api_cannot_reach_the_capture(tmp_path):
    from agentic_fx.store import alert_state

    c = _conn(tmp_path)
    time_cutover.ensure_cutover(c, now=T_C, activity=FakeActivity())
    before = _capture(c)
    with pytest.raises(ValueError):
        alert_state.set(c, time_cutover.CAPTURE_KEY, "{}", now=T_C)
    with pytest.raises(ValueError):
        alert_state.get(c, time_cutover.CAPTURE_KEY)
    assert _capture(c) == before
    assert time_cutover.CAPTURE_KEY not in (SRC / "agentic_fx/store/alert_state.py").read_text(
        encoding="utf-8")


# ---- the write helper never commits somebody else's work ----------------------

def test_marker_write_refuses_to_run_inside_an_open_transaction(tmp_path):
    c = _conn(tmp_path)
    c.execute("INSERT INTO alert_state(key, value, updated_at) VALUES ('other', 'x', 'y')")
    assert c.in_transaction
    with pytest.raises(RuntimeError):
        time_cutover._write_marker_and_capture(c, T_C)
    assert c.in_transaction  # the caller's pending write was neither committed nor dropped
    assert _marker(c) is None
    c.rollback()
