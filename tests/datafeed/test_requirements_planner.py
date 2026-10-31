from types import SimpleNamespace

from agentic_fx.datafeed.planner import (
    InsufficientClosedBars, RequirementPlanner, calendar_days, count_closed,
)
from agentic_fx.datafeed.requirements import RequirementRegistry, build_registry
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


def _watch_settings(watch=("JP225",), primary=("1h",)):
    settings = _settings(intervals=("1m", "1h"), primary=primary)
    settings.datafeed.watch_symbols = list(watch)
    return settings


def _ref_strategy(indicators, *, timeframe="1h", max_bars=100, pairs=("USDJPY",)):
    return _strategy(name="ref", timeframe=timeframe, pairs=pairs,
                     max_bars=max_bars, indicators=tuple(indicators))


def test_registry_keys_are_only_the_ones_somebody_asked_for():
    registry = build_registry(_settings(intervals=("1m", "15m"), primary=("1h",)),
                              [_strategy(timeframe="15m", pairs=("USDJPY",))])
    assert ("EURUSD", "15m") not in registry.keys()
    assert ("USDJPY", "15m") in registry.keys()
    assert registry.keys() == tuple(sorted(registry.keys()))


def test_registry_hard_keys_for_a_pairs_only_config_equal_the_old_derivation():
    registry = build_registry(_settings(intervals=("1m", "1h", "15m"), primary=("1h",)),
                              [_strategy(timeframe="15m")])
    old = frozenset((pair, req.interval) for req in registry.entries if req.hard
                    for pair in req.pairs)
    assert registry.hard_keys() == old
    assert registry.hard_keys() == {(p, i) for p in ("USDJPY", "EURUSD")
                                    for i in ("1m", "1h")}
    assert not registry.is_hard("USDJPY", "15m")
    assert not registry.is_reference("USDJPY", "15m")


def test_literal_reference_is_a_soft_key_with_the_largest_window():
    refs = (IndicatorRef("jp", "ema", {}, None, symbol="JP225", timeframe="1h"),)
    registry = build_registry(_watch_settings(), [_ref_strategy(refs, max_bars=100),
                                                  _ref_strategy(refs, max_bars=300)])
    assert ("JP225", "1h") in registry.keys()
    assert registry.is_hard("JP225", "1h") is False
    assert registry.required_closed_bars("JP225", "1h") == 300
    assert registry.requirements_for("JP225", "1h")[0].consumer_id == "ref"


def test_hard_wins_when_a_reference_key_is_also_a_hard_requirement():
    settings = _watch_settings()
    settings.pairs = ["USDJPY", "JP225"]
    settings.datafeed.watch_symbols = []
    registry = build_registry(settings, [])
    assert registry.is_hard("JP225", "1h")
    soft = RequirementRegistry()
    soft.register("a", ["JP225"], "1h", 5, hard=False, reason="reference")
    soft.register("b", ["JP225"], "1h", 9, hard=True, reason="paper")
    assert soft.is_hard("JP225", "1h") and soft.required_closed_bars("JP225", "1h") == 9


def test_literal_trading_pair_reference_keeps_its_registry_key_hard():
    refs = (IndicatorRef("eur", "ema", {}, None, symbol="EURUSD", timeframe="1h"),)
    registry = build_registry(_watch_settings(watch=()), [_ref_strategy(refs)])

    assert ("EURUSD", "1h") in registry.keys()
    assert registry.is_hard("EURUSD", "1h") is True
    assert registry.is_reference("EURUSD", "1h") is False


def test_four_hour_and_daily_references_fold_into_native_hours():
    four = (IndicatorRef("jp", "ema", {}, None, symbol="JP225", timeframe="4h"),)
    daily = (IndicatorRef("jd", "ema", {}, None, symbol="JP225", timeframe="1d"),)
    r4 = build_registry(_watch_settings(), [_ref_strategy(four, max_bars=100)])
    rd = build_registry(_watch_settings(), [_ref_strategy(daily, max_bars=100)])
    nominal4 = -(-(99 * 3600) // 14400) + 1
    nominald = -(-(99 * 3600) // 86400) + 1
    assert r4.required_closed_bars("JP225", "1h") == nominal4 * 4
    assert rd.required_closed_bars("JP225", "1h") == nominald * 24
    assert ("JP225", "4h") not in r4.keys() and ("JP225", "1d") not in rd.keys()


def test_a_literal_outside_watch_symbols_is_not_a_key():
    refs = (IndicatorRef("us", "ema", {}, None, symbol="US500", timeframe="1h"),)
    registry = build_registry(_watch_settings(), [_ref_strategy(refs)])
    assert all(symbol != "US500" for symbol, _ in registry.keys())


def test_self_with_another_timeframe_adds_the_trading_pair_native_key():
    refs = (IndicatorRef("rsi4", "rsi", {}, None, timeframe="4h"),)
    registry = build_registry(_watch_settings(watch=()),
                              [_ref_strategy(refs, max_bars=100, pairs=("EURUSD",))])
    assert registry.required_closed_bars("EURUSD", "1h") == (-(-(99 * 3600) // 14400) + 1) * 4
    assert registry.required_closed_bars("USDJPY", "1h") == 1
    assert registry.max_age_seconds("EURUSD", "1h") == 3 * 14400


def test_max_age_is_the_tightest_among_indicators_reading_the_key():
    refs = (IndicatorRef("a", "ema", {}, None, symbol="JP225", timeframe="1h", max_age="2h"),
            IndicatorRef("b", "ema", {}, None, symbol="JP225", timeframe="1h", max_age="5h"))
    registry = build_registry(_watch_settings(), [_ref_strategy(refs)])
    assert registry.max_age_seconds("JP225", "1h") == 7200
    assert registry.max_age_seconds("USDJPY", "1h") is None


def test_trading_pair_reference_on_a_non_primary_interval_is_hard_for_registry_and_ingest():
    from agentic_fx.datafeed.ingest import Ingest

    settings = _watch_settings(watch=(), primary=("5m",))
    settings.datafeed.primary = "mt5"
    refs = (IndicatorRef("eur", "ema", {}, None, symbol="EURUSD", timeframe="1h"),)
    plugins = [_ref_strategy(refs)]
    registry = build_registry(settings, plugins)

    assert ("EURUSD", "1h") in registry.hard_keys()
    assert registry.is_reference("EURUSD", "1h") is False
    ingest = Ingest(settings, plugins)
    assert ingest._is_hard(("EURUSD", "1h")) is True
    assert ingest._is_hard(("EURUSD", "1h")) == (("EURUSD", "1h") in registry.hard_keys())


def test_readable_symbols_are_computed_once_per_registry_build(monkeypatch):
    from agentic_fx.datafeed import requirements

    calls = []
    real = requirements.readable_symbols
    monkeypatch.setattr(requirements, "readable_symbols",
                        lambda settings: calls.append(1) or real(settings))
    refs = (IndicatorRef("a", "ema", {}, None, symbol="JP225", timeframe="1h"),
            IndicatorRef("b", "ema", {}, None, symbol="EURUSD", timeframe="1h"))
    build_registry(_watch_settings(), [_ref_strategy(refs), _ref_strategy(refs)])
    assert len(calls) == 1
