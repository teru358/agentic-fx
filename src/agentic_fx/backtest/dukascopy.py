"""Dukascopy tick data decoder — bi5 format parsing and tick extraction."""
import logging
import lzma
import math
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

    Raises ValueError if dt_utc is naive or not UTC.
    """
    if dt_utc.tzinfo is None:
        raise ValueError(f"dt_utc must be timezone-aware; got naive datetime")
    if dt_utc.tzinfo != timezone.utc:
        raise ValueError(f"dt_utc must be UTC; got {dt_utc.tzinfo}")

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

    Rejects records where:
    - bid <= 0 or ask < bid
    - ms_offset not in [0, 3600000) range

    Raises ValueError if hour_start_utc is naive/non-UTC or point is not finite/positive.
    Returns list of Tick(ts, bid, ask).
    """
    # Validate time contract
    if hour_start_utc.tzinfo is None:
        raise ValueError(f"hour_start_utc must be timezone-aware; got naive datetime")
    if hour_start_utc.tzinfo != timezone.utc:
        raise ValueError(f"hour_start_utc must be UTC; got {hour_start_utc.tzinfo}")

    # Validate point contract
    if not math.isfinite(point):
        raise ValueError(f"point must be finite; got {point}")
    if point <= 0:
        raise ValueError(f"point must be positive; got {point}")

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

        # Reject records with out-of-range ms_offset
        if not (0 <= ms_offset < 3_600_000):
            rejected += 1
            continue

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
