"""Dukascopy importer — fetch hourly tick data, aggregate to 1-minute bars, store."""
import logging
from datetime import datetime, timedelta, timezone

import httpx

from agentic_fx.backtest.dukascopy import Tick, decode_bi5, hour_url, point_of
from agentic_fx.store.ohlcv import ImportResult, import_bars

_log = logging.getLogger(__name__)


def ticks_to_1m(ticks: list[Tick], symbol: str) -> list[tuple]:
    """Aggregate ticks to 1-minute bars.

    Converts mid=(bid+ask)/2 prices to OHLC, calculates mean spread,
    and returns import_bars row tuples.

    Args:
        ticks: List of Tick(ts, bid, ask) objects
        symbol: Trading pair symbol

    Returns:
        List of tuples: (symbol, "1m", bar_time_iso, o, h, l, c, volume, spread)
        One row per minute, sorted by bar_time.
    """
    if not ticks:
        return []

    # Group ticks by minute (hour and minute components)
    minute_groups = {}
    for tick in ticks:
        # Key: (year, month, day, hour, minute) to group by minute
        key = (tick.ts.year, tick.ts.month, tick.ts.day, tick.ts.hour, tick.ts.minute)
        if key not in minute_groups:
            minute_groups[key] = []
        minute_groups[key].append(tick)

    # Sort keys to maintain chronological order
    sorted_keys = sorted(minute_groups.keys())
    rows = []

    for key in sorted_keys:
        ticks_in_minute = minute_groups[key]

        # Calculate mid price for each tick
        mids = [(tick.bid + tick.ask) / 2.0 for tick in ticks_in_minute]

        # OHLC from mid prices
        o = mids[0]  # open: first mid
        h = max(mids)  # high: max mid
        l = min(mids)  # low: min mid
        c = mids[-1]  # close: last mid

        # Volume: number of ticks
        v = len(ticks_in_minute)

        # Mean spread: average of (ask - bid) for all ticks
        spreads = [tick.ask - tick.bid for tick in ticks_in_minute]
        mean_spread = sum(spreads) / len(spreads)

        # Bar timestamp: the minute boundary (first tick's hour:minute)
        bar_ts = ticks_in_minute[0].ts.replace(second=0, microsecond=0)

        # Row tuple: (symbol, interval, bar_time_iso, o, h, l, c, volume, spread)
        row = (symbol, "1m", bar_ts.isoformat(), o, h, l, c, v, mean_spread)
        rows.append(row)

    return rows


def import_dukascopy(conn, symbol: str, start: datetime, end: datetime, *,
                     fetch=None, progress=None) -> ImportResult:
    """Import Dukascopy tick data and aggregate to 1-minute bars.

    Fetches hourly tick data files from Dukascopy, decodes them, aggregates
    to 1-minute OHLC bars, and imports into database. Empty hours (404 or
    empty payload) are skipped.

    Args:
        conn: sqlite3 connection
        symbol: Trading pair symbol
        start: Start time (inclusive) — aware UTC datetime
        end: End time (exclusive) — aware UTC datetime
        fetch: Optional fetch function (url: str) -> bytes. Defaults to httpx.get
               with 30s timeout. 404 errors return empty bytes, other errors raise.
        progress: Optional callback (url_or_time) -> None. Called for each hour processed.

    Returns:
        ImportResult with inserted, unchanged, conflicted counts.
    """
    if fetch is None:
        def fetch(url: str) -> bytes:
            try:
                resp = httpx.get(url, timeout=30)
                resp.raise_for_status()
                return resp.content
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return b""
                raise

    # Get point value for the symbol
    point = point_of(symbol)

    # Initialize ImportResult counters
    total_inserted = 0
    total_unchanged = 0
    total_conflicted = 0

    # Iterate through hours in [start, end)
    current = start
    while current < end:
        # Progress callback
        if progress is not None:
            progress(current)

        # Fetch hourly data
        url = hour_url(symbol, current)
        payload = fetch(url)

        # Skip empty payloads (404, no data)
        if not payload:
            current += timedelta(hours=1)
            continue

        # Decode bi5 payload
        try:
            ticks = decode_bi5(payload, point=point, hour_start_utc=current)
        except Exception as e:
            _log.error(f"Failed to decode bi5 for {symbol} at {current}: {e}")
            current += timedelta(hours=1)
            continue

        # Skip if no valid ticks
        if not ticks:
            current += timedelta(hours=1)
            continue

        # Aggregate to 1-minute bars
        rows = ticks_to_1m(ticks, symbol)

        # Import bars into database
        if rows:
            result = import_bars(conn, rows, source="dukascopy")
            total_inserted += result.inserted
            total_unchanged += result.unchanged
            total_conflicted += result.conflicted

        current += timedelta(hours=1)

    return ImportResult(total_inserted, total_unchanged, total_conflicted)
