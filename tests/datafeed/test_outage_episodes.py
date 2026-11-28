"""OutageStateMachine の週末をまたぐ episode・永続の原子性・複数 key。

時刻はすべて実時刻 (UTC) の例で固定する。期待値は spec から手で書いた値。
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.outage import EMPTY_REPORT, IngestTickReport, OutageStateMachine
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import upsert_cache_bars

PAIR = "USDJPY"
OTHER = "EURUSD"
KEY = (PAIR, "1m")
OTHER_KEY = (OTHER, "1m")
UTC = timezone.utc
GRACE = timedelta(seconds=30)
WIDTHS = {"1m": timedelta(minutes=1)}
WED = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


def _rig(tmp_path, *, keys=(KEY,), activity=None, **kw):
    conn = connect(tmp_path / "outage.db")
    init_db(conn)
    machine = OutageStateMachine(conn, hard_keys=frozenset(keys), interval_widths=WIDTHS,
                                 grace=GRACE, storage_source="mt5-live",
                                 activity=activity, **kw)
    return conn, machine


def _bar(conn, bar_time, pair=PAIR):
    upsert_cache_bars(conn, [Bar(pair, "1m", bar_time, 1, 1, 1, 1, 1)], source="mt5-live")
    conn.commit()


def _ok(*keys):
    keys = keys or (KEY,)
    return IngestTickReport(attempted=frozenset(keys), succeeded=frozenset(keys),
                            failed=frozenset(), deferred=frozenset(), empty=frozenset())


def _events(log):
    return [line.split("\t")[2] for line in log.tail(100)]


def _row(conn):
    return dict(conn.execute("SELECT * FROM datafeed_outage_state WHERE id=1").fetchone())


def test_friday_restricted_survives_the_weekend_and_recovers_on_the_first_healthy_tick(
        tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    conn, machine = _rig(tmp_path, activity=log)
    fri = datetime(2026, 9, 25, 20, 58, tzinfo=UTC)
    _bar(conn, datetime(2026, 9, 25, 20, 50, tzinfo=UTC))

    assert machine.observe(fri, _ok()) == "restricted"
    stored = _row(conn)
    assert stored["restricted_since"] == "2026-09-25T20:52:30+00:00"
    assert stored["restricted_deadline_at"] == "2026-09-25T21:22:30+00:00"
    assert stored["epoch"] == 1

    # 閉場中は報告が失敗でも空でも、state・epoch・streak のどれも動かない。
    failed = IngestTickReport(attempted=frozenset({KEY}), succeeded=frozenset(),
                              failed=frozenset({(KEY, "x")}), deferred=frozenset(),
                              empty=frozenset())
    for closed in (datetime(2026, 9, 25, 21, 30, tzinfo=UTC),
                   datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
                   datetime(2026, 9, 27, 20, 59, tzinfo=UTC)):
        assert machine.observe(closed, failed) == "restricted"
        assert machine.observe(closed, EMPTY_REPORT) == "restricted"
        after = _row(conn)
        assert (after["state"], after["epoch"], after["ready_streak"]) == ("restricted", 1, 0)
        assert after["restricted_deadline_at"] == "2026-09-25T21:22:30+00:00"

    # restricted の連続は開場時間で数える: 金曜 20:52:30 から閉場 (21:00) までの 7.5 分と、
    # 日曜 21:00 から 2 分の 9.5 分。壁時計の 2 日ではないので、日曜の最初の healthy tick で復帰する
    _bar(conn, datetime(2026, 9, 27, 21, 0, tzinfo=UTC))
    sunday = datetime(2026, 9, 27, 21, 2, tzinfo=UTC)
    assert machine.observe(sunday, _ok()) == "ready"
    recovered = _row(conn)
    assert recovered["pending_human_confirmation"] == 0
    assert recovered["epoch"] == 1
    assert recovered["restricted_since"] is None and recovered["ready_streak"] == 0
    assert _events(log) == ["datafeed_restricted", "datafeed_recovered_auto"]
    assert recovered["updated_at"] == "2026-09-27T21:02:00+00:00"


def test_friday_restricted_stops_after_thirty_open_minutes_counted_across_the_weekend(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    conn, machine = _rig(tmp_path, activity=log, auto_resume_when_flat=False)
    _bar(conn, datetime(2026, 9, 25, 20, 50, tzinfo=UTC))
    assert machine.observe(datetime(2026, 9, 25, 20, 58, tzinfo=UTC), _ok()) == "restricted"
    # 開場分: 金曜 7.5 分 + 日曜 21:00 から。21:22 までで 29.5 分、21:23 で 30.5 分
    _bar(conn, datetime(2026, 9, 27, 21, 0, tzinfo=UTC))
    assert machine.observe(datetime(2026, 9, 27, 21, 22, tzinfo=UTC), _ok()) == "restricted"
    assert machine.observe(datetime(2026, 9, 27, 21, 23, tzinfo=UTC), _ok()) == "degraded"
    row = _row(conn)
    assert row["confirmed"] == 1 and row["epoch"] == 1


def test_winter_sunday_2100_to_2200_is_closed_for_outage_failures(tmp_path):
    conn, machine = _rig(tmp_path)
    failed = IngestTickReport(
        attempted=frozenset({KEY}), succeeded=frozenset(),
        failed=frozenset({(KEY, "x")}), deferred=frozenset(), empty=frozenset())

    for now in (
        datetime(2026, 11, 8, 21, 0, tzinfo=UTC),
        datetime(2026, 11, 8, 21, 59, tzinfo=UTC),
    ):
        assert machine.observe(now, failed) == "ready"
        assert _row(conn)["epoch"] == 0

    # 開場後の失敗は、flat なら猶予内の restricted (停止はしない)
    assert machine.observe(datetime(2026, 11, 8, 22, 1, tzinfo=UTC), failed) == "restricted"
    assert _row(conn)["epoch"] == 1


# ---- 原子性 ---------------------------------------------------------------


def test_failure_inside_the_state_write_rolls_back_state_and_gap_rows_together(
        tmp_path, monkeypatch):
    log = ActivityLog(tmp_path / "activity.log")
    conn, machine = _rig(tmp_path, keys=(KEY, OTHER_KEY), activity=log)
    _bar(conn, WED - timedelta(minutes=10))
    _bar(conn, WED - timedelta(minutes=10), OTHER)
    machine._ensure_row(WED)  # row の作成は observe の transaction の中なので、先に確定させておく
    original = machine._save_state

    def save_then_crash(**kwargs):
        original(**kwargs)  # UPDATE まで実行したうえで落とす
        raise sqlite3.OperationalError("injected crash")

    monkeypatch.setattr(machine, "_save_state", save_then_crash)
    with pytest.raises(sqlite3.OperationalError):
        machine.observe(WED, _ok(KEY, OTHER_KEY))

    stored = _row(conn)
    assert (stored["state"], stored["epoch"]) == ("ready", 0)
    assert stored["restricted_since"] is None and stored["restricted_deadline_at"] is None
    assert conn.execute("SELECT count(*) FROM datafeed_outage_gap").fetchone()[0] == 0
    assert _events(log) == []

    # 障害が去った次の tick は、食い違いなく同じ遷移を確定させる。
    monkeypatch.undo()
    assert machine.observe(WED + timedelta(minutes=1), _ok(KEY, OTHER_KEY)) == "restricted"
    assert _row(conn)["epoch"] == 1
    assert conn.execute("SELECT count(*) FROM datafeed_outage_gap WHERE epoch=1"
                        ).fetchone()[0] == 2


def test_failure_while_writing_gap_rows_leaves_the_state_row_untouched(
        tmp_path, monkeypatch):
    conn, machine = _rig(tmp_path, keys=(KEY, OTHER_KEY))
    _bar(conn, WED - timedelta(minutes=10))
    _bar(conn, WED - timedelta(minutes=10), OTHER)
    machine._ensure_row(WED)  # row の作成は observe の transaction の中なので、先に確定させておく
    def half_written(epoch, origins):
        # 1 key 分の gap を書いた後で落とす
        machine.conn.execute(
            "INSERT INTO datafeed_outage_gap (pair, interval, epoch, gap_start, "
            "replay_through) VALUES (?, '1m', ?, ?, NULL)", (PAIR, epoch, WED.isoformat()))
        raise sqlite3.OperationalError("injected crash")

    monkeypatch.setattr(machine, "_open_gaps", half_written)
    with pytest.raises(sqlite3.OperationalError):
        machine.observe(WED, _ok(KEY, OTHER_KEY))

    assert _row(conn)["state"] == "ready"
    assert conn.execute("SELECT count(*) FROM datafeed_outage_gap").fetchone()[0] == 0


class _RaisingActivity:
    def write(self, *args, **kwargs):
        raise OSError("disk full")


def test_activity_exception_after_commit_does_not_undo_the_new_state(tmp_path):
    conn, machine = _rig(tmp_path, activity=_RaisingActivity())
    _bar(conn, WED - timedelta(minutes=10))
    with pytest.raises(OSError):
        machine.observe(WED, _ok())

    assert _row(conn)["state"] == "restricted"
    assert _row(conn)["epoch"] == 1
    assert machine.state == "restricted"
    assert conn.execute("SELECT count(*) FROM datafeed_outage_gap WHERE epoch=1"
                        ).fetchone()[0] == 1
    # 次の tick は確定済みの state から続く (遷移を二重に起こさない)。
    machine.activity = None
    assert machine.observe(WED + timedelta(minutes=1), _ok()) == "restricted"
    assert _row(conn)["epoch"] == 1


def test_fail_soft_activity_write_failure_still_commits_the_transition(tmp_path):
    # 書き込み先がディレクトリで常に失敗する実 sink (例外は呼び出し元へ出ない契約)
    broken = tmp_path / "activity-dir"
    broken.mkdir()
    log = ActivityLog(broken)
    conn, machine = _rig(tmp_path, activity=log)
    _bar(conn, WED - timedelta(minutes=10))

    assert machine.observe(WED, _ok()) == "restricted"
    assert _row(conn)["state"] == "restricted"
    assert machine.state == "restricted"


# ---- 複数 key -------------------------------------------------------------


def test_simultaneous_stalls_use_the_smallest_expected_as_the_restricted_origin(tmp_path):
    conn, machine = _rig(tmp_path, keys=(KEY, OTHER_KEY))
    _bar(conn, datetime(2026, 9, 30, 9, 55, tzinfo=UTC), OTHER)   # expected 09:57:30
    _bar(conn, datetime(2026, 9, 30, 9, 50, tzinfo=UTC), PAIR)    # expected 09:52:30

    assert machine.observe(WED, _ok(KEY, OTHER_KEY)) == "restricted"
    stored = _row(conn)
    assert stored["restricted_since"] == "2026-09-30T09:52:30+00:00"
    assert stored["restricted_deadline_at"] == "2026-09-30T10:22:30+00:00"
    assert stored["epoch"] == 1


def test_a_later_stall_on_another_key_does_not_extend_the_deadline(tmp_path):
    conn, machine = _rig(tmp_path, keys=(KEY, OTHER_KEY))
    _bar(conn, datetime(2026, 9, 30, 9, 50, tzinfo=UTC), PAIR)
    _bar(conn, WED - timedelta(minutes=2), OTHER)
    assert machine.observe(WED, _ok(KEY, OTHER_KEY)) == "restricted"
    first = _row(conn)["restricted_deadline_at"]

    later = WED + timedelta(minutes=5)
    _bar(conn, later - timedelta(minutes=2), PAIR)
    # EURUSD は 10:00 以降の足が来ず、10:05 に停滞する
    assert machine.observe(later, _ok(KEY, OTHER_KEY)) == "restricted"
    stored = _row(conn)
    assert stored["restricted_deadline_at"] == first == "2026-09-30T10:22:30+00:00"
    assert stored["epoch"] == 1


def test_ready_requires_every_configured_key_to_be_healthy(tmp_path):
    conn, machine = _rig(tmp_path, keys=(KEY, OTHER_KEY))
    _bar(conn, datetime(2026, 9, 30, 9, 50, tzinfo=UTC), PAIR)
    _bar(conn, WED - timedelta(minutes=2), OTHER)
    assert machine.observe(WED, _ok(KEY, OTHER_KEY)) == "restricted"

    for minute in (1, 2, 3, 4):
        now = WED + timedelta(minutes=minute)
        _bar(conn, now - timedelta(minutes=2), PAIR)   # USDJPY だけ健全
        assert machine.observe(now, _ok(KEY, OTHER_KEY)) == "restricted"


def test_stall_of_a_symbol_outside_the_configured_set_is_ignored(tmp_path):
    conn, machine = _rig(tmp_path)
    _bar(conn, WED - timedelta(minutes=2))
    _bar(conn, WED - timedelta(minutes=30), "GBPUSD")

    assert machine.observe(WED, _ok()) == "ready"
    assert _row(conn)["epoch"] == 0
