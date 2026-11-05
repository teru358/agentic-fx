"""Read-only normalization of retained 1m bars for economic observations."""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from agentic_fx.core.contracts import Bar
from agentic_fx.store import ohlcv, ohlcv_slices

_MARKER_SOURCE = "econ-normalizer"
_TOL = 1e-9


@dataclass(frozen=True, slots=True)
class NormalizedBars:
    rows: tuple[ohlcv_slices.SliceRow, ...]
    expected_rows: int
    has_markers: bool
    conflicting_minutes: tuple[datetime, ...] = ()
    invalid_minutes: tuple[datetime, ...] = ()
    missing_minutes: tuple[datetime, ...] = ()
    unrecoverable: bool = False
    reason: str | None = None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("range datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def _valid(bar: Bar) -> bool:
    vals = (bar.open, bar.high, bar.low, bar.close)
    return (all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0
                for x in vals)
            and isinstance(bar.volume, (int, float))
            and math.isfinite(bar.volume) and 0 <= bar.volume < 1e308
            and bar.low <= bar.open <= bar.high
            and bar.low <= bar.close <= bar.high)


def _same(left: Bar, right: Bar) -> bool:
    return all(abs(a - b) <= _TOL for a, b in zip(
        (left.open, left.high, left.low, left.close),
        (right.open, right.high, right.low, right.close)))


def _oldest(conn: sqlite3.Connection, pair: str) -> datetime | None:
    stamps = []
    for table, sources in (("ohlcv_cache", ohlcv.LIVE_SOURCES),
                           ("ohlcv_history", ohlcv.IMPORT_SOURCES)):
        for source in sources:
            row = conn.execute(
                f"SELECT MIN(bar_time) FROM {table} "
                "WHERE symbol=? AND interval='1m' AND source=?",
                (pair, source)).fetchone()
            if row and row[0]:
                stamps.append(datetime.fromisoformat(row[0]).astimezone(timezone.utc))
    return min(stamps) if stamps else None


def normalize_1m(conn: sqlite3.Connection, pair: str, start: datetime,
                 end: datetime) -> NormalizedBars:
    """Return one observed bar or outage marker per open minute in [start,end)."""
    start, end = _utc(start), _utc(end)
    minutes = ohlcv_slices.open_minutes(start, end)
    expected = len(minutes)
    if start >= end:
        raise ValueError("range_start must be before range_end")
    oldest = _oldest(conn, pair)
    if oldest is None or start < oldest:
        return NormalizedBars((), expected, False, unrecoverable=True,
                              reason="range starts before retained 1m history")

    by_minute: dict[datetime, list[tuple[int, str, Bar]]] = {}
    invalid: set[datetime] = set()
    for priority, (sources, loader) in enumerate((
            (sorted(ohlcv.LIVE_SOURCES), ohlcv.load_cache_bars),
            (sorted(ohlcv.IMPORT_SOURCES), ohlcv.load_history_bars))):
        for source in sources:
            bars = loader(conn, pair, "1m", source=source, since=start, until=end)
            for bar in bars:
                at = _utc(bar.ts)
                if at not in minutes:
                    continue
                if not _valid(bar):
                    invalid.add(at)
                    continue
                by_minute.setdefault(at, []).append((priority, source, bar))

    rows = []
    conflicts = []
    invalid_minutes = []
    missing_minutes = []
    for minute in minutes:
        values = by_minute.get(minute, [])
        conflict = (len(values) > 1 and any(
            not _same(values[0][2], value[2]) for value in values[1:]))
        mismatch = (minute in invalid or conflict)
        if not values or mismatch:
            rows.append(ohlcv_slices.SliceRow(
                minute, None, None, None, None, None, _MARKER_SOURCE,
                ohlcv_slices.QUALITY_OUTAGE))
            if conflict:
                conflicts.append(minute)
            elif minute in invalid:
                invalid_minutes.append(minute)
            elif not values:
                missing_minutes.append(minute)
            continue
        _, source, bar = min(values, key=lambda item: (item[0], item[1]))
        rows.append(ohlcv_slices.SliceRow(
            minute, bar.open, bar.high, bar.low, bar.close, bar.volume,
            source, ohlcv_slices.QUALITY_OBSERVED))
    return NormalizedBars(tuple(rows), expected,
                          any(r.quality == ohlcv_slices.QUALITY_OUTAGE for r in rows),
                          tuple(conflicts), tuple(invalid_minutes),
                          tuple(missing_minutes))
