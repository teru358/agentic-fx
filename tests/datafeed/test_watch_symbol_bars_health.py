"""参照銘柄の足は FX の鮮度・開場 gap の検査に通さない (鮮度は alignment の max_age が持つ)。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar, FixedClock
from agentic_fx.datafeed.health import DataUnhealthy, validate_bars
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.store.db import connect, init_db

REPO = Path(__file__).resolve().parents[2]
# 金曜 14:05 UTC: FX は開場中。日本株価指数 CFD (JP225) は取引時間外。
NOW = datetime(2026, 1, 2, 14, 5, tzinfo=timezone.utc)
RAW = "agentic_fx.datafeed.price_provider.sources.mt5_raw_bars_range"
VENDOR = "agentic_fx.datafeed.price_provider.sources.mt5_bars"


def make_provider(tmp_path):
    settings = load_settings(REPO / "config" / "settings.yaml.example")
    settings.datafeed.primary = "mt5"
    settings.datafeed.mt5.enabled = True
    settings.datafeed.yfinance.enabled = False
    settings.datafeed.watch_symbols = ["JP225"]
    conn = connect(tmp_path / "p.db")
    init_db(conn)
    return PriceProvider(conn, settings, FixedClock(NOW), readonly=True), conn


def series(symbol, last, count=120, hole=None, close=lambda i: 100.0 + i * 0.01):
    """最後の足の開始時刻が `last` の 1h 足。`hole` = (開始 index, 本数) はその分の欠損。"""
    out = []
    for i in range(count):
        if hole and hole[0] <= i < hole[0] + hole[1]:
            continue
        ts = last - timedelta(hours=count - 1 - i)
        c = close(i)
        out.append(Bar(symbol, "1h", ts, c, c + 0.5, c - 0.5, c, 1))
    return out


def test_a_closed_index_market_does_not_make_the_watch_frame_unhealthy(tmp_path):
    provider, conn = make_provider(tmp_path)
    last = NOW - timedelta(hours=6)  # 指数は 6 時間前から休止中
    with patch(RAW, return_value=series("JP225", last)):
        bars = provider.get_bars("JP225", "1h")
    assert bars and bars[-1].ts <= last
    conn.close()


def test_an_index_gap_that_the_fx_calendar_calls_open_market_is_not_unhealthy(tmp_path):
    provider, conn = make_provider(tmp_path)
    last = NOW - timedelta(hours=1)
    with patch(RAW, return_value=series("JP225", last, hole=(100, 8))):  # 末尾窓内の 8 本欠損
        assert provider.get_bars("JP225", "1h")
    conn.close()


def test_the_same_frames_still_fail_for_a_trading_pair(tmp_path):
    provider, conn = make_provider(tmp_path)
    with patch(VENDOR, return_value=series("USDJPY", NOW - timedelta(hours=6))):
        with pytest.raises(DataUnhealthy):
            provider.get_bars("USDJPY", "1h")
    with patch(VENDOR, return_value=series("USDJPY", NOW - timedelta(hours=1), hole=(100, 8))):
        with pytest.raises(DataUnhealthy):
            provider.get_bars("USDJPY", "1h")
    conn.close()


@pytest.mark.parametrize("bad", ["zero", "nan", "spike"])
def test_a_watch_frame_is_still_rejected_for_anomalous_values(tmp_path, bad):
    provider, conn = make_provider(tmp_path)
    last = NOW - timedelta(hours=6)

    def close(i):
        if i == 60:
            return {"zero": 0.0, "nan": float("nan"), "spike": 1000.0}[bad]
        return 100.0

    with patch(RAW, return_value=series("JP225", last, close=close)):
        with pytest.raises(DataUnhealthy):
            provider.get_bars("JP225", "1h")
    conn.close()


def test_a_derived_four_hour_watch_frame_is_not_stale_checked_either(tmp_path):
    provider, conn = make_provider(tmp_path)
    with patch(RAW, return_value=series("JP225", NOW - timedelta(hours=9), count=700)):
        assert provider.get_bars("JP225", "4h")
    conn.close()


def test_validate_bars_keeps_checking_the_calendar_by_default():
    stale = series("X", NOW - timedelta(hours=6))
    with pytest.raises(DataUnhealthy):
        validate_bars(stale, NOW, 30, 60)
    validate_bars(stale, NOW, 30, 60, check_calendar=False)


def test_every_validation_in_the_provider_passes_the_raw_watch_predicate():
    text = (REPO / "src" / "agentic_fx" / "datafeed" / "price_provider.py").read_text()
    assert len(re.findall(r"\bvalidate_bars\(", text)) == 5
    assert text.count("check_calendar=not self._is_raw_watch(pair)") == 5
