from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.fx_time import session_boundary_utc
from agentic_fx.core.market_hours import (
    DAY_CLOSE_BUFFER,
    is_friday_after,
    is_market_open,
    next_expected_trading_time,
    next_rollover,
    session_start,
    trading_date,
    trading_day_start,
)


UTC = timezone.utc


def _dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize(("day", "expected"), [
    (datetime(2026, 7, 24).date(), _dt(2026, 7, 24, 21, 0)),
    (datetime(2026, 11, 6).date(), _dt(2026, 11, 6, 22, 0)),
    (datetime(2026, 3, 6).date(), _dt(2026, 3, 6, 22, 0)),
    (datetime(2026, 3, 9).date(), _dt(2026, 3, 9, 21, 0)),
    (datetime(2027, 3, 12).date(), _dt(2027, 3, 12, 22, 0)),
    (datetime(2027, 3, 15).date(), _dt(2027, 3, 15, 21, 0)),
])
def test_session_boundary_utc_follows_new_york_dst(day, expected):
    assert session_boundary_utc(day) == expected


@pytest.mark.parametrize(("instant", "open_"), [
    (_dt(2026, 10, 30, 20, 59), True),
    (_dt(2026, 10, 30, 21, 0), False),
    (_dt(2026, 11, 6, 21, 59), True),
    (_dt(2026, 11, 6, 22, 0), False),
    (_dt(2026, 11, 1, 21, 59), False),
    (_dt(2026, 11, 1, 22, 0), True),
    (_dt(2027, 3, 14, 20, 59), False),
    (_dt(2027, 3, 14, 21, 0), True),
])
def test_weekend_open_and_close_follow_new_york_1700(instant, open_):
    assert is_market_open(instant) is open_


@pytest.mark.parametrize(("instant", "start", "rollover", "label"), [
    (_dt(2026, 11, 4, 12, 0), _dt(2026, 11, 3, 22, 0), _dt(2026, 11, 4, 22, 0), "2026-11-04"),
    (_dt(2026, 7, 22, 12, 0), _dt(2026, 7, 21, 21, 0), _dt(2026, 7, 22, 21, 0), "2026-07-22"),
    (_dt(2026, 11, 4, 22, 0), _dt(2026, 11, 4, 22, 0), _dt(2026, 11, 5, 22, 0), "2026-11-05"),
    (_dt(2026, 11, 1, 23, 0), _dt(2026, 11, 1, 22, 0), _dt(2026, 11, 2, 22, 0), "2026-11-02"),
    (_dt(2026, 3, 8, 21, 30), _dt(2026, 3, 8, 21, 0), _dt(2026, 3, 9, 21, 0), "2026-03-09"),
])
def test_trading_day_values_use_new_york_calendar_dates(instant, start, rollover, label):
    assert trading_day_start(instant) == start
    assert next_rollover(instant) == rollover
    assert trading_date(instant).isoformat() == label


def test_rollover_uses_calendar_days_across_dst_closed_weekend():
    assert next_rollover(_dt(2026, 10, 31, 21, 30)) == _dt(2026, 11, 1, 22, 0)
    assert next_rollover(_dt(2026, 3, 7, 22, 30)) == _dt(2026, 3, 8, 21, 0)


def test_holidays_follow_the_trading_date_at_winter_boundary():
    assert is_market_open(_dt(2025, 12, 24, 21, 59)) is True
    assert is_market_open(_dt(2025, 12, 24, 22, 0)) is False
    assert is_market_open(_dt(2025, 12, 25, 21, 59)) is False
    assert is_market_open(_dt(2025, 12, 25, 22, 0)) is True
    assert is_market_open(_dt(2026, 12, 27, 21, 59)) is False
    assert is_market_open(_dt(2026, 12, 27, 22, 0)) is True


def test_session_start_and_next_expected_bar_use_dst_boundaries():
    assert session_start(_dt(2026, 11, 4, 12, 0)) == _dt(2026, 11, 1, 22, 0)
    assert session_start(_dt(2026, 3, 10, 12, 0)) == _dt(2026, 3, 8, 21, 0)
    assert next_expected_trading_time(_dt(2026, 11, 6, 21, 55), timedelta(minutes=5)) == _dt(2026, 11, 8, 22, 0)


def test_winter_friday_cutoff_is_still_new_york_local_time():
    assert is_friday_after(_dt(2026, 11, 6, 18, 59), "14:00") is False
    assert is_friday_after(_dt(2026, 11, 6, 19, 0), "14:00") is True


def test_day_close_deadline_is_five_minutes_before_the_winter_rollover():
    assert next_rollover(_dt(2026, 11, 4, 10, 0)) - DAY_CLOSE_BUFFER == _dt(2026, 11, 4, 21, 55)
