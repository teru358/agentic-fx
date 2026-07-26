from datetime import datetime, timedelta, timezone

import pytest

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


def test_timezone_normalization():
    """非 UTC の tz-aware datetime を渡した場合、UTC に正規化して判定する"""
    jst = timezone(timedelta(hours=9))
    # 2026-07-25 05:00 JST = 2026-07-24 20:00 UTC (金曜 20:00、市場 open)
    jst_time = datetime(2026, 7, 25, 5, 0, tzinfo=jst)
    utc_time = datetime(2026, 7, 24, 20, 0, tzinfo=timezone.utc)

    # 同じ瞬間でも JST では土曜日が壁時計だが、UTC では金曜 20:00
    # → UTC に正規化すれば True (市場 open)
    assert is_market_open(jst_time) is True
    assert is_market_open(utc_time) is True

    # trading_day_start も同じ瞬間なら同じ UTC 時刻を返す
    # 20:00 は 21:00 より前なので、前日（2026-07-23）のロールオーバーを返す
    assert trading_day_start(jst_time) == _dt(2026, 7, 23, 21, 0)
    assert trading_day_start(utc_time) == _dt(2026, 7, 23, 21, 0)

    # next_rollover も同じ
    assert next_rollover(jst_time) == _dt(2026, 7, 24, 21, 0)
    assert next_rollover(utc_time) == _dt(2026, 7, 24, 21, 0)

    # is_friday_after: JST では土曜、UTC では金曜
    # → UTC に正規化すれば False (金曜 20:00 は "21:00" cutoff 前なので False)
    assert is_friday_after(jst_time, "21:00") is False
    assert is_friday_after(utc_time, "21:00") is False


def test_naive_datetime_rejected():
    """naive datetime (tzinfo なし) は ValueError を送出する"""
    naive = datetime(2026, 7, 24, 12, 0)  # tzinfo=None

    with pytest.raises(ValueError, match="timezone-aware"):
        is_market_open(naive)

    with pytest.raises(ValueError, match="timezone-aware"):
        trading_day_start(naive)

    with pytest.raises(ValueError, match="timezone-aware"):
        next_rollover(naive)

    with pytest.raises(ValueError, match="timezone-aware"):
        is_friday_after(naive, "18:00")
