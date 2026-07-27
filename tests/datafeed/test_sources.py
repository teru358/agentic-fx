from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from agentic_fx.datafeed.sources import (
    INTERVAL_MIN, NATIVE_INTERVALS, mt5_bars, mt5_bars_range, mt5_quote,
    td_bars, td_quote, vendor_symbol, yf_bars, yf_quote,
)

IDX = pd.DatetimeIndex(
    [datetime(2026, 7, 22, 11, 58, tzinfo=timezone.utc),
     datetime(2026, 7, 22, 11, 59, tzinfo=timezone.utc)])
_DATA = {"Open": [148.4, 148.45], "High": [148.5, 148.55],
         "Low": [148.3, 148.4], "Close": [148.45, 148.5],
         "Volume": [100, 120]}
DF = pd.DataFrame(_DATA, index=IDX)
# 現行 yfinance デフォルトの (Price, Ticker) MultiIndex 列も検証する
DF_MULTI = pd.DataFrame(
    {(k, "USDJPY=X"): v for k, v in _DATA.items()}, index=IDX)
DF_MULTI.columns = pd.MultiIndex.from_tuples(DF_MULTI.columns)


def test_vendor_symbol_resolver():
    assert vendor_symbol("USDJPY", "yf") == "USDJPY=X"
    assert vendor_symbol("USDJPY", "td") == "USD/JPY"
    assert vendor_symbol("USDJPY", "mt5") == "USDJPY"
    with pytest.raises(KeyError):
        vendor_symbol("GBPUSD", "yf")  # 未定義は KeyError


def test_yf_bars_maps_dataframe():
    with patch("yfinance.download", return_value=DF) as dl:
        bars = yf_bars("USDJPY", "1m", 1)
    assert dl.call_args[0][0] == "USDJPY=X"
    assert dl.call_args[1]["multi_level_index"] is False
    assert len(bars) == 2
    assert bars[-1].close == 148.5


def test_yf_bars_normalizes_multiindex():
    # multi_level_index=False が効かない旧版でも列を単層化して読めること
    with patch("yfinance.download", return_value=DF_MULTI):
        bars = yf_bars("USDJPY", "1m", 1)
    assert len(bars) == 2
    assert bars[-1].close == 148.5


def test_yf_quote_uses_last_close():
    with patch("yfinance.download", return_value=DF):
        q = yf_quote("USDJPY")
    assert q.bid == q.ask == 148.5
    assert q.source == "yfinance"


def test_mt5_quote_parses_bridge_response():
    resp = MagicMock()
    resp.json.return_value = {"bid": 148.49, "ask": 148.51,
                              "time": "2026-07-22T11:59:30+00:00"}
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp) as g:
        q = mt5_quote("http://localhost:8812", "USDJPY")
    assert "quote" in g.call_args[0][0]
    assert q.bid == 148.49 and q.source == "mt5"


def test_td_quote():
    resp = MagicMock()
    resp.json.return_value = {"symbol": "USD/JPY", "bid": "148.49",
                              "ask": "148.51",
                              "timestamp": 1784721570}
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp) as g:
        q = td_quote("key", "USDJPY")
    assert g.call_args[1]["params"]["symbol"] == "USD/JPY"
    assert q.bid == 148.49 and q.source == "twelvedata"
    assert q.ts.tzinfo is not None


def test_td_bars_parses_and_maps_interval():
    resp = MagicMock()
    resp.json.return_value = {"values": [
        {"datetime": "2026-07-22 11:59:00", "open": "148.45",
         "high": "148.55", "low": "148.40", "close": "148.50"},
        {"datetime": "2026-07-22 11:58:00", "open": "148.40",
         "high": "148.50", "low": "148.30", "close": "148.45"},
    ]}
    resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=resp) as g:
        bars = td_bars("key", "USDJPY", "1m", 30)
    params = g.call_args[1]["params"]
    assert params["interval"] == "1min"      # TD の interval 表記へ変換
    assert params["outputsize"] <= 5000      # API 上限で clamp
    assert params["symbol"] == "USD/JPY"
    assert bars[0].ts < bars[1].ts           # 昇順に並べ替え
    assert bars[-1].close == 148.5


def test_interval_table_covers_all_supported_timeframes():
    """取引の時間軸を固定しないため、4h / 30m も表に載る (spec §5)。"""
    assert INTERVAL_MIN["1h"] == 60
    assert INTERVAL_MIN["4h"] == 240
    assert INTERVAL_MIN["30m"] == 30


def test_native_intervals_reflect_source_capability():
    """足の可否はシステムの仕様ではなくソースの能力として持つ。"""
    assert "4h" in NATIVE_INTERVALS["mt5"]        # MT5 は H4 をネイティブに持つ
    assert "4h" not in NATIVE_INTERVALS["yfinance"]  # yfinance は持たない
    assert "30m" not in NATIVE_INTERVALS["yfinance"]
    # 全ソースのネイティブ足は INTERVAL_MIN に載っていること
    for src, ivs in NATIVE_INTERVALS.items():
        assert ivs <= set(INTERVAL_MIN), f"{src} に未知の足がある"
