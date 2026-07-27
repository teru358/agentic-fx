"""データ健全性検証 — 「取得成功」でなくこの検証の通過がフォールバック採用条件 (設計書 §5)。

バー timestamp はバーの開始時刻とする。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from agentic_fx.core import market_hours
from agentic_fx.core.contracts import Bar, Quote
from agentic_fx.core.timeutil import as_utc

_MAX_GAP_BARS = 3
_SPIKE_PCT = 10.0
# 休場時間サンプリングの上限回数。超過する場合は「数え切れない = 健全と
# 断言できない」として fail closed (DataUnhealthy) にする (修正ラウンド 1: 指摘 1)。
_MAX_CLOSED_TIME_SAMPLES = 100_000


class DataUnhealthy(Exception):
    pass


def validate_quote(quote: Quote, now: datetime,
                   freshness_max_min: float) -> None:
    now = as_utc(now)
    ts = as_utc(quote.ts)
    if now - ts > timedelta(minutes=freshness_max_min):
        raise DataUnhealthy(f"quote stale: {ts} (source={quote.source})")
    if not (quote.bid > 0 and quote.ask > 0 and
            math.isfinite(quote.bid) and math.isfinite(quote.ask)):
        raise DataUnhealthy("quote has non-positive/NaN price")
    if quote.bid > quote.ask:
        raise DataUnhealthy(f"bid/ask inverted: {quote.bid} > {quote.ask}")


def _closed_minutes(a: datetime, b: datetime, interval_min: float) -> float | None:
    """[a, b] のうち市場クローズだった時間 (分) を interval_min 刻みでサンプリング
    して推定する。刻み幅はバーの足種 (1m/5m/15m/... ) に追従させる (固定 1 時間
    刻みだと sub-hour 足の休場境界を誤判定するため)。

    サンプル数が上限を超える場合は None を返す (fail closed: 呼び出し側で
    健全性を断言しない)。
    """
    step_min = max(interval_min, 1.0)
    total_min = (b - a).total_seconds() / 60
    if total_min <= 0:
        return 0.0
    n_samples = int(total_min / step_min) + 2
    if n_samples > _MAX_CLOSED_TIME_SAMPLES:
        return None
    step = timedelta(minutes=step_min)
    cur = a
    closed = 0
    samples = 0
    while cur <= b:
        samples += 1
        if not market_hours.is_market_open(cur):
            closed += 1
        cur += step
    if samples == 0:
        return 0.0
    return closed / samples * total_min


def validate_bars(bars: list[Bar], now: datetime, freshness_max_min: float,
                  interval_min: float) -> None:
    if not bars:
        raise DataUnhealthy("empty bars")
    now = as_utc(now)
    last_ts = as_utc(bars[-1].ts)
    # ts はバー開始時刻: 確定直後を stale にしないため interval 分を許容に足す
    allowed = timedelta(minutes=freshness_max_min + interval_min)
    if now - last_ts > allowed:
        raise DataUnhealthy(f"bars stale: last={last_ts}")
    prev_ts: datetime | None = None
    prev_close: float | None = None
    for b in bars:
        ts = as_utc(b.ts)
        vals = (b.open, b.high, b.low, b.close)
        if any(v <= 0 or not math.isfinite(v) for v in vals):
            raise DataUnhealthy(f"anomalous bar (zero/NaN) at {ts}")
        if prev_ts is not None:
            gap_units = (ts - prev_ts).total_seconds() / 60 / interval_min
            if gap_units > _MAX_GAP_BARS:
                # 区間全体を免除するのではなく、休場だった時間だけを差し引き、
                # 残り (開場中のはずの欠損) が閾値を超えるかで判定する
                # (修正ラウンド 1: 指摘 1 — 丸ごと免除は fail-open だった)。
                closed_min = _closed_minutes(prev_ts, ts, interval_min)
                if closed_min is None:
                    raise DataUnhealthy(
                        f"gap of {gap_units:.0f} bars before {ts} "
                        "(unable to verify market hours within sample limit)")
                open_gap_units = gap_units - closed_min / interval_min
                if open_gap_units > _MAX_GAP_BARS:
                    raise DataUnhealthy(
                        f"gap of {open_gap_units:.0f} open-market bars before {ts}")
            move = abs(b.close - prev_close) / prev_close * 100
            if move > _SPIKE_PCT:
                raise DataUnhealthy(f"anomalous spike {move:.1f}% at {ts}")
        prev_ts = ts
        prev_close = b.close
