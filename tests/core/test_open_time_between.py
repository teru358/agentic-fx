"""開場している時間だけを積算する。区間は UTC の実時刻の例で固定する。"""
from datetime import datetime, timedelta, timezone

from agentic_fx.core.market_hours import open_time_between

UTC = timezone.utc


def _dt(*args):
    return datetime(*args, tzinfo=UTC)


def test_a_span_inside_one_open_session_counts_all_of_it():
    assert open_time_between(_dt(2026, 7, 22, 12, 0), _dt(2026, 7, 22, 12, 10)) == timedelta(minutes=10)


def test_the_daily_rollover_is_not_a_closure():
    # 夏: 21:00 UTC が日次境界。水曜は閉まらない
    assert open_time_between(_dt(2026, 7, 22, 20, 55), _dt(2026, 7, 22, 21, 5)) == timedelta(minutes=10)


def test_the_summer_weekend_closure_is_left_out():
    assert open_time_between(_dt(2026, 7, 24, 20, 55), _dt(2026, 7, 26, 21, 5)) == timedelta(minutes=10)


def test_the_winter_weekend_closure_is_left_out():
    assert open_time_between(_dt(2026, 12, 4, 21, 55), _dt(2026, 12, 6, 22, 5)) == timedelta(minutes=10)


def test_the_year_end_holiday_is_left_out():
    # 12-24 22:00 UTC (NY 17:00) から 12-27 22:00 UTC まで閉まる
    assert open_time_between(_dt(2026, 12, 24, 21, 55), _dt(2026, 12, 27, 22, 5)) == timedelta(minutes=10)


def test_a_span_that_lies_wholly_in_a_closure_is_zero():
    assert open_time_between(_dt(2026, 7, 25, 3, 0), _dt(2026, 7, 26, 12, 0)) == timedelta(0)


def test_an_empty_or_reversed_span_is_zero():
    at = _dt(2026, 7, 22, 12, 0)
    assert open_time_between(at, at) == timedelta(0)
    assert open_time_between(at + timedelta(minutes=5), at) == timedelta(0)


def test_a_long_span_adds_up_every_open_day():
    # 水曜 21:00 UTC から翌水曜 21:00 UTC: 7 日のうち 週末の 2 日 (金 21:00 〜 日 21:00) を除く 5 日
    assert open_time_between(_dt(2026, 7, 22, 21, 0), _dt(2026, 7, 29, 21, 0)) == timedelta(days=5)
