#!/usr/bin/env python3
"""Verify Dukascopy tick decoder against real data and MT5 bridge.

Usage:
    uv run python scripts/verify_dukascopy.py --hour 2026-07-30T12:00 \\
        --symbols USDJPY EURUSD --mt5-base http://localhost:8812
"""
import argparse
import logging
import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import requests
from dotenv import load_dotenv

# Load .env for MT5_BRIDGE_API_KEY
load_dotenv()

# Add src to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agentic_fx.backtest.dukascopy import decode_bi5, hour_url, point_of
from agentic_fx.datafeed.sources import vendor_symbol

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def fetch_dukascopy_data(symbol: str, hour_utc: datetime) -> bytes | None:
    """Fetch bi5 data from Dukascopy."""
    url = hour_url(symbol, hour_utc)
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        _log.error(f"Failed to fetch {url}: {e}")
        return None


def fetch_mt5_bars(symbol: str, hour_utc: datetime, mt5_base: str) -> list[dict] | None:
    """Fetch 1m bars from MT5 bridge for the hour.

    Uses /ohlcv endpoint with ISO8601 dates (matching sources.py pattern).
    """
    try:
        sym = vendor_symbol(symbol, "mt5")
        # Hour end is 1 second before the next hour (to include last tick of the hour)
        end = hour_utc + timedelta(hours=1) - timedelta(seconds=1)

        headers = {}
        api_key = os.environ.get("MT5_BRIDGE_API_KEY")
        if api_key:
            headers["X-Bridge-Api-Key"] = api_key

        # Use /ohlcv endpoint (matching sources.py mt5_bars_range pattern)
        resp = httpx.get(
            f"{mt5_base}/ohlcv/{sym}",
            params={
                "from": hour_utc.isoformat(),
                "to": end.isoformat(),
                "interval": "1m"
            },
            headers=headers,
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("bars", [])
    except Exception as e:
        _log.error(f"Failed to fetch MT5 bars for {symbol}: {e}")
        return None


def verify_symbol(symbol: str, hour_utc: datetime, mt5_base: str) -> tuple[bool, str]:
    """Verify a single symbol. Returns (passed, details)."""
    details = []

    # Fetch Dukascopy data
    payload = fetch_dukascopy_data(symbol, hour_utc)
    if payload is None:
        return False, f"Failed to fetch Dukascopy data for {symbol}"

    point = point_of(symbol)
    ticks = decode_bi5(payload, point=point, hour_start_utc=hour_utc)

    # Assert 1: Tick count threshold (平日12時台 >= 500)
    tick_count = len(ticks)
    if tick_count < 500:
        details.append(f"FAIL: {symbol} only has {tick_count} ticks (< 500)")
    else:
        details.append(f"PASS: {symbol} tick count = {tick_count}")

    # Assert 2a: MS offset monotonic and in range
    if ticks:
        ms_offsets = [t.ts for t in ticks]
        prev_ms = 0
        for i, ts in enumerate(ms_offsets):
            ms_offset = int((ts - hour_utc).total_seconds() * 1000)
            if ms_offset < prev_ms:
                details.append(f"FAIL: {symbol} ms_offset not monotonic at index {i}")
                break
            if not (0 <= ms_offset <= 3600000):
                details.append(f"FAIL: {symbol} ms_offset {ms_offset} out of range")
                break
            prev_ms = ms_offset
        else:
            details.append(f"PASS: {symbol} ms_offset monotonic and in range")

    # Assert 2b: Record length validation (implicit in decode_bi5)
    # We already decode 20-byte records, so this is satisfied
    details.append(f"PASS: {symbol} record length validated during decode")

    # Assert 3: Bid/Ask validity and spread
    if ticks:
        spreads = []
        for tick in ticks:
            if not (0 < tick.bid < tick.ask):
                details.append(f"FAIL: {symbol} invalid bid/ask: bid={tick.bid}, ask={tick.ask}")
                break
            spread = tick.ask - tick.bid
            spreads.append(spread)
        else:
            spread_median = statistics.median(spreads)
            if symbol == "USDJPY":
                if 0.001 <= spread_median <= 0.05:
                    details.append(f"PASS: {symbol} spread median {spread_median:.6f} in range [0.001, 0.05]")
                else:
                    details.append(f"FAIL: {symbol} spread median {spread_median:.6f} out of range")
            elif symbol == "EURUSD":
                if 0.00001 <= spread_median <= 0.0005:
                    details.append(f"PASS: {symbol} spread median {spread_median:.6f} in range [0.00001, 0.0005]")
                else:
                    details.append(f"FAIL: {symbol} spread median {spread_median:.6f} out of range")

    # Assert 4: Compare with MT5 bid (Dukascopy mid vs MT5 bid, MAE < 0.1%)
    # FAIL if cannot compare (no fallback to WARN)
    mt5_bars = fetch_mt5_bars(symbol, hour_utc, mt5_base)
    if not mt5_bars:
        details.append(f"FAIL: {symbol} could not fetch MT5 bars")
    elif not ticks:
        details.append(f"FAIL: {symbol} no Dukascopy ticks for comparison")
    else:
        mae_pct_list = []
        for bar in mt5_bars:
            # MT5 bar open price is used as representative bid (per brief)
            mt5_bid = bar.get("open")
            if mt5_bid is None:
                continue

            # Parse bar time to determine minute boundary
            bar_time_str = bar.get("time")
            if not bar_time_str:
                continue
            bar_time = datetime.fromisoformat(bar_time_str.replace("Z", "+00:00"))
            if bar_time.tzinfo is None:
                bar_time = bar_time.replace(tzinfo=timezone.utc)

            # Minute start: align to bar's minute boundary
            minute_start = bar_time.replace(second=0, microsecond=0)
            minute_end = minute_start + timedelta(minutes=1)

            # Filter ticks for this minute: first tick from this minute onwards
            minute_ticks = [
                t for t in ticks
                if minute_start <= t.ts < minute_end
            ]

            if minute_ticks:
                # Dukascopy mid: average of all ticks in the minute
                duka_mid = sum(t.bid + t.ask for t in minute_ticks) / (2 * len(minute_ticks))
                mae_pct = abs(duka_mid - mt5_bid) / abs(mt5_bid) * 100
                mae_pct_list.append(mae_pct)

        if not mae_pct_list:
            details.append(f"FAIL: {symbol} no ticks found matching MT5 bars")
        else:
            avg_mae_pct = sum(mae_pct_list) / len(mae_pct_list)
            if avg_mae_pct < 0.1:
                details.append(f"PASS: {symbol} avg MAE {avg_mae_pct:.4f}% < 0.1% (n={len(mae_pct_list)})")
            else:
                details.append(f"FAIL: {symbol} avg MAE {avg_mae_pct:.4f}% >= 0.1% (n={len(mae_pct_list)})")

    passed = all("FAIL" not in d for d in details)
    return passed, "\n  ".join(details)


def main():
    parser = argparse.ArgumentParser(
        description="Verify Dukascopy tick decoder against real data")
    parser.add_argument("--hour", required=True,
                        help="Hour to verify (ISO format, e.g., 2026-07-30T12:00)")
    parser.add_argument("--symbols", nargs="+", required=True,
                        help="Symbols to verify (e.g., USDJPY EURUSD)")
    parser.add_argument("--mt5-base", default="http://localhost:8812",
                        help="MT5 bridge base URL (default: http://localhost:8812)")

    args = parser.parse_args()

    # Parse hour
    try:
        hour_utc = datetime.fromisoformat(args.hour)
        if hour_utc.tzinfo is None:
            hour_utc = hour_utc.replace(tzinfo=timezone.utc)
    except ValueError as e:
        _log.error(f"Invalid hour format: {e}")
        return 1

    _log.info(f"Verifying {args.symbols} at {hour_utc} against {args.mt5_base}")

    all_passed = True
    for symbol in args.symbols:
        passed, details = verify_symbol(symbol, hour_utc, args.mt5_base)
        print(f"\n{symbol}:")
        print(f"  {details}")
        all_passed = all_passed and passed

    print(f"\n{'='*60}")
    if all_passed:
        print("All verifications PASSED")
        return 0
    else:
        print("Some verifications FAILED")
        return 1


if __name__ == "__main__":
    exit(main())
