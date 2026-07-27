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


# --- 修正ラウンド 1: 指摘 1 (Critical) 再現 ---
# 週末休場を跨ぐ区間全体を免除すると、休場後の実開場中の長期欠損 (別のバグ)
# を見逃してしまう。金曜最後のバー (開場中) の直後から、月曜 (休場明けから
# さらに丸1日経過した時点) までバーが一本もない = 休場分を差し引いても
# 24 本超の開場中欠損があるはずで、これは検出されなければならない。
def test_gap_spanning_weekend_with_real_missing_data_is_detected():
    fri_last = datetime(2026, 7, 24, 20, 0, tzinfo=timezone.utc)  # 金 20:00 (開場中)
    mon_bar = datetime(2026, 7, 27, 21, 0, tzinfo=timezone.utc)   # 月 21:00 (休場明けから24h後)
    bars = [
        Bar("USDJPY", "1h", fri_last, 148.0, 148.1, 147.9, 148.05, 100),
        Bar("USDJPY", "1h", mon_bar, 148.0, 148.1, 147.9, 148.05, 100),
    ]
    with pytest.raises(DataUnhealthy, match="gap"):
        validate_bars(bars, mon_bar, freshness_max_min=90, interval_min=60)


def test_uncountable_gap_fails_closed():
    # サンプル数が上限を超える場合は休場時間を計算しきれない → fail closed。
    bars = [
        Bar("USDJPY", "1m", NOW - timedelta(days=100), 148.0, 148.1, 147.9,
            148.05, 100),
        Bar("USDJPY", "1m", NOW, 148.0, 148.1, 147.9, 148.05, 100),
    ]
    with pytest.raises(DataUnhealthy, match="gap"):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=1)


def test_weekend_gap_sub_hour_interval_uses_interval_granularity():
    # サンプリング刻みが interval_min に追従していることの判別テスト。
    # 15分足でぴったり休場境界に接する区間: interval_min 刻み (15分) なら
    # 休場中サンプルは 192/194、open_gap_units ≈ 1.99 (<=3, 通る) だが、
    # 固定 1 時間刻みのままだと 48/49、open_gap_units ≈ 3.9 (>3, 誤検出) になる。
    fri = datetime(2026, 7, 24, 20, 45, tzinfo=timezone.utc)  # 金 16:45 NY (開場中)
    sun = datetime(2026, 7, 26, 21, 0, tzinfo=timezone.utc)   # 日 17:00 NY (再開)
    bars = [
        Bar("USDJPY", "15m", fri, 148.0, 148.1, 147.9, 148.05, 100),
        Bar("USDJPY", "15m", sun, 148.0, 148.1, 147.9, 148.05, 100),
    ]
    validate_bars(bars, sun, freshness_max_min=90, interval_min=15)  # 例外なし


# --- 修正ラウンド 1: 指摘 2 (Important) 再現 ---
# naive datetime はプロジェクト規約 (timeutil.as_utc) に反してサイレント
# 受理されてはならない。ValueError を要求する。
def test_validate_quote_rejects_naive_now():
    q = Quote("USDJPY", 148.49, 148.51, NOW, "yfinance")
    naive_now = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError):
        validate_quote(q, naive_now, freshness_max_min=20)


def test_validate_quote_rejects_naive_quote_ts():
    naive_ts = NOW.replace(tzinfo=None)
    q = Quote("USDJPY", 148.49, 148.51, naive_ts, "yfinance")
    with pytest.raises(ValueError):
        validate_quote(q, NOW, freshness_max_min=20)


def test_validate_bars_rejects_naive_now():
    with pytest.raises(ValueError):
        validate_bars(_bars(), NOW.replace(tzinfo=None),
                      freshness_max_min=90, interval_min=60)


def test_validate_bars_rejects_naive_bar_ts():
    bars = _bars()
    bad = bars[5]
    bars[5] = Bar(bad.symbol, bad.interval, bad.ts.replace(tzinfo=None),
                  bad.open, bad.high, bad.low, bad.close, bad.volume)
    with pytest.raises(ValueError):
        validate_bars(bars, NOW, freshness_max_min=90, interval_min=60)
