"""market の読み取り tool だけが参照銘柄を受ける。schema・実行時の所属検査・raw adapter 経路を組で見る。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar, FixedClock
from agentic_fx.datafeed import sources
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.loops.summary import trade_intent_schema
from agentic_fx.store.db import connect, init_db
from agentic_fx.tools import market_tools, signal_tools
from agentic_fx.tools.registry import ToolRegistry

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 1, 2, 0, 5, tzinfo=timezone.utc)


def settings_with(watch):
    settings = load_settings(REPO / "config" / "settings.yaml.example")
    settings.datafeed.primary = "mt5"
    settings.datafeed.mt5.enabled = True
    settings.datafeed.yfinance.enabled = False
    settings.datafeed.watch_symbols = list(watch)
    return settings


def bars(symbol, count=120, interval="1h"):
    last = NOW.replace(minute=0) - timedelta(hours=1)
    return [Bar(symbol, interval, last - timedelta(hours=count - 1 - i), 1.0, 2.0, 0.5, 1.5, 1)
            for i in range(count)]


def tools_of(settings, provider=None):
    provider = provider or MagicMock()
    return {t.name: t for t in market_tools.build(provider, MagicMock(), settings,
                                                  sandbox_run=lambda *a, **k: {})}


def real_provider(tmp_path, settings):
    conn = connect(tmp_path / "p.db")
    init_db(conn)
    return PriceProvider(conn, settings, FixedClock(NOW), readonly=True), conn


# --- schema ----------------------------------------------------------------------

def test_the_read_tools_enum_is_pairs_plus_watch_symbols():
    settings = settings_with(["JP225", "US500"])
    for name in ("get_ohlcv", "get_indicators"):
        schema = tools_of(settings)[name].parameters
        assert schema["properties"]["pair"]["enum"] == [*settings.pairs, "JP225", "US500"]
        assert schema["properties"]["timeframe"]["enum"] == list(settings.datafeed.intervals)


def test_the_shared_pair_param_is_not_widened():
    settings = settings_with(["JP225"])
    assert market_tools.pair_param(settings)["pair"]["enum"] == list(settings.pairs)


def test_get_signals_and_the_trade_intent_schema_stay_pairs_only(tmp_path):
    settings = settings_with(["JP225"])
    conn = connect(tmp_path / "s.db")
    init_db(conn)
    [tool] = signal_tools.build(conn, settings, FixedClock(NOW))
    assert tool.parameters["properties"]["pair"]["enum"] == list(settings.pairs)
    with pytest.raises(ValueError):
        tool.func("JP225")
    assert trade_intent_schema(settings.pairs)["properties"]["pair"]["enum"] == list(settings.pairs)
    # 実際の呼び出し元が watch ではなく pairs を渡していることを pin する
    for relative in ("loops/trade_loop.py", "service.py"):
        text = (REPO / "src" / "agentic_fx" / relative).read_text()
        calls = re.findall(r"trade_intent_schema\(([^)]*)\)", text)
        assert calls and all(argument.endswith("settings.pairs") for argument in calls), (relative, calls)
        assert "watch_symbols" not in "".join(calls)


def test_with_no_watch_symbols_the_tool_schemas_are_byte_identical_to_the_pair_only_schema():
    settings = settings_with([])
    old = market_tools.pair_param(settings)
    for name in ("get_ohlcv", "get_indicators"):
        assert json.dumps(tools_of(settings)[name].parameters["properties"], sort_keys=True) == \
            json.dumps(old, sort_keys=True)


# --- 実行時の所属検査 ---------------------------------------------------------------

@pytest.mark.parametrize("name", ["get_ohlcv", "get_indicators"])
def test_the_runtime_membership_is_checked_at_call_time_against_the_current_settings(name):
    settings = settings_with(["JP225"])
    provider = MagicMock()
    provider.get_bars.return_value = []
    tool = tools_of(settings, provider)[name]
    with pytest.raises(ValueError):
        tool.func("US500")
    provider.get_bars.assert_not_called()
    tool.func("JP225", "1h")
    settings.datafeed.watch_symbols = []  # 設定の集合が変わったら、すでに組んだ tool も拒む
    provider.get_bars.reset_mock()
    with pytest.raises(ValueError):
        tool.func("JP225", "1h")
    provider.get_bars.assert_not_called()


@pytest.mark.parametrize("name", ["get_ohlcv", "get_indicators"])
def test_the_registry_refuses_a_symbol_outside_the_enum_before_the_function_runs(name):
    settings = settings_with(["JP225"])
    provider = MagicMock()
    registry = ToolRegistry()
    registry.register_all(list(tools_of(settings, provider).values()))
    reply = json.loads(registry.execute(name, {"pair": "US500"}, [name]))
    assert "invalid arguments" in reply["error"]
    provider.get_bars.assert_not_called()


# --- raw adapter 経路 -------------------------------------------------------------

def test_a_watch_symbol_is_read_through_the_raw_adapter_never_the_vendor_table(tmp_path):
    settings = settings_with(["JP225"])
    provider, conn = real_provider(tmp_path, settings)
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_raw_bars_range",
               return_value=bars("JP225")) as raw, \
         patch("agentic_fx.datafeed.price_provider.sources.mt5_bars") as vendor:
        rows = tools_of(settings, provider)["get_ohlcv"].func("JP225", "1h")
    assert isinstance(rows, list) and len(rows) == 100
    vendor.assert_not_called()
    (url, symbol, interval, start, end), _ = raw.call_args
    assert symbol == sources.MT5RawSymbol("JP225") and interval == "1h" and end == NOW
    assert url == settings.datafeed.mt5.bridge_url
    conn.close()


def test_a_trading_pair_keeps_the_vendor_route(tmp_path):
    settings = settings_with(["JP225"])
    provider, conn = real_provider(tmp_path, settings)
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_raw_bars_range") as raw, \
         patch("agentic_fx.datafeed.price_provider.sources.mt5_bars",
               return_value=bars("USDJPY")) as vendor:
        tools_of(settings, provider)["get_ohlcv"].func("USDJPY", "1h")
    raw.assert_not_called()
    vendor.assert_called_once()
    conn.close()


def test_get_indicators_also_reads_a_watch_symbol_through_the_raw_adapter(tmp_path):
    settings = settings_with(["JP225"])
    provider, conn = real_provider(tmp_path, settings)
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_raw_bars_range",
               return_value=bars("JP225", 200)) as raw, \
         patch("agentic_fx.datafeed.price_provider.sources.mt5_bars") as vendor:
        result = tools_of(settings, provider)["get_indicators"].func("JP225", "1h")
    assert result["interval"] == "1h" and "sma_20" in result
    raw.assert_called()
    vendor.assert_not_called()
    conn.close()


def test_a_derived_four_hour_watch_read_fetches_one_hour_bars_through_the_raw_adapter(tmp_path):
    settings = settings_with(["JP225"])
    provider, conn = real_provider(tmp_path, settings)
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_raw_bars_range",
               return_value=bars("JP225", 700)) as raw, \
         patch("agentic_fx.datafeed.price_provider.sources.mt5_bars") as vendor:
        tools_of(settings, provider)["get_ohlcv"].func("JP225", "4h")
    vendor.assert_not_called()
    assert raw.call_args.args[2] == "1h"
    conn.close()


def test_an_overlong_or_empty_raw_name_never_reaches_the_bridge(tmp_path):
    settings = settings_with(["JP225"])
    provider, conn = real_provider(tmp_path, settings)
    settings.datafeed.watch_symbols = ["A" * 33]  # 検証をすり抜けた値でも adapter が拒む
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_raw_bars_range") as raw, \
         patch("agentic_fx.datafeed.price_provider.sources.httpx.get") as get:
        with pytest.raises(Exception):
            provider._mt5_bars("A" * 33, "1h", 1)
    raw.assert_not_called()
    get.assert_not_called()
    conn.close()
