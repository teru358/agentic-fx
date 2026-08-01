"""Dukascopy tick data decoder — bi5 format parsing and tick extraction."""
import logging
import lzma
import struct
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

_log = logging.getLogger(__name__)

# Dukascopy-specific metadata table.
# Separate from _SPECS in price_provider since it covers watch symbols
# (XAUUSD, indices, etc.) that are not trading pairs.
_DUKASCOPY_POINTS = {
    "USDJPY": 1e-3,
    "EURUSD": 1e-5,
}


class Tick(NamedTuple):
    """Single tick: timestamp, bid, ask."""
    ts: datetime
    bid: float
    ask: float


def hour_url(symbol: str, dt_utc: datetime) -> str:
    """Dukascopy hourly tick data URL.

    Month is 0-based in Dukascopy URLs (1 January = "00").
    """
    year = dt_utc.year
    month = dt_utc.month - 1  # 0-based
    day = dt_utc.day
    hour = dt_utc.hour
    return (f"https://datafeed.dukascopy.com/datafeed/{symbol}/"
            f"{year}/{month:02d}/{day:02d}/{hour:02d}h_ticks.bi5")


def decode_bi5(payload: bytes, *, point: float, hour_start_utc: datetime) -> list[Tick]:
    """Decode LZMA-compressed bi5 tick data.

    Format: LZMA (FORMAT_ALONE or FORMAT_XZ) containing 20-byte records.
    Each record: struct '>3i2f' = (ms_offset, ask_points, bid_points, ask_vol, bid_vol)

    Rejects records where bid <= 0 or ask < bid.
    Returns list of Tick(ts, bid, ask).
    """
    # Decompress LZMA payload (try FORMAT_ALONE first, fallback to auto-detect)
    try:
        raw = lzma.decompress(payload, format=lzma.FORMAT_ALONE)
    except lzma.LZMAError:
        raw = lzma.decompress(payload)

    ticks = []
    rejected = 0
    incomplete = 0

    # Process 20-byte records
    for i in range(0, len(raw), 20):
        if len(raw) - i < 20:
            incomplete = len(raw) - i  # Bytes remaining (< 20)
            break  # Incomplete record at end

        ms_offset, ask_points, bid_points, ask_vol, bid_vol = struct.unpack(
            ">3i2f", raw[i:i+20]
        )

        # Convert points to prices
        bid = bid_points * point
        ask = ask_points * point

        # Reject invalid records: bid must be positive, ask >= bid
        if bid <= 0 or ask < bid:
            rejected += 1
            continue

        # Calculate timestamp
        ts = hour_start_utc + timedelta(milliseconds=ms_offset)

        ticks.append(Tick(ts, bid, ask))

    if rejected > 0 or incomplete > 0:
        msg = f"decode_bi5: rejected {rejected} invalid records"
        if incomplete > 0:
            msg += f", {incomplete} incomplete bytes at end"
        _log.debug(msg)

    return ticks


def point_of(symbol: str) -> float:
    """Get point size for symbol from Dukascopy metadata table.

    Raises KeyError if symbol not found (no string inference).
    """
    return _DUKASCOPY_POINTS[symbol]
