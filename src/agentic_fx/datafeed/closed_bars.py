"""ライブ足の確定時刻を一箇所で判定する。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.sources import INTERVAL_MIN


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def normalize_closed_range(raw_bars: list[Bar], *, interval: str,
                           start: datetime, end: datetime, cutoff: datetime,
                           grace: timedelta) -> list[Bar]:
    """指定窓内にあり、評価時点までに閉じた足だけを時刻順で返す。"""
    if interval not in INTERVAL_MIN:
        raise ValueError(f"unknown interval: {interval}")
    start_utc, end_utc, cutoff_utc = (
        _utc(start, "start"), _utc(end, "end"), _utc(cutoff, "cutoff"))
    if start_utc > end_utc:
        raise ValueError("start must be <= end")
    if grace < timedelta(0):
        raise ValueError("grace must not be negative")
    width = timedelta(minutes=INTERVAL_MIN[interval])
    result: dict[datetime, Bar] = {}
    for bar in raw_bars:
        ts = _utc(bar.ts, "bar.ts")
        if start_utc <= ts < end_utc and ts + width + grace <= cutoff_utc:
            result.setdefault(ts, bar)
    return [result[ts] for ts in sorted(result)]
