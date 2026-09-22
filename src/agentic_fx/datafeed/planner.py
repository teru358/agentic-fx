"""Capability-aware request sizing for closed OHLCV bars."""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil

from agentic_fx.datafeed.requirements import _INTERVAL_MINUTES


SOURCE_CAPABILITIES = {
    "yfinance": {"1m": 8, "2m": 60, "5m": 60, "15m": 60, "30m": 60,
                  "60m": 60, "90m": 60, "1h": 730},
    # Bridge measurements: 1m is about 90 days, 5m about 480 days, and
    # intermediate 15m/30m ranges about four years; 1h+ is ten years.
    "mt5-live": {"1m": 90, "5m": 480, "15m": 1460, "30m": 1460, "1h": 3650},
    "mt5": {"1m": 90, "5m": 480, "15m": 1460, "30m": 1460, "1h": 3650},
}

# Twelve Data accepts at most ``outputsize=5000`` bars per request, rather
# than yfinance's calendar-window limits.  Keep this separate from the day
# table so the structural decision uses the adapter's real limit.
SOURCE_MAX_BARS = {"twelvedata": 5000}


def calendar_days(required_closed_bars: int, interval: str,
                  holiday_headroom_days: int = 0) -> int:
    if required_closed_bars < 1:
        raise ValueError("required_closed_bars must be positive")
    try:
        minutes = _INTERVAL_MINUTES[interval]
    except KeyError as exc:
        raise ValueError(f"unknown interval: {interval!r}") from exc
    return ceil(required_closed_bars * minutes / 1440 * 7 / 5) + 3 + holiday_headroom_days


@dataclass(frozen=True, slots=True)
class InsufficientClosedBars:
    consumer: str
    source: str
    interval: str
    required: int
    available_max: int | None
    capability: int | None
    classification: str
    code: str = "insufficient_closed_bars"


@dataclass(frozen=True, slots=True)
class PlanResult:
    days: int
    ok: bool
    insufficient: InsufficientClosedBars | None = None

    def __iter__(self):
        yield self.days
        yield self.ok


def count_closed(bars, required: int) -> bool:
    return len(bars) >= required


class RequirementPlanner:
    def __init__(self, *, holiday_headroom_days: int = 0) -> None:
        self.holiday_headroom_days = holiday_headroom_days

    def capability_days(self, source: str, interval: str) -> int | None:
        if source == "twelvedata":
            return None
        table = SOURCE_CAPABILITIES.get(source, SOURCE_CAPABILITIES["yfinance"])
        return table.get(interval, None if _INTERVAL_MINUTES.get(interval, 0) >= 60 else 60)

    def plan(self, source: str, interval: str, required_closed_bars: int, *,
             consumer: str = "request") -> PlanResult:
        days = calendar_days(required_closed_bars, interval, self.holiday_headroom_days)
        max_bars = SOURCE_MAX_BARS.get(source)
        if max_bars is not None and required_closed_bars > max_bars:
            return PlanResult(0, False, InsufficientClosedBars(
                consumer, source, interval, required_closed_bars, max_bars,
                max_bars, "structural"))
        capability = self.capability_days(source, interval)
        if capability is not None and days > capability:
            available = int(capability * 1440 / _INTERVAL_MINUTES[interval] * 5 / 7)
            return PlanResult(0, False, InsufficientClosedBars(
                consumer, source, interval, required_closed_bars, available,
                capability, "structural"))
        return PlanResult(days, True)

    def check_closed(self, bars, required_closed_bars: int, *, consumer: str,
                     source: str, interval: str) -> InsufficientClosedBars | None:
        if count_closed(bars, required_closed_bars):
            return None
        return InsufficientClosedBars(consumer, source, interval,
                                     required_closed_bars, len(bars),
                                     self.capability_days(source, interval),
                                     "transient")


def startup_disposition(requirement, result: PlanResult) -> str:
    """Return the action for a capability failure before runtime fetching."""
    if result.ok:
        return "enabled"
    return "reject_startup" if requirement.hard else "disabled"
