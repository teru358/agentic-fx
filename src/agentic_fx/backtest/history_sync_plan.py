"""評価用履歴の同期の範囲計画 (純関数。DB・時計・network に触れない)。

足幅 w、確定の猶予 g、固定した時刻 T から、取得する範囲・検査尾・要求窓の数を決める。
開場の判定は共通暦 (`core.market_hours.is_market_open`) で、暦の遷移は常に UTC の正時にある。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Sequence

from agentic_fx.core.market_hours import is_market_open
from agentic_fx.store.ohlcv import inspection_tail_bars

IsOpen = Callable[[datetime], bool]

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)
_SEARCH_MAX = timedelta(days=21)

#: 検査尾の基本の長さと、探索・取得幅の上限 (開場時間)、native 格子数の上限
TAIL_BASE_OPEN = timedelta(hours=2)
TAIL_MAX_OPEN = timedelta(hours=3)
TAIL_MAX_GRID = 180
#: 確定 prefix が 1 回に進む開場時間と、追いつける遅れの上限 (これを超えたら停止中の手動へ)
PREFIX_OPEN = timedelta(hours=2)
BACKLOG_MAX_OPEN = timedelta(hours=24)

REASON_UNINITIALIZED = "cursor_uninitialized"
REASON_BACKLOG = "recent_backlog_offline_required"
REASON_INSPECTION = "inspection_insufficient"
REASON_TAIL_LIMIT = "tail_exceeds_limit"


class PlanError(ValueError):
    """暦を探索しても範囲を決められない。"""


def floor_grid(dt: datetime, width: timedelta) -> datetime:
    return _EPOCH + ((dt - _EPOCH) // width) * width


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def effective_end(requested_end: datetime, now: datetime, *, grace: timedelta,
                  width: timedelta) -> datetime:
    """E = min(要求終端, floor_grid(T - g - w))。確定済みでも余裕の 1 本は E の外。"""
    return min(requested_end, floor_grid(now - grace - width, width))


def clip(start: datetime, end: datetime, *, boundary: datetime, limit: datetime
         ) -> tuple[datetime, datetime] | None:
    """範囲を [B, E) に切り詰める。空なら None。B 以前の行は書かない。"""
    lo, hi = max(start, boundary), min(end, limit)
    return (lo, hi) if lo < hi else None


def advance_open(start: datetime, duration: timedelta, *, is_open: IsOpen = is_market_open
                 ) -> datetime:
    """`start` から開場時間を `duration` 積算した時刻 (排他的終端)。閉場の時間は数えない。"""
    if duration < timedelta(0):
        raise ValueError("duration must be >= 0")
    remaining, cur = duration, start
    while remaining > timedelta(0):
        if cur - start > _SEARCH_MAX:
            raise PlanError("no open time found while advancing")
        seg_end = _floor_hour(cur) + _HOUR
        seg = seg_end - cur
        if is_open(cur):
            if remaining <= seg:
                return cur + remaining
            remaining -= seg
        cur = seg_end
    return cur


def retreat_open(end: datetime, duration: timedelta, *, is_open: IsOpen = is_market_open
                 ) -> datetime:
    """`end` から開場時間を `duration` 遡った時刻 (含む始端)。閉場の時間は数えない。"""
    if duration < timedelta(0):
        raise ValueError("duration must be >= 0")
    remaining, cur = duration, end
    while remaining > timedelta(0):
        if end - cur > _SEARCH_MAX:
            raise PlanError("no open time found while retreating")
        seg_start = _floor_hour(cur - timedelta(microseconds=1))
        seg = cur - seg_start
        if is_open(seg_start):
            if remaining <= seg:
                return cur - remaining
            remaining -= seg
        cur = seg_start
    return cur


def open_duration(start: datetime, end: datetime, *, is_open: IsOpen = is_market_open
                  ) -> timedelta:
    total = timedelta(0)
    cur = start
    while cur < end:
        seg_end = min(_floor_hour(cur) + _HOUR, end)
        if is_open(cur):
            total += seg_end - cur
        cur = seg_end
    return total


def open_slots(start: datetime, end: datetime, *, width: timedelta,
               is_open: IsOpen = is_market_open,
               excluded_dates: frozenset[date] = frozenset()) -> list[datetime]:
    """[start, end) の足格子のうち、開場で無足日でないもの (期待される足の開始時刻)。"""
    out = []
    t = floor_grid(start, width)
    if t < start:
        t += width
    while t < end:
        if is_open(t) and t.date() not in excluded_dates:
            out.append(t)
        t += width
    return out


def has_open_time(start: datetime, end: datetime, *, is_open: IsOpen = is_market_open) -> bool:
    cur = start
    while cur < end:
        if is_open(cur):
            return True
        cur = min(_floor_hour(cur) + _HOUR, end)
    return False


def fetch_windows(start: datetime, end: datetime, *, is_open: IsOpen = is_market_open,
                  utc_day_windows: bool = True) -> list[tuple[datetime, datetime]]:
    """取得の要求窓。UTC 日境界で区切り、全日が閉場の窓は要求にも予算にも数えない。"""
    windows = []
    cur = start
    while cur < end:
        if utc_day_windows:
            nxt = min(datetime(cur.year, cur.month, cur.day, tzinfo=timezone.utc) + _DAY, end)
        else:
            nxt = min(cur + _DAY, end)
        if has_open_time(cur, nxt, is_open=is_open):
            windows.append((cur, nxt))
        cur = nxt
    return windows


@dataclass(frozen=True)
class Tail:
    start: datetime
    end: datetime
    keys: int
    slots: int


@dataclass(frozen=True)
class TailResult:
    tail: Tail | None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.tail is not None


def select_tail(existing_times: Iterable[datetime], anchor: datetime, *,
                width: timedelta, is_open: IsOpen = is_market_open,
                excluded_dates: frozenset[date] = frozenset(),
                boundary: datetime | None = None) -> TailResult:
    """検査尾を、取得の前に、既存の key だけから選ぶ。

    anchor の直前 2 開場時間を基本尾とし、そこに K 本 (K = ceil(2h / w)) の既存 key が
    なければ、より古い既存 key を近い順に足して K 本を覆う連続区間を固定する。区間の長さは
    最大 3 開場時間・最大 180 格子。K 本を揃えられない、または上限を超えるときは尾を作らない
    (検査量を緩めない)。結果を見た拡張はしない。
    """
    need = inspection_tail_bars(int(width.total_seconds()))
    valid = sorted({t for t in existing_times
                    if t < anchor and (boundary is None or t >= boundary)
                    and is_open(t) and t.date() not in excluded_dates
                    and (t - _EPOCH) % width == timedelta(0)})
    try:
        base_start = retreat_open(anchor, TAIL_BASE_OPEN, is_open=is_open)
    except PlanError:
        return TailResult(None, REASON_INSPECTION)
    inside = [t for t in valid if t >= base_start]
    start = base_start
    if len(inside) < need:
        older = [t for t in valid if t < base_start]
        missing = need - len(inside)
        if len(older) < missing:
            return TailResult(None, REASON_INSPECTION)
        start = older[-missing]
        keys = missing + len(inside)
    else:
        keys = len(inside)
    slots = open_slots(start, anchor, width=width, is_open=is_open,
                       excluded_dates=excluded_dates)
    if (open_duration(start, anchor, is_open=is_open) > TAIL_MAX_OPEN
            or len(slots) > TAIL_MAX_GRID):
        return TailResult(None, REASON_TAIL_LIMIT)
    return TailResult(Tail(start, anchor, keys, len(slots)))


@dataclass(frozen=True)
class PrefixPlan:
    ok: bool
    reason: str | None
    cursor: datetime | None = None
    end: datetime | None = None


def plan_prefix(last_processed_e: datetime | None, *, boundary: datetime, limit: datetime,
                is_open: IsOpen = is_market_open) -> PrefixPlan:
    """確定 prefix の追記範囲 [c, F)。c = max(B, last_processed_E)、F = min(E, c から 2 開場時間先)。

    cursor が未初期化なら MAX(bar_time) から推測せず停止中の初回同期へ送る。遅れが 24 開場時間を
    超えるときも自動でコールド範囲を広げない。
    """
    if last_processed_e is None:
        return PrefixPlan(False, REASON_UNINITIALIZED)
    c = max(boundary, last_processed_e)
    if c >= limit:
        return PrefixPlan(True, None, c, c)
    if open_duration(c, limit, is_open=is_open) > BACKLOG_MAX_OPEN:
        return PrefixPlan(False, REASON_BACKLOG, c)
    return PrefixPlan(True, None, c, min(limit, advance_open(c, PREFIX_OPEN, is_open=is_open)))


def lookback_start(limit: datetime, days: int, boundary: datetime) -> datetime:
    """手動同期の後ろ窓の始端。`max(B, E - days 日)` (日境界へは丸めない)。"""
    return max(boundary, limit - timedelta(days=days))


def request_windows(ranges: Sequence[tuple[datetime, datetime]], *,
                    is_open: IsOpen = is_market_open) -> list[tuple[datetime, datetime]]:
    """複数の範囲 (追記・後ろ窓・固定尾) を UTC 日で分割した要求窓の和集合。

    同じ UTC 日の重なった範囲は 1 窓にまとめ、全日閉場の窓は含めない。"""
    spans: dict[datetime, list[datetime]] = {}
    for s, e in ranges:
        for ws, we in fetch_windows(s, e, is_open=is_open):
            day = datetime(ws.year, ws.month, ws.day, tzinfo=timezone.utc)
            cur = spans.get(day)
            spans[day] = [ws, we] if cur is None else [min(cur[0], ws), max(cur[1], we)]
    return [(lo, hi) for _day, (lo, hi) in sorted(spans.items())]


def request_count(ranges: Sequence[tuple[datetime, datetime]], *,
                  is_open: IsOpen = is_market_open) -> int:
    """取得前に数える要求数 (UTC 日窓、全日閉場の窓は数えない、重複窓は 1 回)。尾を含む範囲を渡す。"""
    return len(request_windows(ranges, is_open=is_open))
