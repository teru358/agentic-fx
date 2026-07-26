from datetime import datetime, timezone

from agentic_fx.core.market_hours import (
    is_friday_after, is_market_open, next_rollover, trading_day_start,
)


def _dt(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_weekend_closed():
    assert is_market_open(_dt(2026, 7, 22, 12, 0)) is True    # 水曜
    assert is_market_open(_dt(2026, 7, 24, 20, 59)) is True   # 金 20:59
    assert is_market_open(_dt(2026, 7, 24, 21, 0)) is False   # 金 21:00
    assert is_market_open(_dt(2026, 7, 25, 12, 0)) is False   # 土
    assert is_market_open(_dt(2026, 7, 26, 20, 59)) is False  # 日 20:59
    assert is_market_open(_dt(2026, 7, 26, 21, 0)) is True    # 日 21:00


def test_trading_day_start():
    assert trading_day_start(_dt(2026, 7, 22, 12, 0)) == _dt(2026, 7, 21, 21, 0)
    assert trading_day_start(_dt(2026, 7, 22, 22, 0)) == _dt(2026, 7, 22, 21, 0)


def test_next_rollover():
    assert next_rollover(_dt(2026, 7, 22, 12, 0)) == _dt(2026, 7, 22, 21, 0)
    assert next_rollover(_dt(2026, 7, 22, 21, 0)) == _dt(2026, 7, 23, 21, 0)


def test_friday_cutoff():
    assert is_friday_after(_dt(2026, 7, 24, 18, 30), "18:00") is True
    assert is_friday_after(_dt(2026, 7, 24, 17, 59), "18:00") is False
    assert is_friday_after(_dt(2026, 7, 23, 19, 0), "18:00") is False  # 木曜
