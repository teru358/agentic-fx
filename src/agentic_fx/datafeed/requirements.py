"""Declared closed-bar requirements, independent of fetching and scheduling."""
from __future__ import annotations

from dataclasses import dataclass

from agentic_fx.plugin.loader import DEFAULT_MAX_BARS
from agentic_fx.plugin.reference_time import (
    DEFAULT_MAX_AGE_FACTOR, PLUGIN_TIMEFRAMES, canonical_duration_seconds,
    nominal_reference_bars, timeframe_seconds,
)
from agentic_fx.symbols import readable_symbols


REFERENCE_REASONS = frozenset({"reference", "reference_pair"})

_INTERVAL_MINUTES = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
                     "60m": 60, "90m": 90, "1h": 60, "4h": 240,
                     "1d": 1440, "1wk": 10080}


@dataclass(frozen=True, slots=True)
class Requirement:
    consumer_id: str
    symbols: tuple[str, ...]
    interval: str
    required_closed_bars: int
    hard: bool
    reason: str
    max_age_seconds: int | None = None

    @property
    def pairs(self) -> tuple[str, ...]:
        return self.symbols


def native_interval(interval: str) -> str:
    """Intervals of 4h and above are derived from native 1h rows."""
    return "1h" if _INTERVAL_MINUTES.get(interval, 0) >= 240 else interval


class RequirementRegistry:
    """Requirements keyed by ``(effective_symbol, native_interval)``.

    The same key may be requested by several consumers; it is hard when any
    request is hard, and its window is the largest requested one.
    """

    def __init__(self) -> None:
        self.entries: list[Requirement] = []

    def register(self, consumer_id: str, symbols, interval: str,
                 required_closed_bars: int, *, hard: bool, reason: str,
                 max_age_seconds: int | None = None) -> None:
        if required_closed_bars < 1:
            raise ValueError("required_closed_bars must be positive")
        selected = tuple(dict.fromkeys(symbols))
        if _INTERVAL_MINUTES.get(interval, 0) >= 240:
            required_closed_bars *= _INTERVAL_MINUTES[interval] // 60
            interval = "1h"
        if selected:
            self.entries.append(Requirement(consumer_id, selected, interval,
                                            required_closed_bars, hard, reason,
                                            max_age_seconds))

    def requirements_for(self, symbol: str, interval: str) -> tuple[Requirement, ...]:
        return tuple(entry for entry in self.entries
                     if symbol in entry.symbols and entry.interval == interval)

    def required_closed_bars(self, symbol: str, interval: str) -> int:
        return max((entry.required_closed_bars
                    for entry in self.requirements_for(symbol, interval)), default=0)

    def keys(self) -> tuple[tuple[str, str], ...]:
        """Only keys somebody asked for, in a stable order."""
        return tuple(sorted({(symbol, entry.interval)
                             for entry in self.entries for symbol in entry.symbols}))

    def is_hard(self, symbol: str, interval: str) -> bool:
        return any(entry.hard for entry in self.requirements_for(symbol, interval))

    def is_reference(self, symbol: str, interval: str) -> bool:
        """True when only literal reference symbols ask for this key; such keys are
        fetched with the leftover budget and retried with a backoff instead of
        every tick."""
        entries = self.requirements_for(symbol, interval)
        return bool(entries) and all(entry.reason == "reference" for entry in entries)

    def is_referenced(self, symbol: str, interval: str) -> bool:
        """True when some strategy declared this key as a literal reference."""
        return any(entry.reason in REFERENCE_REASONS
                   for entry in self.requirements_for(symbol, interval))

    def hard_keys(self) -> frozenset[tuple[str, str]]:
        return frozenset((symbol, entry.interval)
                         for entry in self.entries if entry.hard
                         for symbol in entry.symbols)

    def max_age_seconds(self, symbol: str, interval: str) -> int | None:
        """Tightest freshness bound among the indicators reading this key."""
        ages = [entry.max_age_seconds
                for entry in self.requirements_for(symbol, interval)
                if entry.max_age_seconds is not None]
        return min(ages) if ages else None

    @property
    def required_intervals(self) -> frozenset[str]:
        return frozenset(native_interval(entry.interval) for entry in self.entries)


def build_registry(settings, deployed_plugins) -> RequirementRegistry:
    registry = RequirementRegistry()
    pairs = tuple(settings.pairs)
    readable: frozenset[str] | None = None
    registry.register("paper", pairs, "1m", 1, hard=True, reason="paper")
    registry.register("mtm", pairs, "1m", 1, hard=True, reason="mtm")
    registry.register("hwm", pairs, "1m", 1, hard=True, reason="hwm")
    for interval in settings.datafeed.primary_intervals:
        registry.register("health", pairs, interval, 1, hard=True, reason="freshness")

    strategies = [meta for meta in deployed_plugins
                  if getattr(meta, "kind", None) == "strategy"]
    configured_pairs = set(pairs)
    for strategy in strategies:
        strategy_pairs = tuple(pair for pair in strategy.pairs if pair in configured_pairs)
        bars = getattr(strategy, "max_bars", DEFAULT_MAX_BARS)
        registry.register(strategy.name, strategy_pairs, strategy.timeframe, bars,
                          hard=False, reason="strategy")
        # Indicator requirements are deliberately charged to their consuming
        # strategy's timeframe; standalone indicator metadata has no runtime
        # evaluation window.
        for ref in getattr(strategy, "indicators", ()):
            symbol = getattr(ref, "symbol", "self")
            reference_tf = getattr(ref, "timeframe", None) or strategy.timeframe
            if symbol == "self" and reference_tf == strategy.timeframe:
                registry.register(ref.plugin, strategy_pairs, strategy.timeframe, bars,
                                  hard=False, reason="indicator")
                continue
            if reference_tf not in PLUGIN_TIMEFRAMES or not strategy_pairs:
                continue
            if symbol == "self":
                targets = strategy_pairs
            else:
                if readable is None:
                    readable = frozenset(readable_symbols(settings))
                if symbol not in readable:
                    continue
                targets = (symbol,)
            try:
                max_age = (canonical_duration_seconds(ref.max_age)
                           if getattr(ref, "max_age", None) is not None
                           else DEFAULT_MAX_AGE_FACTOR * timeframe_seconds(reference_tf))
            except ValueError:
                continue
            trading_pair = symbol in configured_pairs
            registry.register(strategy.name, targets, reference_tf,
                              nominal_reference_bars(
                                  getattr(strategy, "max_bars", DEFAULT_MAX_BARS),
                                  strategy.timeframe, reference_tf),
                              hard=trading_pair,
                              reason="reference_pair" if trading_pair else "reference",
                              max_age_seconds=max_age)
    return registry
