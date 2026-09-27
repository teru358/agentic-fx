from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar
from agentic_fx.tools import market_tools


def test_get_ohlcv_returns_structured_shortage_below_100_closed_bars():
    settings = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    provider = MagicMock()
    provider.get_bars.return_value = [
        Bar("USDJPY", "1h", start + timedelta(hours=i), 1, 1, 1, 1, 1)
        for i in range(99)]
    tool = next(t for t in market_tools.build(provider, MagicMock(), settings)
                if t.name == "get_ohlcv")
    assert tool.func("USDJPY", "1h") == {"insufficient_closed_bars": {
        "consumer": "get_ohlcv", "source": settings.datafeed.primary,
        "interval": "1h", "role": "decision",
        "required": 100, "available": 99,
        "capability": "temporary"}}


def test_readonly_tool_uses_primary_without_calling_enabled_mt5(tmp_path):
    settings = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    settings.datafeed.primary = "yfinance"
    settings.datafeed.mt5.enabled = True
    settings.datafeed.yfinance.enabled = True
    from agentic_fx.datafeed.price_provider import PriceProvider
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.store.db import connect, init_db
    conn = connect(tmp_path / "readonly-primary.db")
    init_db(conn)
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    provider = PriceProvider(conn, settings, FixedClock(now),
                             readonly=True)
    bars = [Bar("USDJPY", "1h", now - timedelta(hours=100 - i),
                1, 1, 1, 1, 1) for i in range(100)]
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_bars") as mt5, \
         patch("agentic_fx.datafeed.price_provider.sources.yf_bars", return_value=bars):
        tool = next(t for t in market_tools.build(provider, MagicMock(), settings)
                    if t.name == "get_ohlcv")
        tool.func("USDJPY", "1h")
    mt5.assert_not_called()
    conn.close()


def test_get_indicators_plans_for_the_largest_plugin_window():
    settings = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    provider = MagicMock()
    provider.get_bars.return_value = []
    plugin = MagicMock(kind="indicator", max_bars=200, params={}, name="wide")
    tool = next(t for t in market_tools.build(provider, MagicMock(), settings,
                                               indicator_plugins=[plugin],
                                               sandbox_run=lambda *args, **kwargs: {})
                if t.name == "get_indicators")
    tool.func("USDJPY", "1h")
    provider.get_bars.assert_called_once_with("USDJPY", "1h", lookback_days=15)
