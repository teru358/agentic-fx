from types import SimpleNamespace

from agentic_fx.datafeed.planner import (
    InsufficientClosedBars, RequirementPlanner, calendar_days, count_closed,
)
from agentic_fx.datafeed.requirements import build_registry
from agentic_fx.plugin.loader import IndicatorRef


def _settings(intervals=("1m", "1h"), primary=("1h",)):
    return SimpleNamespace(
        pairs=["USDJPY", "EURUSD"],
        datafeed=SimpleNamespace(intervals=list(intervals),
                                  primary_intervals=list(primary)),
    )


def _strategy(name="cross", timeframe="1h", pairs=("USDJPY",), max_bars=200,
              indicators=()):
    return SimpleNamespace(name=name, kind="strategy", timeframe=timeframe,
                           pairs=pairs, max_bars=max_bars, indicators=indicators)


def test_registry_core_plugins_and_native_intervals_are_coalesced():
    indicator = SimpleNamespace(name="sma", kind="indicator", timeframe="5m",
                                pairs=(), max_bars=999, indicators=())
    cross = _strategy(
        max_bars=30,
        indicators=(IndicatorRef("fast", "sma", {}, None),),
    )
    registry = build_registry(_settings(intervals=("1m", "1h", "4h")),
                              [indicator, cross])

    assert registry.required_closed_bars("USDJPY", "1m") == 1
    assert registry.required_closed_bars("USDJPY", "1h") == 30
    assert registry.required_closed_bars("EURUSD", "1h") == 1
    assert registry.required_intervals == frozenset({"1m", "1h"})


def test_registry_converts_coarse_requirements_to_native_1h_bars():
    four_hour = _strategy(timeframe="4h", max_bars=30)
    daily = _strategy(name="daily", timeframe="1d", pairs=("EURUSD",), max_bars=7)
    registry = build_registry(_settings(intervals=("1m", "1h", "4h", "1d")),
                              [four_hour, daily])

    assert registry.required_closed_bars("USDJPY", "1h") == 120
    assert registry.required_closed_bars("EURUSD", "1h") == 168


def test_registry_uses_normalized_default_and_ignores_unrelated_intervals():
    plugin = _strategy(pairs=("USDJPY", "XAUUSD"), max_bars=200)
    a = build_registry(_settings(intervals=("1m", "1h")), [plugin])
    b = build_registry(_settings(intervals=("1m", "5m", "1h", "15m")), [plugin])
    assert a.requirements_for("USDJPY", "1h") == b.requirements_for("USDJPY", "1h")
    assert a.required_closed_bars("USDJPY", "1h") == 200
    assert a.required_closed_bars("XAUUSD", "1h") == 0


def test_registry_health_uses_derived_decision_timeframe():
    settings = _settings(intervals=("1m", "15m", "1h"), primary=("15m",))
    registry = build_registry(settings, [])

    assert registry.required_closed_bars("USDJPY", "15m") == 1
    assert registry.required_closed_bars("USDJPY", "1h") == 0


def test_planner_calculates_capability_and_transient_shortage():
    assert calendar_days(400, "1h", 0) == 27
    assert calendar_days(1600, "1h", 0) == 97
    planner = RequirementPlanner()
    ok = planner.plan("yfinance", "1h", 1600, consumer="strategy")
    assert ok.ok and ok.days == 97 and ok.insufficient is None
    denied = planner.plan("yfinance", "15m", 6000, consumer="strategy")
    assert not denied.ok and denied.days == 0
    assert denied.insufficient.classification == "structural"
    assert count_closed([1, 2], 2)
    short = planner.check_closed([1], 2, consumer="strategy", source="yfinance",
                                 interval="1h")
    assert isinstance(short, InsufficientClosedBars)
    assert short.classification == "transient"


def test_planner_uses_twelvedata_outputsize_and_measured_mt5_windows():
    planner = RequirementPlanner()
    assert planner.plan("twelvedata", "1h", 5000).ok
    assert not planner.plan("twelvedata", "1h", 5001).ok
    assert planner.plan("mt5", "15m", 5000).ok
    assert not planner.plan("mt5", "1h", 100_000).ok


def test_planner_uses_measured_mt5_30m_window_boundaries():
    planner = RequirementPlanner()
    assert planner.plan("mt5", "30m", 2000).ok
    assert not planner.plan("mt5", "30m", 100_000).ok
