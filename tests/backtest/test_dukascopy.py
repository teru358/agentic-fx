import lzma
import struct
from datetime import datetime, timezone

import pytest

from agentic_fx.backtest.dukascopy import Tick, decode_bi5, hour_url, point_of

H = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _bi5(records: list[tuple[int, int, int, float, float]]) -> bytes:
    raw = b"".join(struct.pack(">3i2f", *r) for r in records)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def test_decode_roundtrip_jpy_point():
    payload = _bi5([(1500, 148_205, 148_193, 1.2, 0.8)])  # ask, bid (points)
    ticks = decode_bi5(payload, point=1e-3, hour_start_utc=H)
    assert ticks == [Tick(H.replace(second=1, microsecond=500_000),
                          148.193, 148.205)]


def test_decode_rejects_negative_and_inverted():
    bad = _bi5([(0, 148_000, 148_100, 1.0, 1.0)])  # ask < bid (逆転)
    assert decode_bi5(bad, point=1e-3, hour_start_utc=H) == []


def test_hour_url_month_is_zero_based():
    assert hour_url("USDJPY", H) == (
        "https://datafeed.dukascopy.com/datafeed/USDJPY/2026/06/22/12h_ticks.bi5")


def test_point_of_uses_metadata_table():
    assert point_of("USDJPY") == 1e-3
    assert point_of("EURUSD") == 1e-5
    with pytest.raises(KeyError):
        point_of("GBPZAR")  # 表に無い symbol は明示エラー (文字列推測禁止)


def test_points_table_consistent_with_trading_specs():
    """取引ペアについては _SPECS の quote_currency と表の整合をピン。"""
    from agentic_fx.datafeed.price_provider import _SPECS
    for sym, spec in _SPECS.items():
        expected = 1e-3 if spec.quote_currency == "JPY" else 1e-5
        assert point_of(sym) == expected


def test_decode_rejects_out_of_range_ms_offset():
    """ms_offset が [0, 3600000) 範囲外のレコードを棄却する。"""
    # ms_offset < 0: rejected
    bad_negative = _bi5([(-1, 148_205, 148_193, 1.0, 1.0)])
    assert decode_bi5(bad_negative, point=1e-3, hour_start_utc=H) == []

    # ms_offset >= 3600000: rejected (1 hour = 3600000 ms)
    bad_overflow = _bi5([(3_600_000, 148_205, 148_193, 1.0, 1.0)])
    assert decode_bi5(bad_overflow, point=1e-3, hour_start_utc=H) == []

    # boundary: ms_offset = 3599999 should pass (if bid/ask valid)
    valid_boundary = _bi5([(3_599_999, 148_205, 148_193, 1.0, 1.0)])
    ticks = decode_bi5(valid_boundary, point=1e-3, hour_start_utc=H)
    assert len(ticks) == 1


def test_hour_url_requires_aware_utc_datetime():
    """hour_url は naive または non-UTC datetime で ValueError を raise する。"""
    from datetime import timezone as tz
    from datetime import timedelta

    naive = datetime(2026, 7, 22, 12, 0)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        hour_url("USDJPY", naive)

    # Non-UTC (JST = UTC+9)
    jst = tz(timedelta(hours=9))
    jst_dt = datetime(2026, 7, 22, 12, 0, tzinfo=jst)
    with pytest.raises(ValueError, match="must be UTC"):
        hour_url("USDJPY", jst_dt)


def test_decode_bi5_requires_aware_utc_datetime():
    """decode_bi5 は naive または non-UTC hour_start_utc で ValueError を raise する。"""
    from datetime import timezone as tz
    from datetime import timedelta

    payload = _bi5([(1500, 148_205, 148_193, 1.0, 1.0)])

    naive = datetime(2026, 7, 22, 12, 0)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        decode_bi5(payload, point=1e-3, hour_start_utc=naive)

    # Non-UTC (JST = UTC+9)
    jst = tz(timedelta(hours=9))
    jst_dt = datetime(2026, 7, 22, 12, 0, tzinfo=jst)
    with pytest.raises(ValueError, match="must be UTC"):
        decode_bi5(payload, point=1e-3, hour_start_utc=jst_dt)


def test_decode_bi5_rejects_invalid_point():
    """decode_bi5 は NaN, inf, <= 0 の point で ValueError を raise する。"""
    import math

    payload = _bi5([(1500, 148_205, 148_193, 1.0, 1.0)])

    # NaN
    with pytest.raises(ValueError, match="must be finite"):
        decode_bi5(payload, point=float('nan'), hour_start_utc=H)

    # Infinity
    with pytest.raises(ValueError, match="must be finite"):
        decode_bi5(payload, point=float('inf'), hour_start_utc=H)

    # Negative
    with pytest.raises(ValueError, match="must be positive"):
        decode_bi5(payload, point=-1e-3, hour_start_utc=H)

    # Zero
    with pytest.raises(ValueError, match="must be positive"):
        decode_bi5(payload, point=0.0, hour_start_utc=H)
