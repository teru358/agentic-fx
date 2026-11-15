"""recent の範囲: 始端は max(B, E - D 日)、要求窓は追記・後ろ窓・検査尾の和集合 (D+2 上限)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.backtest import history_sync as hs
from agentic_fx.backtest import history_sync_plan as plan
from agentic_fx.backtest.holdout import in_sample_until
from agentic_fx.store import history_sync as store
from tests.backtest.history_sync_support import (
    SYMBOL, UTC, WIDTH, FakeBridge, conn_at, grid, make_settings, seed)
from tests.backtest.test_history_sync import _run

ALWAYS_OPEN = lambda _t: True  # noqa: E731


def dt(*a):
    return datetime(*a, tzinfo=UTC)


def _windows(end, days, *, base_hours, boundary=dt(2020, 1, 1)):
    """連続開場暦で、後ろ窓の始端から固定尾を付けた要求窓の数。"""
    start = plan.lookback_start(end, days, boundary)
    keys = [t for t in grid(start - timedelta(hours=6), start)]
    result = plan.select_tail(keys, start, width=WIDTH, is_open=ALWAYS_OPEN, boundary=boundary)
    assert result.ok
    tail_start = max(result.tail.start, boundary)
    return plan.request_count([(tail_start, end)], is_open=ALWAYS_OPEN)


@pytest.mark.parametrize("days", [2, 31])
@pytest.mark.parametrize("hour,expected_extra", [(0, 1), (1, 2), (3, 1), (18, 1)])
def test_window_count_of_the_normal_tail_by_the_hour_of_e(days, hour, expected_extra):
    end = dt(2026, 11, 12, hour)
    assert _windows(end, days, base_hours=2) == days + expected_extra
    assert _windows(end, days, base_hours=2) <= days + hs.RECENT_EXTRA_WINDOWS


def test_a_three_hour_extended_tail_still_fits_in_d_plus_two():
    end = dt(2026, 11, 12, 1)
    start = plan.lookback_start(end, 7, dt(2020, 1, 1))
    # 基本尾 (直前 2 時間) に既存 key が無く、古い key で 3 時間まで拡張する
    keys = [*grid(start - timedelta(hours=3), start - timedelta(hours=2)),
            *grid(start - timedelta(hours=1), start)]
    result = plan.select_tail(keys, start, width=WIDTH, is_open=ALWAYS_OPEN,
                              boundary=dt(2020, 1, 1))
    assert result.ok and result.tail.start == start - timedelta(hours=3)
    assert plan.request_count([(result.tail.start, end)], is_open=ALWAYS_OPEN) == 7 + 2


def test_overlapping_windows_are_counted_once():
    a = (dt(2026, 11, 9, 22), dt(2026, 11, 10, 12))
    b = (dt(2026, 11, 10, 6), dt(2026, 11, 11, 3))
    c = (dt(2026, 11, 10, 8), dt(2026, 11, 10, 9))
    assert plan.request_count([a, b, c], is_open=ALWAYS_OPEN) == 3
    assert plan.request_count([a, a], is_open=ALWAYS_OPEN) == plan.request_count(
        [a], is_open=ALWAYS_OPEN) == 2
    merged = plan.request_windows([a, b], is_open=ALWAYS_OPEN)
    assert [w[0].date() for w in merged] == [dt(2026, 11, 9).date(), dt(2026, 11, 10).date(),
                                             dt(2026, 11, 11).date()]


def test_the_start_is_clipped_by_b_and_shrinks_the_window_count():
    boundary = dt(2026, 11, 11, 12)
    end = dt(2026, 11, 12, 1)
    assert plan.lookback_start(end, 31, boundary) == boundary
    assert plan.request_count([(boundary, end)], is_open=ALWAYS_OPEN) == 2


def test_the_weekend_does_not_add_request_windows_to_the_count():
    # 日曜 22:00Z 開場。月曜 00:30 の E で D=2: 後ろ窓は土曜 (全日閉場) から、尾は金曜の開場時間まで
    end = dt(2026, 11, 9, 0, 30)
    start = plan.lookback_start(end, 2, dt(2020, 1, 1))
    assert start == dt(2026, 11, 7, 0, 30)
    tail_start = plan.retreat_open(start, plan.TAIL_BASE_OPEN)
    assert tail_start == dt(2026, 11, 6, 20)
    windows = plan.request_windows([(tail_start, end)])
    assert [w[0].date().isoformat() for w in windows] == ["2026-11-06", "2026-11-08", "2026-11-09"]


def _fixture_at_e(tmp_path, now, *, days=2):
    conn = conn_at(tmp_path)
    settings = make_settings(holdout_months=1, lookback_days=days)
    boundary = in_sample_until(now, 1, base_interval="15m")
    limit = plan.effective_end(now, now, grace=timedelta(seconds=settings.datafeed.closed_bar_grace_sec),
                               width=WIDTH)
    seed(conn, SYMBOL, boundary, limit)
    return conn, settings, limit


def test_a_recent_operation_of_exactly_d_plus_two_windows_is_fetched(tmp_path):
    now = dt(2026, 11, 11, 1, 0, 7)      # E=00:30 -> 日曜 22:00Z の開場から 11-08..11-11 の 4 窓
    conn, settings, limit = _fixture_at_e(tmp_path, now)
    bridge = FakeBridge()
    result = _run(conn, settings, bridge, now=now)
    assert limit == dt(2026, 11, 11, 0, 30)
    assert result.requests == len(bridge.calls) == 2 + hs.RECENT_EXTRA_WINDOWS
    assert result.rc == 0 and result.cursors[SYMBOL] == limit.isoformat()


def test_a_recent_operation_over_the_window_cap_is_not_requested_and_stays_in_the_queue(
        tmp_path, monkeypatch):
    now = dt(2026, 11, 11, 1, 0, 7)
    conn, settings, limit = _fixture_at_e(tmp_path, now)
    monkeypatch.setattr(plan, "lookback_start",
                        lambda lim, d, b: lim - timedelta(days=d + 3))
    bridge = FakeBridge()
    result = _run(conn, settings, bridge, now=now)
    recent = next(o for o in result.ops if o.operation == "recent")
    assert (recent.status, recent.reason, recent.requests) == ("deferred", "request_budget", 0)
    rows = conn.execute("SELECT day_utc, state FROM history_sync_gaps WHERE kind='unprocessed' "
                        "ORDER BY day_utc").fetchall()
    assert rows and rows[0]["day_utc"] == "2026-11-06" and rows[-1]["day_utc"] == "2026-11-11"
    assert result.cursors[SYMBOL] is None


def test_the_old_cursor_append_beyond_the_cap_is_queued_explicitly(tmp_path):
    now = dt(2026, 11, 11, 12, 0, 7)
    conn, settings, limit = _fixture_at_e(tmp_path, now)
    store.write_txn(conn, lambda c: store.advance_cursor(
        c, "mt5", "15m", SYMBOL, last_processed_e=dt(2026, 11, 2, 12), run_id="old", now=now),
        sleep=lambda _s: None)
    result = _run(conn, settings, FakeBridge(), now=now)
    days = [r["day_utc"] for r in conn.execute(
        "SELECT day_utc FROM history_sync_gaps WHERE kind='unprocessed' ORDER BY day_utc")]
    # 後ろ窓は 11-09 11:30 から。cursor 11-02 12:00 からそこまでは日ごとの未取得として残り、解消される
    assert days[0] == "2026-11-02" and "2026-11-08" in days
    assert result.cursors[SYMBOL] == limit.isoformat()
