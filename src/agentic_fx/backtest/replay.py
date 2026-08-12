"""ReplayClock and BarFeed for backtesting with UTC continuous 1m grid."""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone

from agentic_fx.core.contracts import Bar, Quote


class ReplayClock:
    """Clock that advances by 1 minute per call, simulating replay time.

    Implements the Clock protocol (has `now()` method).
    Requires start to be timezone-aware UTC.
    """

    def __init__(self, start: datetime):
        """Initialize with start time.

        Args:
            start: Timezone-aware datetime (any offset), on 1-minute grid boundary.
                   Normalized to UTC. Raises ValueError if naive or off-grid (second != 0).
                   Off-grid start makes all bar_at lookups fail (fail-open trap).
        """
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        # Normalize any aware datetime to UTC
        start_utc = start.astimezone(timezone.utc)
        if start_utc.second != 0 or start_utc.microsecond != 0:
            raise ValueError("start must be on minute boundary (second and microsecond must be 0)")
        self._current = start_utc

    def now(self) -> datetime:
        """Return current replay time."""
        return self._current

    def advance(self) -> datetime:
        """Advance 1 minute and return new time."""
        self._current = self._current + timedelta(minutes=1)
        return self._current


class BarFeed:
    """Preloaded 1-minute bar feed with spread information.

    Preloads all bars for a symbol/source in [start, end] range into memory.
    Provides O(1) lookup by timestamp. Gaps return None (no look-back).
    """

    def __init__(self, conn: sqlite3.Connection, symbol: str, *,
                 source: str, start: datetime, end: datetime):
        """Initialize bar feed.

        Args:
            conn: Database connection.
            symbol: Instrument symbol (e.g., "USDJPY").
            source: Data source identifier.
            start: Start of range (inclusive), timezone-aware (any offset).
                   Normalized to UTC. Raises ValueError if naive.
            end: End of range (inclusive), timezone-aware (any offset).
                 Normalized to UTC. Raises ValueError if naive or > start.
        """
        # Validate start and end, normalize to UTC
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        if end.tzinfo is None:
            raise ValueError("end must be timezone-aware")

        # Normalize to UTC
        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)

        if start_utc > end_utc:
            raise ValueError("start > end")

        self._symbol = symbol
        self._source = source
        self._bars: dict[str, Bar] = {}
        self._spreads: dict[str, float] = {}

        # Preload bars and spreads from database
        # Use custom SELECT to get spread column (load_history_bars doesn't return it)
        rows = conn.execute(
            "SELECT * FROM ohlcv_history WHERE symbol=? AND interval='1m' AND source=? "
            "AND bar_time>=? AND bar_time<=? ORDER BY bar_time",
            (symbol, source, start_utc.isoformat(), end_utc.isoformat())
        ).fetchall()

        for row in rows:
            # Normalize bar_time to UTC for consistent keying and storage
            bar_time_dt = datetime.fromisoformat(row["bar_time"])
            bar_time_utc = bar_time_dt.astimezone(timezone.utc)
            normalized_key = bar_time_utc.isoformat()

            # Store Bar object with UTC-normalized ts
            bar = Bar(
                symbol=row["symbol"],
                interval=row["interval"],
                ts=bar_time_utc,
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"]
            )
            self._bars[normalized_key] = bar

            # Store spread if available
            if row["spread"] is not None:
                self._spreads[normalized_key] = row["spread"]

    def bar_at(self, ts: datetime) -> Bar | None:
        """Return bar at given timestamp, or None if gap (no look-back).

        Args:
            ts: Timezone-aware datetime (any offset).
                Normalized to UTC for lookup. Raises ValueError if naive.

        Returns:
            Bar if exists, None if missing or out of bounds.
        """
        if ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware")

        # Normalize lookup key to UTC
        ts_utc = ts.astimezone(timezone.utc)
        normalized_key = ts_utc.isoformat()
        return self._bars.get(normalized_key)

    def latest_completed_1m(self, ts: datetime) -> Bar | None:
        """Return the most recently completed 1-minute bar before ts.

        bar_time represents the start of the bar interval. bar_at(now) returns
        the [now, now+1m) bar which is still forming and includes future values
        (high, low, close not yet observed). Only completed bars are suitable
        for decision-making (§6: no look-ahead).

        Args:
            ts: Timezone-aware datetime (any offset). Raises ValueError if naive.

        Returns bar at ts - 1 minute (UTC), or None if no such bar exists.
        """
        if ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        return self.bar_at(ts - timedelta(minutes=1))

    def spread_at(self, ts: datetime) -> float | None:
        """Return spread at given timestamp, or None if missing.

        Args:
            ts: Timezone-aware datetime (any offset).
                Normalized to UTC for lookup. Raises ValueError if naive.

        Returns:
            Spread value if exists, None if missing or out of bounds.
        """
        if ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware")

        # Normalize lookup key to UTC
        ts_utc = ts.astimezone(timezone.utc)
        normalized_key = ts_utc.isoformat()
        return self._spreads.get(normalized_key)


def quote_from_bar(bar: Bar, spread: float) -> Quote:
    """Create a Quote from a Bar with bid/ask derived from spread.

    Args:
        bar: Bar to convert.
        spread: Bid-ask spread (ask - bid). Must be a finite non-negative number.
                Raises ValueError if None, NaN, infinite, or negative.

    Returns:
        Quote with bid = close - spread/2, ask = close + spread/2.
    """
    if spread is None:
        raise ValueError("spread must not be None")
    if not math.isfinite(spread):
        raise ValueError("spread must be finite")
    if spread < 0:
        raise ValueError("spread must not be negative")
    return Quote(
        symbol=bar.symbol,
        bid=bar.close - spread / 2,
        ask=bar.close + spread / 2,
        ts=bar.ts,
        source="backtest"
    )
