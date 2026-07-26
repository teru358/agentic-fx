"""FX 市場時間 (簡易 UTC 固定境界)。DST 精緻化は finance 移植で置換可能な境界を保つ。"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from agentic_fx.core.timeutil import as_utc as _as_utc

_ROLLOVER_UTC = time(21, 0)  # NY 17:00 相当の日次ロールオーバー (DST 無視の近似)


def is_market_open(now: datetime) -> bool:
    now = _as_utc(now)
    wd, t = now.weekday(), now.timetz().replace(tzinfo=None)
    if wd == 4 and t >= _ROLLOVER_UTC:   # 金 21:00〜
        return False
    if wd == 5:                          # 土
        return False
    if wd == 6 and t < _ROLLOVER_UTC:    # 日 〜21:00
        return False
    return True


def trading_day_start(now: datetime) -> datetime:
    now = _as_utc(now)
    boundary = now.replace(hour=21, minute=0, second=0, microsecond=0)
    if now.timetz().replace(tzinfo=None) < _ROLLOVER_UTC:
        boundary -= timedelta(days=1)
    return boundary


def next_rollover(now: datetime) -> datetime:
    return trading_day_start(now) + timedelta(days=1)


def is_friday_after(now: datetime, cutoff_hhmm: str) -> bool:
    now = _as_utc(now)
    if now.weekday() != 4:
        return False
    hh, mm = map(int, cutoff_hhmm.split(":"))
    return now.timetz().replace(tzinfo=None) >= time(hh, mm)
