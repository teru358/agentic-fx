"""同期の範囲計画 (純関数): 開場時間の積算・固定尾・prefix・要求窓。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.backtest import history_sync_plan as plan
from agentic_fx.core.market_hours import is_market_open

UTC = timezone.utc
H = timedelta(hours=1)


def dt(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


# 2026-11-13 は金曜 (冬時間: 22:00Z に閉場、日曜 22:00Z に開場)
FRI = dt(2026, 11, 13)
SUN_OPEN = dt(2026, 11, 15, 22)


def test_effective_end_leaves_the_confirmation_margin_bar_out():
    w = timedelta(minutes=15)
    now = dt(2026, 11, 11, 12, 0, 7)
    assert plan.effective_end(now, now, grace=timedelta(seconds=30), width=w) == dt(
        2026, 11, 11, 11, 30)
    assert plan.effective_end(dt(2026, 11, 1), now, grace=timedelta(seconds=30), width=w) == dt(
        2026, 11, 1)


def test_clip_cuts_to_b_and_e_and_returns_none_when_empty():
    b, e = dt(2026, 10, 11, 12), dt(2026, 11, 11, 11)
    assert plan.clip(dt(2026, 10, 1), dt(2026, 10, 12), boundary=b, limit=e) == (
        b, dt(2026, 10, 12))
    assert plan.clip(dt(2026, 11, 11), dt(2026, 11, 12), boundary=b, limit=e) == (
        dt(2026, 11, 11), e)
    assert plan.clip(dt(2026, 10, 1), dt(2026, 10, 11), boundary=b, limit=e) is None


def test_advance_and_retreat_count_only_open_time_across_the_weekend():
    # 金曜 21:00Z から 2 開場時間 = 金曜 22:00 閉場までの 1 時間 + 日曜 22:00 開場後の 1 時間
    assert plan.advance_open(FRI.replace(hour=21), 2 * H) == dt(2026, 11, 15, 23)
    assert plan.retreat_open(dt(2026, 11, 15, 23), 2 * H) == FRI.replace(hour=21)
    assert plan.advance_open(dt(2026, 11, 11, 10), timedelta(0)) == dt(2026, 11, 11, 10)
    assert plan.advance_open(dt(2026, 11, 11, 10), 2 * H) == dt(2026, 11, 11, 12)
    assert plan.retreat_open(dt(2026, 11, 11, 12), 3 * H) == dt(2026, 11, 11, 9)
    with pytest.raises(plan.PlanError):
        plan.advance_open(dt(2026, 11, 11), timedelta(days=60), is_open=lambda t: False)


def test_open_duration_and_slots_skip_closed_time_and_excluded_dates():
    w = timedelta(minutes=15)
    assert plan.open_duration(FRI.replace(hour=20), dt(2026, 11, 15, 23)) == 3 * H
    slots = plan.open_slots(FRI.replace(hour=21), dt(2026, 11, 15, 23), width=w)
    assert len(slots) == 8
    assert plan.open_slots(dt(2026, 11, 11), dt(2026, 11, 12), width=w,
                           excluded_dates=frozenset({dt(2026, 11, 11).date()})) == []


def test_fetch_windows_are_utc_days_and_fully_closed_days_are_not_requested():
    windows = plan.fetch_windows(dt(2026, 11, 13, 20), dt(2026, 11, 15, 23))
    # 金曜 (開場の時間あり)、土曜 (全日閉場: 数えない)、日曜 (22:00 以降は開場)
    assert windows == [(dt(2026, 11, 13, 20), dt(2026, 11, 14)),
                       (dt(2026, 11, 15), dt(2026, 11, 15, 23))]
    assert plan.request_count([(dt(2026, 11, 13, 20), dt(2026, 11, 15, 23))]) == 2
    # 日曜の recent で金・土・日を数えるとき、土曜は予算に入らない (D=2 窓)
    assert plan.request_count([(dt(2026, 11, 13), dt(2026, 11, 15, 23))]) == 2


def test_lookback_start_covers_the_current_utc_day_and_is_clipped_by_b():
    limit = dt(2026, 11, 11, 11, 30)
    b = dt(2026, 10, 11, 12)
    assert plan.lookback_start(limit, 2, b) == dt(2026, 11, 10)
    assert plan.lookback_start(limit, 31, b) == dt(2026, 10, 12)
    assert plan.lookback_start(limit, 40, b) == b


# ---- 検査尾 ---------------------------------------------------------------

def _keys(end, count, width, step_from=None):
    out, t = [], end - width
    while len(out) < count:
        if is_market_open(t):
            out.append(t)
        t -= width
    return sorted(out)


@pytest.mark.parametrize("minutes,need", [(1, 120), (5, 24), (15, 8)])
def test_base_tail_is_two_open_hours_and_is_not_extended_when_k_keys_exist(minutes, need):
    w = timedelta(minutes=minutes)
    anchor = dt(2026, 11, 11, 12)
    result = plan.select_tail(_keys(anchor, need + 5, w), anchor, width=w)
    assert result.ok
    assert result.tail.start == dt(2026, 11, 11, 10) and result.tail.end == anchor
    assert result.tail.keys >= need and result.tail.slots == 2 * H // w


@pytest.mark.parametrize("minutes,need", [(1, 120), (5, 24), (15, 8)])
def test_one_missing_key_is_filled_by_the_nearest_older_existing_key(minutes, need):
    w = timedelta(minutes=minutes)
    anchor = dt(2026, 11, 11, 12)
    keys = _keys(anchor, need + 10, w)
    keys.remove(anchor - w)                     # 直前の 1 本が欠けている
    inside = [k for k in keys if k >= dt(2026, 11, 11, 10)]
    assert len(inside) == need - 1
    result = plan.select_tail(keys, anchor, width=w)
    assert result.ok
    older = max(k for k in keys if k < dt(2026, 11, 11, 10))
    assert result.tail.start == older and result.tail.keys == need
    assert result.tail.slots <= plan.TAIL_MAX_GRID


def test_a_tail_that_cannot_reach_k_keys_is_refused_before_any_request():
    w = timedelta(minutes=15)
    anchor = dt(2026, 11, 11, 12)
    result = plan.select_tail(_keys(anchor, 7, w), anchor, width=w)
    assert not result.ok and result.reason == plan.REASON_INSPECTION
    assert not plan.select_tail([], anchor, width=w).ok


def test_the_tail_never_exceeds_three_open_hours_or_180_grid_slots():
    w = timedelta(minutes=1)
    anchor = dt(2026, 11, 11, 12)
    keys = _keys(anchor, 40, w)                  # 直前 40 本しか無く、残りは遠い (3 時間超)
    far = [anchor - timedelta(hours=5) - i * w for i in range(200)]
    result = plan.select_tail(keys + far, anchor, width=w)
    assert not result.ok and result.reason == plan.REASON_TAIL_LIMIT
    near = [anchor - timedelta(minutes=170 + i) for i in range(30)]   # 170〜199 分前 (3h 弱の外)
    r2 = plan.select_tail(keys + near, anchor, width=w)
    assert not r2.ok


def test_tail_keys_outside_the_calendar_or_excluded_dates_or_before_b_are_ignored():
    w = timedelta(minutes=15)
    anchor = dt(2026, 11, 11, 12)
    keys = _keys(anchor, 8, w)
    assert plan.select_tail(keys, anchor, width=w,
                            excluded_dates=frozenset({anchor.date()})).ok is False
    assert plan.select_tail(keys, anchor, width=w, boundary=anchor - timedelta(minutes=30)
                            ).ok is False
    closed = [dt(2026, 11, 14, 10) + i * w for i in range(20)]            # 土曜 (閉場)
    assert plan.select_tail(closed, dt(2026, 11, 14, 14), width=w).ok is False


def test_tail_across_a_weekend_counts_open_time_only():
    w = timedelta(minutes=15)
    anchor = dt(2026, 11, 15, 23)                # 日曜 22:00 開場の 1 時間後
    keys = [dt(2026, 11, 15, 22) + i * w for i in range(4)] + _keys(FRI.replace(hour=22), 8, w)
    result = plan.select_tail(keys, anchor, width=w)
    assert result.ok
    assert result.tail.start == FRI.replace(hour=21) and result.tail.slots == 8


# ---- prefix ---------------------------------------------------------------

def test_prefix_advances_two_open_hours_and_is_bounded_by_e():
    b, e = dt(2026, 10, 11, 12), dt(2026, 11, 11, 11, 30)
    p = plan.plan_prefix(dt(2026, 11, 11, 8), boundary=b, limit=e)
    assert (p.ok, p.cursor, p.end) == (True, dt(2026, 11, 11, 8), dt(2026, 11, 11, 10))
    p = plan.plan_prefix(dt(2026, 11, 11, 11), boundary=b, limit=e)
    assert p.end == e
    p = plan.plan_prefix(dt(2026, 11, 11, 11, 30), boundary=b, limit=e)
    assert p.ok and p.end == p.cursor                       # 進める範囲なし


def test_prefix_uninitialized_cursor_and_backlog_go_to_the_offline_path():
    b, e = dt(2026, 10, 11, 12), dt(2026, 11, 11, 11, 30)
    assert plan.plan_prefix(None, boundary=b, limit=e).reason == plan.REASON_UNINITIALIZED
    late = plan.plan_prefix(dt(2026, 11, 10, 10), boundary=b, limit=e)
    assert (late.ok, late.reason) == (False, plan.REASON_BACKLOG)         # 25.5 開場時間
    edge = plan.plan_prefix(dt(2026, 11, 10, 11, 30), boundary=b, limit=e)
    assert edge.ok                                                         # ちょうど 24 開場時間
    assert plan.plan_prefix(dt(2026, 9, 1), boundary=b, limit=e).reason == plan.REASON_BACKLOG


def test_prefix_cursor_is_clipped_up_to_b():
    b, e = dt(2026, 11, 11, 10), dt(2026, 11, 11, 11, 30)
    assert plan.plan_prefix(dt(2026, 10, 1), boundary=b, limit=e).cursor == b


def test_default_calendar_is_the_shared_market_hours_function():
    assert plan.is_market_open is is_market_open
