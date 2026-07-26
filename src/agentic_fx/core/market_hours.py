"""FX 市場時間。日次ロールオーバー/週末境界は NY 現地時間 17:00 基準。

レビュー修正 (codex 6): 従来は UTC 21:00 固定 (夏時間 EDT を前提) だったが、
冬季 (EST, UTC-5) は NY 17:00 が 22:00 UTC になるため、固定 21:00 UTC のまま
だと冬季に日次損失上限のリセット・day 強制決済が本来より 1 時間早まって
しまう (資金制限の意味が変わる)。`zoneinfo.ZoneInfo("America/New_York")` で
現地 17:00 から UTC 境界を構成することで DST を吸収する。

`is_friday_after` も設定キー `friday_swing_cutoff_ny` の名前が示すとおり
NY 現地時間基準に変更した (レビュー修正: 従来は UTC 固定の cutoff だったため、
市場クローズ (NY 金 17:00) までの残り時間が DST の季節で 1 時間ずれる欠陥が
あった)。渡される cutoff_hhmm は NY 現地時間の hh:mm として扱い、曜日判定も
NY 現地の曜日で行う。

**注意 (再発防止 — 方式比較の過程で発見したバグの教訓):** `next_rollover` /
`trading_day_start` は必ず**現地カレンダー基準**で計算すること。tz-aware
datetime への `timedelta` の加減算は壁時計ベース (tzinfo を保ったまま naive
フィールドを操作) なので、NY 現地の datetime に対して `+ timedelta(days=1)`
すれば「翌カレンダー日の同じ現地時刻」になり、その後 `astimezone(utc)` で
その日の正しいオフセットが適用される。**逆に、UTC に変換した後に
timedelta を加減算すると絶対時間になってしまい、DST 遷移週末に 1 時間
ずれる** (2026 年: 3/8 開始の週は 23h、11/1 終了の週は 25h が正しいのに、
UTC 側で +24h すると春は 1 時間遅く・秋は 1 時間早くなるバグがあった)。
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
    now = _as_utc(now)
    local = now.astimezone(_NY)
    boundary = local.replace(hour=17, minute=0, second=0, microsecond=0)
    if local.timetz().replace(tzinfo=None) >= _LOCAL_ROLLOVER:
        boundary += timedelta(days=1)  # 壁時計で翌日の現地 17:00
    return boundary.astimezone(timezone.utc)


def is_friday_after(now: datetime, cutoff_hhmm: str) -> bool:
    now = _as_utc(now)
    local = now.astimezone(_NY)
    if local.weekday() != 4:
        return False
    hh, mm = map(int, cutoff_hhmm.split(":"))
    return local.timetz().replace(tzinfo=None) >= time(hh, mm)
