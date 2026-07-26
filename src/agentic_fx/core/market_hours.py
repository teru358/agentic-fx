"""FX 市場時間。日次ロールオーバー/週末境界は NY 現地時間 17:00 基準。

レビュー修正 (codex 6): 従来は UTC 21:00 固定 (夏時間 EDT を前提) だったが、
冬季 (EST, UTC-5) は NY 17:00 が 22:00 UTC になるため、固定 21:00 UTC のまま
だと冬季に日次損失上限のリセット・day 強制決済が本来より 1 時間早まって
しまう (資金制限の意味が変わる)。`zoneinfo.ZoneInfo("America/New_York")` で
現地 17:00 から UTC 境界を構成することで DST を吸収する。

`is_friday_after` は設定キー `friday_swing_cutoff_utc` の名前が示すとおり
明示的に UTC 基準の cutoff であり、NY 現地時間化の対象外 (意図的)。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from agentic_fx.core.timeutil import as_utc as _as_utc

_NY = ZoneInfo("America/New_York")
_LOCAL_ROLLOVER = time(17, 0)  # NY 現地時間の日次ロールオーバー


def is_market_open(now: datetime) -> bool:
    now = _as_utc(now)
    local = now.astimezone(_NY)
    wd, t = local.weekday(), local.timetz().replace(tzinfo=None)
    if wd == 4 and t >= _LOCAL_ROLLOVER:   # 金 17:00 (現地) 〜
        return False
    if wd == 5:                            # 土
        return False
    if wd == 6 and t < _LOCAL_ROLLOVER:    # 日 〜17:00 (現地)
        return False
    return True


def trading_day_start(now: datetime) -> datetime:
    now = _as_utc(now)
    local = now.astimezone(_NY)
    boundary = local.replace(hour=17, minute=0, second=0, microsecond=0)
    if local.timetz().replace(tzinfo=None) < _LOCAL_ROLLOVER:
        boundary -= timedelta(days=1)
    return boundary.astimezone(timezone.utc)


def next_rollover(now: datetime) -> datetime:
    return trading_day_start(now) + timedelta(days=1)


def is_friday_after(now: datetime, cutoff_hhmm: str) -> bool:
    now = _as_utc(now)
    if now.weekday() != 4:
        return False
    hh, mm = map(int, cutoff_hhmm.split(":"))
    return now.timetz().replace(tzinfo=None) >= time(hh, mm)
