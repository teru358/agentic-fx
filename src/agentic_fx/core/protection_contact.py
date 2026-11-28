"""停止中に届いた足が、注文の保護価格・指値に触れたかの真偽だけを評価する (純関数)。

約定・決済・損益は計算せず、注文・足のどちらも書き換えない。通常保護 (paper_fills) と
同じ half spread の式を使い、判定の式を二重に持たない。
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agentic_fx.core.contracts import Bar
from agentic_fx.core.paper_fills import check_limit_fill, exit_hits

KIND_SL = "sl"
KIND_TP = "tp"
KIND_BOTH = "both"
KIND_LIMIT = "limit"
KIND_UNKNOWN = "unknown"
KINDS = (KIND_SL, KIND_TP, KIND_BOTH, KIND_LIMIT, KIND_UNKNOWN)

_BAR_WIDTH = timedelta(minutes=1)

# 足の生存窓の判定に使う状態語。保有の有無・保護を確定できない途中の状態は不明に倒す
_HELD = "open"
_PENDING = "pending_fill"
_UNCERTAIN = frozenset({
    "protection_pending", "submitting", "submitted", "closing", "cancelling",
    "submit_unknown", "cancel_unknown", "close_unknown"})
_FILLED_TERMINAL = "closed"
_UNFILLED_TERMINAL = frozenset({"cancelled", "expired"})
_NEVER_LIVE = frozenset({"approval_pending", "rejected", "invalidated"})
_NON_TERMINAL = frozenset({_HELD, _PENDING}) | _UNCERTAIN

@dataclass(frozen=True, slots=True)
class Contact:
    """足と注文の組の判定。`kind` が unknown なら接触の有無を確定できなかった組。"""

    order_id: int
    pair: str
    bar_time: datetime
    kind: str


def _finite(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _parse(text) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _window_end(order: dict, status: str) -> tuple[bool, datetime | None]:
    """(読めたか, 終了時刻)。非終端は上限なし (None)。closed_at が無い終端は updated_at。"""
    if status in _NON_TERMINAL:
        return True, None
    closed = order.get("closed_at")
    if closed is not None:
        parsed = _parse(closed)
        return (parsed is not None), parsed
    parsed = _parse(order.get("updated_at"))
    return (parsed is not None), parsed


def _spread_of(spread, pair: str) -> float | None:
    """評価時の spread。読めない・非有限は None (接触不明)。"""
    try:
        value = spread(pair) if callable(spread) else spread
    except Exception:  # noqa: BLE001 - 読めない spread は不明として扱う
        return None
    return float(value) if _finite(value) else None


def _held_kind(order: dict, bar: Bar, spread: float) -> str | None:
    """保有していた注文の SL/TP の接触。保護条件を読めなければ unknown、触れなければ None。"""
    if order.get("direction") not in ("long", "short"):
        return KIND_UNKNOWN
    sl, tp = order.get("stop_loss"), order.get("take_profit")
    if not _finite(sl) or (tp is not None and not _finite(tp)):
        return KIND_UNKNOWN
    sl_hit, tp_hit = exit_hits(order, bar, spread)
    if sl_hit and tp_hit:
        return KIND_BOTH
    if sl_hit:
        return KIND_SL
    return KIND_TP if tp_hit else None


def _unfilled_kind(order: dict, bar: Bar, spread: float) -> str | None:
    """約定していない注文の指値到達と SL/TP 到達。到達の真偽だけで仮約定はしない。"""
    if order.get("direction") not in ("long", "short") or not _finite(order.get("requested_price")):
        return KIND_UNKNOWN
    if check_limit_fill(order, bar, spread) is not None:
        return KIND_LIMIT
    sl, tp = order.get("stop_loss"), order.get("take_profit")
    if sl is not None and not _finite(sl) or tp is not None and not _finite(tp):
        return KIND_UNKNOWN
    if sl is None:
        return None
    return _held_kind(order, bar, spread)


def _classify(order: dict, bar: Bar, spread: Callable[[], float | None]) -> str | None:
    status = order.get("status")
    if status in _NEVER_LIVE:
        return None
    created = _parse(order.get("created_at"))
    ok_end, end = _window_end(order, status)
    if status not in _NON_TERMINAL and status not in _UNFILLED_TERMINAL \
            and status != _FILLED_TERMINAL:
        return KIND_UNKNOWN
    if created is None or not ok_end:
        return KIND_UNKNOWN
    if bar.ts + _BAR_WIDTH <= created or (end is not None and bar.ts >= end):
        return None
    if status in _UNCERTAIN:
        return KIND_UNKNOWN
    spread = spread()
    if spread is None:
        return KIND_UNKNOWN
    if status == _HELD:
        return _held_kind(order, bar, spread)
    if status == _FILLED_TERMINAL:
        if order.get("filled_at") is None:
            return KIND_UNKNOWN
        return _held_kind(order, bar, spread)
    return _unfilled_kind(order, bar, spread)


def protection_contact(orders: Iterable[dict], bars: Sequence[Bar],
                       spread: float | Callable[[str], float]) -> list[Contact]:
    """足 × 注文の全組を評価し、接触または不明の組だけ返す (足の時刻・注文 id の昇順)。

    窓は `created_at` から終了時刻 (`closed_at`、無ければ `updated_at`、非終端は上限なし) で、
    足の `[ts, ts+1分)` と重なる注文が対象。現在 closed / cancelled でも、窓が重なれば除外しない。
    `spread` は pair を受けて価格単位の spread を返す関数か数値 (評価時の値。足には保存しない)。
    """
    orders = sorted(orders, key=lambda o: o.get("id") or 0)
    spreads = {}

    def resolve(pair):
        if pair not in spreads:
            spreads[pair] = _spread_of(spread, pair)
        return spreads[pair]

    found: list[Contact] = []
    for bar in sorted(bars, key=lambda b: b.ts):
        for order in orders:
            pair = order.get("pair")
            if pair != bar.symbol:
                continue
            kind = _classify(order, bar, lambda: resolve(pair))
            if kind is not None:
                found.append(Contact(order_id=order.get("id"), pair=pair,
                                     bar_time=bar.ts, kind=kind))
    return found
