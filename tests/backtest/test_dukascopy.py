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
