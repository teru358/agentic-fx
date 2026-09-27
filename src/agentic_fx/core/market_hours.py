"""FX 市場時間。

OANDA Japan MT5 実測 (2026-09-06、2025-05〜2026-09 の 5m 実データ):
週末境界・日次ロールオーバーは 21:00 UTC 固定で、DST に追従しない。

`is_friday_after` は `friday_swing_cutoff_ny` を NY 現地時間の hh:mm として
扱い、曜日判定も NY 現地で行う。市場クローズは 21:00 UTC 固定で、NY では
17:00 EDT / 16:00 EST に相当するため、cutoff は通年でその前に設定する。

"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from agentic_fx.core.timeutil import as_utc as _as_utc

_NY = ZoneInfo("America/New_York")
_ROLLOVER_UTC = time(21, 0)
_HOLIDAYS_MONTH_DAY = frozenset({(12, 25), (1, 1)})
DAY_CLOSE_BUFFER = timedelta(minutes=5)


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


_SESSION_SEARCH_MAX_DAYS = 14


def session_start(now: datetime) -> datetime:
    """直近の「閉場→開場」遷移の時刻 (21:00 UTC 境界) を返す。

    開閉の状態は 21:00 UTC の rollover 境界でしか変わらないので、直近の
    境界から 1 日ずつ遡り、その境界で開場かつ 1 分前が閉場の最初の境界を
    返す。曜日の固定パターンではないので、祝日と週末が連結した閉場
    (2026-12-25 金・2027-01-01 金) の途中の金曜 21:00 を開場と誤らない。
    """
    boundary = trading_day_start(_as_utc(now))
    for _ in range(_SESSION_SEARCH_MAX_DAYS):
        if (is_market_open(boundary)
                and not is_market_open(boundary - timedelta(minutes=1))):
            return boundary
        boundary -= timedelta(days=1)
    raise ValueError(
        f"no closed-to-open transition within {_SESSION_SEARCH_MAX_DAYS} "
        f"days before {now.isoformat()}")


def next_expected_trading_time(after: datetime, width: timedelta) -> datetime:
    """``after`` の次に確定するはずの足の開始時刻 (既知の休場を飛ばす)。

    価格源の連続性・不通を判定する側は「固定 `width` 間隔で穴が無いか」を
    見ると週末・12/25・1/1 の既知の休場を欠落と誤判定する。休場かどうかは
    `is_market_open` が判定できるので、この関数はそれを踏まえた「次に期待
    される取引時刻」を返す。

    休場中は `next_rollover` (21:00 UTC 境界) で一気に飛ばす — 1 分刻みで
    ループすると週末で最大 2880 回の反復になるため、既知の境界へジャンプする
    実装にする。
    """
    after = _as_utc(after)
    candidate = after + width
    while not is_market_open(candidate):
        candidate = next_rollover(candidate)
    return candidate


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
