from __future__ import annotations

import argparse
import json
import statistics
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_fx.datafeed.cache_window import live_window_days
from agentic_fx.datafeed.health import validate_bars
from agentic_fx.datafeed.sources import INTERVAL_MIN
from agentic_fx.store import ohlcv
from agentic_fx.store.db import connect


def _measure(bars, now: datetime, freshness_max_min: int,
             interval_min: int, repeats: int) -> dict:
    elapsed = []
    peaks = []
    outcomes = []
    errors = []
    for _ in range(repeats):
        tracemalloc.start()
        started = time.perf_counter()
        try:
            validate_bars(bars, now, freshness_max_min, interval_min)
            outcomes.append("PASS")
            errors.append(None)
        except Exception as exc:  # 計測対象の合否を記録して次の反復へ進む
            outcomes.append("FAIL")
            errors.append(f"{type(exc).__name__}: {exc}")
        elapsed.append(time.perf_counter() - started)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peaks.append(peak)
    return {
        "rows": len(bars),
        "outcome": outcomes[0],
        "error": errors[0],
        "median_seconds": statistics.median(elapsed),
        "peak_bytes_max": max(peaks),
        "repeats": repeats,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--history-source", required=True)
    parser.add_argument("--window-source", required=True,
                        choices=("yfinance", "twelvedata", "mt5"))
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--lookback-days", type=int, default=5)
    parser.add_argument("--freshness-max-min", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    conn = connect(args.db)
    try:
        all_bars = ohlcv.load_history_bars(
            conn, args.symbol, args.interval, source=args.history_source)
    finally:
        conn.close()
    if len(all_bars) < 2:
        raise SystemExit("実測不能: ohlcv_history の実バーが 2 行未満")

    weekend_gaps = sum(
        1 for left, right in zip(all_bars, all_bars[1:])
        if right.ts - left.ts >= timedelta(hours=48))
    if weekend_gaps == 0:
        raise SystemExit("実測不能: 48時間以上の週末ギャップを含まない")

    now = all_bars[-1].ts + timedelta(minutes=INTERVAL_MIN[args.interval])
    # history の producer 名 (dukascopy/mt5) と、本番要求窓を決める live source
    # (yfinance/twelvedata/mt5) は別契約なので混同しない。
    days = live_window_days(args.window_source, args.interval,
                            args.lookback_days)
    since = now - timedelta(days=days)
    windowed = [bar for bar in all_bars if bar.ts >= since]
    result = {
        "db": str(args.db),
        "symbol": args.symbol,
        "history_source": args.history_source,
        "window_source": args.window_source,
        "interval": args.interval,
        "first_ts": all_bars[0].ts.isoformat(),
        "last_ts": all_bars[-1].ts.isoformat(),
        "weekend_gaps_ge_48h": weekend_gaps,
        "window_days": days,
        "before": _measure(all_bars, now, args.freshness_max_min,
                           INTERVAL_MIN[args.interval], args.repeats),
        "after": _measure(windowed, now, args.freshness_max_min,
                          INTERVAL_MIN[args.interval], args.repeats),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
