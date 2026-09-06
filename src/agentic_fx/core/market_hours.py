"""FX 市場時間。

OANDA Japan MT5 実測 (2026-09-06、2025-05〜2026-09 の 5m 実データ):
週末境界・日次ロールオーバーは 21:00 UTC 固定で、DST に追従しない。

`is_friday_after` も設定キー `friday_swing_cutoff_ny` の名前が示すとおり
NY 現地時間基準に変更した (レビュー修正: 従来は UTC 固定の cutoff だったため、
市場クローズ (NY 金 17:00) までの残り時間が DST の季節で 1 時間ずれる欠陥が
あった)。渡される cutoff_hhmm は NY 現地時間の hh:mm として扱い、曜日判定も
NY 現地の曜日で行う。

"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from agentic_fx.core.timeutil import as_utc as _as_utc

_NY = ZoneInfo("America/New_York")
_ROLLOVER_UTC = time(21, 0)
_HOLIDAYS_MONTH_DAY = frozenset({(12, 25), (1, 1)})


def is_market_open(now: datetime) -> bool:
    now = _as_utc(now)
    wd, t = now.weekday(), now.timetz().replace(tzinfo=None)
    # 祝日は UTC 暦日でなく**取引日ラベル** (21:00 UTC 起点、= サーバ UTC+3 の日付)
    # で判定する。実測 (2025-12 / 2026-01): 12/24 21:00 UTC に閉場し 12/25 21:00 UTC
    # に再開 = 取引日 12/25 が休場。12/25 21:00 以降は取引日 12/26 で開場。
    label = (now + timedelta(hours=3)).date()
    if (label.month, label.day) in _HOLIDAYS_MONTH_DAY:
        return False
    if wd == 4 and t >= _ROLLOVER_UTC:
        return False
    if wd == 5:
        return False
    if wd == 6 and t < _ROLLOVER_UTC:
        return False
    return True


def trading_day_start(now: datetime) -> datetime:
    now = _as_utc(now)
    boundary = now.replace(hour=21, minute=0, second=0, microsecond=0)
    if now.timetz().replace(tzinfo=None) < _ROLLOVER_UTC:
        boundary -= timedelta(days=1)
    return boundary


def next_rollover(now: datetime) -> datetime:
    now = _as_utc(now)
    boundary = now.replace(hour=21, minute=0, second=0, microsecond=0)
    if now.timetz().replace(tzinfo=None) >= _ROLLOVER_UTC:
        boundary += timedelta(days=1)
    return boundary


def is_friday_after(now: datetime, cutoff_hhmm: str) -> bool:
    """NY金曜の設定cutoff以降か。

    週末クローズは他関数の 21:00 UTC であり、この cutoff はそれより前に
    置くこと。
    """
    now = _as_utc(now)
    local = now.astimezone(_NY)
    if local.weekday() != 4:
        return False
    hh, mm = map(int, cutoff_hhmm.split(":"))
    return local.timetz().replace(tzinfo=None) >= time(hh, mm)
