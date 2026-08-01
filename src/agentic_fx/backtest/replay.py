"""ReplayClock and BarFeed for backtesting with UTC continuous 1m grid."""
from __future__ import annotations

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
            start: Timezone-aware UTC datetime. Raises ValueError if naive or non-UTC.
        """
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        if start.tzinfo != timezone.utc:
            raise ValueError("start must be UTC")
        self._current = start

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
            start: Start of range (inclusive), timezone-aware UTC.
                   Raises ValueError if naive or non-UTC.
            end: End of range (inclusive), timezone-aware UTC.
                 Raises ValueError if naive or non-UTC or > start.
        """
        # Validate start and end
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        if start.tzinfo != timezone.utc:
            raise ValueError("start must be UTC")
        if end.tzinfo is None:
            raise ValueError("end must be timezone-aware")
        if end.tzinfo != timezone.utc:
            raise ValueError("end must be UTC")
        if start > end:
            raise ValueError("start > end")

        self._symbol = symbol
        self._source = source
        self._bars: dict[str, Bar] = {}
        self._spreads: dict[str, float] = {}

        # Preload bars and spreads from database
        # Use custom SELECT to get spread column (load_bars doesn't return it)
        rows = conn.execute(
            "SELECT * FROM ohlcv WHERE symbol=? AND interval='1m' AND source=? "
            "AND bar_time>=? AND bar_time<=? ORDER BY bar_time",
            (symbol, source, start.isoformat(), end.isoformat())
        ).fetchall()

        for row in rows:
            # Normalize bar_time to UTC for consistent keying
            bar_time_dt = datetime.fromisoformat(row["bar_time"])
            normalized_key = bar_time_dt.astimezone(timezone.utc).isoformat()

            # Store Bar object
            bar = Bar(
                symbol=row["symbol"],
                interval=row["interval"],
                ts=bar_time_dt,
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
            ts: Timezone-aware UTC datetime.
                Raises ValueError if naive or non-UTC.

        Returns:
            Bar if exists, None if missing or out of bounds.
        """
        if ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        if ts.tzinfo != timezone.utc:
            raise ValueError("ts must be UTC")

        # Normalize lookup key to UTC
        normalized_key = ts.astimezone(timezone.utc).isoformat()
        return self._bars.get(normalized_key)

    def latest_1m(self, ts: datetime) -> Bar | None:
        """Alias for bar_at (returns bar at exact ts, not look-back)."""
        return self.bar_at(ts)

    def spread_at(self, ts: datetime) -> float | None:
        """Return spread at given timestamp, or None if missing.

        Args:
            ts: Timezone-aware UTC datetime.

        Returns:
            Spread value if exists, None if missing or out of bounds.
        """
        if ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        if ts.tzinfo != timezone.utc:
            raise ValueError("ts must be UTC")

        # Normalize lookup key to UTC
        normalized_key = ts.astimezone(timezone.utc).isoformat()
        return self._spreads.get(normalized_key)


def quote_from_bar(bar: Bar, spread: float) -> Quote:
    """Create a Quote from a Bar with bid/ask derived from spread.

    Args:
        bar: Bar to convert.
        spread: Bid-ask spread (ask - bid).

    Returns:
        Quote with bid = close - spread/2, ask = close + spread/2.
    """
    return Quote(
        symbol=bar.symbol,
        bid=bar.close - spread / 2,
        ask=bar.close + spread / 2,
        ts=bar.ts,
        source="backtest"
    )
