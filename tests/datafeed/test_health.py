from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.contracts import Bar, Quote
from agentic_fx.datafeed.health import (
    DataUnhealthy, validate_bars, validate_quote,
)

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _bars(n=30, start=None, step_min=60, base=148.0):
    start = start or (NOW - timedelta(minutes=step_min * n))
    return [Bar("USDJPY", "1h", start + timedelta(minutes=step_min * i),
                base, base + 0.1, base - 0.1, base + 0.05, 100)
            for i in range(n)]


def test_fresh_quote_ok():
    validate_quote(Quote("USDJPY", 148.49, 148.51, NOW, "yfinance"),
                   NOW, freshness_max_min=20)


def test_stale_quote_rejected():
    q = Quote("USDJPY", 148.49, 148.51, NOW - timedelta(minutes=25), "yfinance")
    with pytest.raises(DataUnhealthy, match="stale"):
        validate_quote(q, NOW, freshness_max_min=20)


def test_inverted_quote_rejected():
    q = Quote("USDJPY", 148.52, 148.51, NOW, "yfinance")
    with pytest.raises(DataUnhealthy, match="bid/ask"):
        validate_quote(q, NOW, freshness_max_min=20)


def test_bars_ok():
    validate_bars(_bars(), NOW, freshness_max_min=90, interval_min=60)


def test_empty_bars_rejected():
    with pytest.raises(DataUnhealthy, match="empty"):
        validate_bars([], NOW, freshness_max_min=90, interval_min=60)


def test_stale_last_bar_rejected():
    old = _bars(n=10, start=NOW - timedelta(hours=20))
    with pytest.raises(DataUnhealthy, match="stale"):
        validate_bars(old, NOW, freshness_max_min=90, interval_min=60)


def test_gap_rejected():
    bars = _bars()
    del bars[10:14]  # 4 本欠損 (市場オープン中)
    with pytest.raises(DataUnhealthy, match="gap"):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=60)


def test_weekend_gap_is_not_a_gap():
    # 金 20:00 のバー → 日 21:00 再開のバー: 休場ギャップは欠損ではない
    fri = datetime(2026, 7, 24, 18, 0, tzinfo=timezone.utc)
    sun = datetime(2026, 7, 26, 21, 0, tzinfo=timezone.utc)
    bars = [Bar("USDJPY", "1h", fri + timedelta(hours=i),
                148.0, 148.1, 147.9, 148.05, 100) for i in range(3)]
    bars += [Bar("USDJPY", "1h", sun + timedelta(hours=i),
                 148.0, 148.1, 147.9, 148.05, 100) for i in range(3)]
    validate_bars(bars, sun + timedelta(hours=3), freshness_max_min=90,
                  interval_min=60)  # 例外なし


def test_spike_rejected():
    bars = _bars()
    bad = bars[15]
    bars[15] = Bar(bad.symbol, bad.interval, bad.ts, bad.open,
                   bad.high, bad.low, bad.close * 1.2, bad.volume)  # +20%
    with pytest.raises(DataUnhealthy, match="anomal"):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=60)


def test_zero_price_rejected():
    bars = _bars()
    bad = bars[5]
    bars[5] = Bar(bad.symbol, bad.interval, bad.ts, 0.0, bad.high,
                  bad.low, bad.close, bad.volume)
    with pytest.raises(DataUnhealthy, match="anomal"):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=60)
