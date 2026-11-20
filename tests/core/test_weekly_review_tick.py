"""週次の確定の独自 tick: 運用開始週・暦からの列挙・週 × UTC 日の試行・中断の回収・打切り。"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from agentic_fx.core import review_weeks
from agentic_fx.core.weekly_review_tick import (
    ABORT_AFTER, MAX_ATTEMPTS_PER_DAY, POLL_INTERVAL, WeeklyReviewTick)
from agentic_fx.store import alert_state
from agentic_fx.store import weekly_review as store
from agentic_fx.store.db import connect, init_db

UTC = timezone.utc
W1 = date(2027, 1, 22)
W2 = date(2027, 1, 29)
W3 = date(2027, 2, 5)
DAY = timedelta(days=1)
# 2027-02-06 (土) の昼は、W3 (02-05 の終端 22:00Z) の猶予 (翌 22:00Z) の前
AFTER_W2 = datetime(2027, 1, 31, 12, tzinfo=UTC)       # W2 の猶予 (01-30 22:00Z) の後
AFTER_W3 = datetime(2027, 2, 7, 12, tzinfo=UTC)


class Rig:
    def __init__(self, path):
        self.path = path
        conn = connect(path)
        init_db(conn)
        conn.close()
        self.calls: list[tuple[date, datetime]] = []
        self.fail: set[date] = set()
        self.reports: list[str] = []
        self.report_fail = False
        self.during: list = []

    def conn(self):
        return connect(self.path)

    def finalize(self, conn, label, close_at, now):
        self.calls.append((label, now))
        self.during.append(self.state(label, now))
        if label in self.fail:
            raise RuntimeError("boom")
        return store.commit_week(conn, store.WeekRecord(
            week=label.isoformat(), window_end=close_at, finalized_at=now, evaluated_at=now,
            status="completed", evaluation="normal", degraded={}, inventory_state="normal",
            inventory_reason=None, stopped=[]), [], [])

    def report(self, conn, label):
        self.reports.append(label)
        if self.report_fail:
            raise OSError("disk")

    def tick(self, **kw):
        return WeeklyReviewTick(open_conn=self.conn, finalize=self.finalize,
                                report=self.report, **kw)

    def state(self, label, now):
        c = self.conn()
        try:
            key = alert_state.weekly_review_attempt_key(label.isoformat(),
                                                        now.astimezone(UTC).date().isoformat())
            raw = alert_state.get(c, key)
            return None if raw is None else json.loads(raw)
        finally:
            c.close()

    def set_start(self, label):
        c = self.conn()
        alert_state.set(c, alert_state.WEEKLY_REVIEW_START_KEY,
                        json.dumps({"week": label.isoformat()}), now=AFTER_W2)
        c.close()

    def put_attempt(self, label, day, record):
        c = self.conn()
        alert_state.set(c, alert_state.weekly_review_attempt_key(label.isoformat(), day),
                        json.dumps(record), now=AFTER_W2)
        c.close()

    def recorded(self):
        c = self.conn()
        try:
            return {r["week"]: r for r in (store.get_week(c, w) for w in
                                           store.recorded_weeks(c))}
        finally:
            c.close()

    def keys(self):
        c = self.conn()
        try:
            return alert_state.keys_with_prefix(c, alert_state.WEEKLY_REVIEW_ATTEMPT_PREFIX)
        finally:
            c.close()


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "t.db")


# ---------------------------------------------------------------- 運用開始週

def test_the_first_run_saves_the_latest_due_week_as_the_start_and_finalizes_only_it(rig):
    rig.tick().run_once(AFTER_W2)
    c = rig.conn()
    saved = json.loads(alert_state.get(c, alert_state.WEEKLY_REVIEW_START_KEY))
    c.close()
    assert saved == {"week": W2.isoformat()}
    assert [label for label, _ in rig.calls] == [W2]


def test_the_start_week_is_kept_across_restarts(rig):
    rig.tick().run_once(AFTER_W2)
    rig.tick().run_once(AFTER_W3)
    assert [label for label, _ in rig.calls] == [W2, W3]


def test_a_corrupt_start_is_replaced_by_the_latest_due_week(rig):
    c = rig.conn()
    alert_state.set(c, alert_state.WEEKLY_REVIEW_START_KEY, "not json", now=AFTER_W2)
    c.close()
    rig.tick().run_once(AFTER_W2)
    assert [label for label, _ in rig.calls] == [W2]


# ---------------------------------------------------------------- 列挙と順序

def test_unfinished_due_weeks_are_processed_oldest_first(rig):
    rig.set_start(W2)
    rig.tick().run_once(AFTER_W3)
    assert [label for label, _ in rig.calls] == [W2, W3]


def test_a_week_still_in_its_grace_is_not_processed(rig):
    rig.set_start(W1)
    rig.tick().run_once(datetime(2027, 1, 30, 21, tzinfo=UTC))      # W2 の猶予の前
    assert [label for label, _ in rig.calls] == [W1]


def test_a_recorded_week_is_not_processed_again(rig):
    rig.set_start(W1)
    rig.tick().run_once(AFTER_W2)
    rig.tick().run_once(AFTER_W2 + timedelta(hours=1))
    assert [label for label, _ in rig.calls] == [W1, W2]


# ---------------------------------------------------------------- 試行の保存

def test_the_attempt_is_saved_as_running_before_the_week_is_run(rig):
    rig.set_start(W2)
    rig.tick().run_once(AFTER_W2)
    state = rig.during[0]
    assert state["count"] == 1 and state["attempts"][0]["state"] == "running"
    assert len(state["attempts"][0]["id"]) >= 16
    assert rig.state(W2, AFTER_W2)["attempts"][0]["state"] == "done"


def test_a_failed_attempt_is_saved_as_failed_and_counted(rig):
    rig.set_start(W2)
    rig.fail.add(W2)
    rig.tick().run_once(AFTER_W2)
    state = rig.state(W2, AFTER_W2)
    assert state["count"] == 1 and state["attempts"][0]["state"] == "failed"
    assert rig.recorded() == {}


def test_a_week_is_tried_at_most_three_times_per_utc_day(rig):
    rig.set_start(W2)
    rig.fail.add(W2)
    assert MAX_ATTEMPTS_PER_DAY == 3
    for i in range(5):
        rig.tick().run_once(AFTER_W2 + timedelta(minutes=20 * i))
    assert len(rig.calls) == 3
    assert rig.state(W2, AFTER_W2)["count"] == 3


def test_the_next_utc_day_allows_three_more_attempts(rig):
    rig.set_start(W2)
    rig.fail.add(W2)
    for i in range(3):
        rig.tick().run_once(AFTER_W2 + timedelta(minutes=20 * i))
    rig.tick().run_once(AFTER_W2 + DAY)
    assert len(rig.calls) == 4
    assert rig.state(W2, AFTER_W2 + DAY)["count"] == 1


def test_the_count_survives_a_restart_within_the_same_day(rig):
    rig.set_start(W2)
    rig.fail.add(W2)
    tick = rig.tick()
    tick.run_once(AFTER_W2)
    tick.run_once(AFTER_W2 + timedelta(minutes=20))
    rig.tick().run_once(AFTER_W2 + timedelta(minutes=40))
    rig.tick().run_once(AFTER_W2 + timedelta(minutes=60))
    assert len(rig.calls) == 3


def test_a_week_at_its_limit_does_not_block_the_later_week(rig):
    rig.set_start(W2)
    rig.fail.add(W2)
    rig.put_attempt(W2, AFTER_W3.date().isoformat(),
                    {"count": 3, "attempts": [{"id": f"x{i}", "state": "failed"}
                                              for i in range(3)]})
    rig.tick().run_once(AFTER_W3)
    assert [label for label, _ in rig.calls] == [W3]


def test_a_failing_week_does_not_stop_the_later_week_in_the_same_run(rig):
    rig.set_start(W2)
    rig.fail.add(W2)
    rig.tick().run_once(AFTER_W3)
    assert [label for label, _ in rig.calls] == [W2, W3]
    assert set(rig.recorded()) == {W3.isoformat()}


# ---------------------------------------------------------------- 起動時の回収

def test_a_running_attempt_of_a_previous_process_is_collected_as_interrupted_first(rig):
    rig.set_start(W2)
    day = AFTER_W2.date().isoformat()
    rig.put_attempt(W2, day, {"count": 1, "attempts": [{"id": "old", "state": "running"}]})
    rig.tick().run_once(AFTER_W2)
    assert rig.during[0]["attempts"][0] == {"id": "old", "state": "interrupted"}
    state = rig.state(W2, AFTER_W2)
    assert state["count"] == 2 and [a["state"] for a in state["attempts"]] == [
        "interrupted", "done"]


def test_collecting_an_interrupted_attempt_does_not_add_to_the_count(rig):
    rig.set_start(W2)
    day = AFTER_W2.date().isoformat()
    rig.put_attempt(W2, day, {"count": 3, "attempts": [
        {"id": "a", "state": "failed"}, {"id": "b", "state": "failed"},
        {"id": "c", "state": "running"}]})
    rig.tick().run_once(AFTER_W2)
    assert rig.calls == []
    state = rig.state(W2, AFTER_W2)
    assert state["count"] == 3 and state["attempts"][2]["state"] == "interrupted"


def test_each_interrupted_attempt_is_collected_only_once(rig):
    rig.set_start(W2)
    day = AFTER_W2.date().isoformat()
    rig.put_attempt(W2, day, {"count": 3, "attempts": [
        {"id": "a", "state": "failed"}, {"id": "b", "state": "failed"},
        {"id": "c", "state": "running"}]})
    rig.tick().run_once(AFTER_W2)
    first = rig.state(W2, AFTER_W2)
    rig.tick().run_once(AFTER_W2 + timedelta(minutes=30))
    assert rig.state(W2, AFTER_W2) == first


def test_the_collection_runs_once_per_process_not_on_every_run(rig):
    rig.set_start(W2)
    tick = rig.tick()
    tick.run_once(AFTER_W2)
    day = AFTER_W3.date().isoformat()
    rig.put_attempt(W3, day, {"count": 1, "attempts": [{"id": "live", "state": "running"}]})
    tick.run_once(AFTER_W3)
    assert rig.state(W3, AFTER_W3)["attempts"][0]["state"] == "running"


def test_a_week_that_was_committed_before_the_process_stopped_is_not_a_failure(rig):
    rig.set_start(W2)
    c = rig.conn()
    rig.finalize(c, W2, review_weeks.close_of_week(W2), AFTER_W2)
    c.close()
    rig.calls.clear()
    rig.put_attempt(W2, AFTER_W2.date().isoformat(),
                    {"count": 1, "attempts": [{"id": "old", "state": "running"}]})
    rig.tick().run_once(AFTER_W2)
    assert rig.calls == []
    assert rig.state(W2, AFTER_W2)["attempts"][0]["state"] == "interrupted"


# ---------------------------------------------------------------- 打切り

def test_a_week_past_fourteen_days_is_aborted_not_retried(rig):
    rig.set_start(W1)
    close = review_weeks.close_of_week(W1)
    rig.tick().run_once(close + ABORT_AFTER + timedelta(seconds=1))
    rows = rig.recorded()
    assert rows[W1.isoformat()]["status"] == "aborted"
    assert all(label != W1 for label, _ in rig.calls)
    rig.calls.clear()
    rig.tick().run_once(close + ABORT_AFTER + DAY)
    assert all(label != W1 for label, _ in rig.calls)
    assert rig.recorded()[W1.isoformat()]["status"] == "aborted"


def test_a_week_at_exactly_fourteen_days_is_still_tried(rig):
    rig.set_start(W1)
    close = review_weeks.close_of_week(W1)
    rig.tick().run_once(close + ABORT_AFTER)
    assert W1 in [label for label, _ in rig.calls]
    assert rig.recorded()[W1.isoformat()]["status"] == "completed"


def test_a_long_stop_aborts_the_old_weeks_and_still_finalizes_the_recent_ones(rig):
    rig.set_start(W1)
    now = review_weeks.close_of_week(W3) + timedelta(days=2)
    rig.tick().run_once(now)
    status = {w: r["status"] for w, r in rig.recorded().items()}
    assert status == {W1.isoformat(): "aborted", W2.isoformat(): "completed",
                      W3.isoformat(): "completed"}


def test_attempt_keys_older_than_fourteen_days_from_the_window_end_are_removed(rig):
    rig.set_start(W2)
    close = review_weeks.close_of_week(W1)
    old_day = (close + ABORT_AFTER + DAY).date().isoformat()
    rig.put_attempt(W1, old_day, {"count": 1, "attempts": [{"id": "a", "state": "failed"}]})
    rig.put_attempt(W2, AFTER_W2.date().isoformat(),
                    {"count": 1, "attempts": [{"id": "b", "state": "failed"}]})
    rig.tick().run_once(close + ABORT_AFTER + timedelta(days=2))
    names = rig.keys()
    assert not any(W1.isoformat() in name for name in names)


# ---------------------------------------------------------------- report

def test_a_report_is_requested_for_a_finalized_week(rig):
    rig.set_start(W2)
    rig.tick().run_once(AFTER_W2)
    assert W2.isoformat() in rig.reports


def test_a_missing_report_of_a_recorded_recent_week_is_requested_again(rig):
    rig.set_start(W2)
    rig.tick().run_once(AFTER_W2)
    rig.reports.clear()
    rig.tick().run_once(AFTER_W2 + timedelta(hours=1))
    assert W2.isoformat() in rig.reports


def test_a_report_failure_is_not_an_attempt_failure(rig):
    rig.set_start(W2)
    rig.report_fail = True
    rig.tick().run_once(AFTER_W2)
    assert rig.state(W2, AFTER_W2)["attempts"][0]["state"] == "done"
    assert rig.recorded()[W2.isoformat()]["status"] == "completed"


def test_an_aborted_week_gets_a_report_too(rig):
    rig.set_start(W1)
    rig.tick().run_once(review_weeks.close_of_week(W1) + ABORT_AFTER + DAY)
    assert W1.isoformat() in rig.reports


# ---------------------------------------------------------------- poll (tick からの起動)

def test_poll_starts_one_thread_and_returns_without_waiting(rig):
    rig.set_start(W2)
    gate, entered = threading.Event(), threading.Event()
    original = rig.finalize

    def slow(conn, label, close_at, now):
        entered.set()
        assert gate.wait(5)
        return original(conn, label, close_at, now)
    tick = WeeklyReviewTick(open_conn=rig.conn, finalize=slow, report=rig.report)
    tick.poll(AFTER_W2)
    assert entered.wait(5)
    tick.poll(AFTER_W2 + POLL_INTERVAL + timedelta(seconds=1))     # 実行中は重ねない
    gate.set()
    assert tick.join(5)
    assert [label for label, _ in rig.calls] == [W2]


def test_poll_is_throttled_by_the_poll_interval(rig):
    rig.set_start(W2)
    rig.fail.add(W2)
    tick = rig.tick()
    tick.poll(AFTER_W2)
    assert tick.join(5)
    tick.poll(AFTER_W2 + POLL_INTERVAL - timedelta(seconds=1))
    assert tick.join(5)
    assert len(rig.calls) == 1
    tick.poll(AFTER_W2 + POLL_INTERVAL)
    assert tick.join(5)
    assert len(rig.calls) == 2


def test_poll_never_raises_even_if_the_database_cannot_be_opened(rig):
    def broken():
        raise OSError("no db")
    tick = WeeklyReviewTick(open_conn=broken, finalize=rig.finalize, report=rig.report)
    tick.poll(AFTER_W2)
    assert tick.join(5)


def test_the_run_stops_between_weeks_when_asked(rig):
    rig.set_start(W2)
    stop = threading.Event()
    original = rig.finalize

    def once(conn, label, close_at, now):
        stop.set()
        return original(conn, label, close_at, now)
    tick = WeeklyReviewTick(open_conn=rig.conn, finalize=once, report=rig.report,
                            stop_event=stop)
    tick.run_once(AFTER_W3)
    assert [label for label, _ in rig.calls] == [W2]
