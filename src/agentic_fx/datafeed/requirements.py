"""Declared closed-bar requirements, independent of fetching and scheduling."""
from __future__ import annotations

from dataclasses import dataclass

from agentic_fx.plugin.loader import DEFAULT_MAX_BARS


_INTERVAL_MINUTES = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
                     "60m": 60, "90m": 90, "1h": 60, "4h": 240,
                     "1d": 1440, "1wk": 10080}


@dataclass(frozen=True, slots=True)
class Requirement:
    consumer_id: str
    pairs: tuple[str, ...]
    interval: str
    required_closed_bars: int
    hard: bool
    reason: str


class RequirementRegistry:
    def __init__(self) -> None:
        self.entries: list[Requirement] = []

    def register(self, consumer_id: str, pairs, interval: str,
                 required_closed_bars: int, *, hard: bool, reason: str) -> None:
        if required_closed_bars < 1:
            raise ValueError("required_closed_bars must be positive")
        selected = tuple(dict.fromkeys(pairs))
        if _INTERVAL_MINUTES.get(interval, 0) >= 240:
            required_closed_bars *= _INTERVAL_MINUTES[interval] // 60
            interval = "1h"
        if selected:
            self.entries.append(Requirement(consumer_id, selected, interval,
                                            required_closed_bars, hard, reason))

    def requirements_for(self, pair: str, interval: str) -> tuple[Requirement, ...]:
        return tuple(entry for entry in self.entries
                     if pair in entry.pairs and entry.interval == interval)

    def required_closed_bars(self, pair: str, interval: str) -> int:
        return max((entry.required_closed_bars
                    for entry in self.requirements_for(pair, interval)), default=0)

    @property
    def required_intervals(self) -> frozenset[str]:
        intervals = {entry.interval for entry in self.entries}
        return frozenset("1h" if _INTERVAL_MINUTES.get(interval, 0) >= 240
                         else interval for interval in intervals)


def build_registry(settings, deployed_plugins) -> RequirementRegistry:
    registry = RequirementRegistry()
    pairs = tuple(settings.pairs)
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
            registry.register(ref.plugin, strategy_pairs, strategy.timeframe, bars,
                              hard=False, reason="indicator")
    return registry
