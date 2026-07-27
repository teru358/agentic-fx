"""データ健全性検証 — 「取得成功」でなくこの検証の通過がフォールバック採用条件 (設計書 §5)。

バー timestamp はバーの開始時刻とする。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from agentic_fx.core import market_hours
from agentic_fx.core.contracts import Bar, Quote

_MAX_GAP_BARS = 3
_SPIKE_PCT = 10.0


class DataUnhealthy(Exception):
    pass


def validate_quote(quote: Quote, now: datetime,
                   freshness_max_min: float) -> None:
    if now - quote.ts > timedelta(minutes=freshness_max_min):
        raise DataUnhealthy(f"quote stale: {quote.ts} (source={quote.source})")
    if not (quote.bid > 0 and quote.ask > 0 and
            math.isfinite(quote.bid) and math.isfinite(quote.ask)):
        raise DataUnhealthy("quote has non-positive/NaN price")
    if quote.bid > quote.ask:
        raise DataUnhealthy(f"bid/ask inverted: {quote.bid} > {quote.ask}")


def _spans_market_close(a: datetime, b: datetime) -> bool:
    """区間 [a, b] に市場クローズ時間が含まれるか (1 時間刻みサンプリング)。"""
    cur = a
    while cur <= b:
        if not market_hours.is_market_open(cur):
            return True
        cur += timedelta(hours=1)
    return not market_hours.is_market_open(b)


def validate_bars(bars: list[Bar], now: datetime, freshness_max_min: float,
                  interval_min: float) -> None:
    if not bars:
        raise DataUnhealthy("empty bars")
    # ts はバー開始時刻: 確定直後を stale にしないため interval 分を許容に足す
    allowed = timedelta(minutes=freshness_max_min + interval_min)
    if now - bars[-1].ts > allowed:
        raise DataUnhealthy(f"bars stale: last={bars[-1].ts}")
    prev = None
    for b in bars:
        vals = (b.open, b.high, b.low, b.close)
        if any(v <= 0 or not math.isfinite(v) for v in vals):
            raise DataUnhealthy(f"anomalous bar (zero/NaN) at {b.ts}")
        if prev is not None:
            gap = (b.ts - prev.ts).total_seconds() / 60 / interval_min
            if gap > _MAX_GAP_BARS and not _spans_market_close(prev.ts, b.ts):
                raise DataUnhealthy(f"gap of {gap:.0f} bars before {b.ts}")
            move = abs(b.close - prev.close) / prev.close * 100
            if move > _SPIKE_PCT:
                raise DataUnhealthy(f"anomalous spike {move:.1f}% at {b.ts}")
        prev = b
