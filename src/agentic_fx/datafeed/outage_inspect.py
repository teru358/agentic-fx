"""停止中に届いた足の接触検査の材料集め (読取りだけ)。

どの関数も commit・削除をしない。後着の表の消費と接触参照の保存は observe が状態の保存と
同じ transaction で行う。注文・約定・損益は書き換えない。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from agentic_fx.core.contracts import Bar
from agentic_fx.core.protection_contact import protection_contact
from agentic_fx.core.timeutil import as_utc
from agentic_fx.datafeed.outage_transition import Finding
from agentic_fx.store import ohlcv, orders, outage_facts

BAR_WIDTH = timedelta(minutes=1)
# 通常保護が最新の足として評価する鮮度 (scheduler の鮮度窓と同じ値。test で突き合わせる)
LATEST_BAR_FRESHNESS = timedelta(minutes=5)


def _orders_for(conn, bars: Iterable[Bar]) -> list[dict]:
    bars = list(bars)
    if not bars:
        return []
    pairs = sorted({b.symbol for b in bars})
    start = min(b.ts for b in bars)
    end = max(b.ts for b in bars) + BAR_WIDTH
    return orders.list_overlapping(conn, pairs, start, end)


def _findings(contacts) -> list[Finding]:
    return [Finding(c.order_id, c.pair, c.bar_time, c.kind) for c in contacts]


def late_bar_findings(conn, source: str, spread: Callable[[str], float]
                      ) -> tuple[list[Finding], list[tuple[str, str, datetime]]]:
    """未検査の後着足の接触と、検査した行の key。評価で例外が出たら送出し、呼び出し側は行を消さない。"""
    rows = outage_facts.list_late_bars(conn, source)
    if not rows:
        return [], []
    bars = [bar for bar, _detected in rows]
    found = protection_contact(_orders_for(conn, bars), bars, spread)
    return _findings(found), [(b.symbol, b.interval, b.ts) for b in bars]


def new_bar_findings(conn, source: str, ranges: dict[str, tuple[datetime | None, datetime]],
                     spread: Callable[[str], float], now: datetime
                     ) -> tuple[list[Finding], list[Finding]]:
    """non-ready の間に新規保存された足 (pair ごとの (下限を含まない水位, 上限を含む水位)) の接触。

    返り値は (最新・鮮度内の足と建玉・未約定指値の組を除いた組, 最新・鮮度内の足と未約定指値の組)。
    最新・鮮度内の足は通常保護が評価する足で、建玉の分は通常保護に任せる。未約定指値の分は、
    ready になる遷移では通常の約定に任せ、non-ready のままなら止める根拠として別に返す。"""
    now = as_utc(now)
    bars: list[Bar] = []
    latest: dict[str, datetime] = {}
    for pair, (lower, upper) in sorted(ranges.items()):
        if upper is None or (lower is not None and upper <= lower):
            continue
        found = ohlcv.load_cache_bars(conn, pair, "1m", source=source, until=upper)
        if lower is not None:
            found = [b for b in found if b.ts > lower]
        bars.extend(found)
        row = conn.execute(
            "SELECT MAX(bar_time) FROM ohlcv_cache WHERE symbol=? AND interval='1m' AND source=?",
            (pair, source)).fetchone()
        if row and row[0]:
            latest[pair] = as_utc(datetime.fromisoformat(row[0]))
    if not bars:
        return [], []
    order_rows = _orders_for(conn, bars)
    status = {o["id"]: o.get("status") for o in order_rows}
    first: list[Finding] = []
    latest_pending: list[Finding] = []
    for item in _findings(protection_contact(order_rows, bars, spread)):
        newest = latest.get(item.pair)
        is_latest = (newest is not None and item.bar_time == newest
                     and now - newest <= LATEST_BAR_FRESHNESS)
        if not is_latest:
            first.append(item)
        elif status.get(item.order_id) == "pending_fill":
            latest_pending.append(item)
        elif status.get(item.order_id) != "open":
            first.append(item)
    return first, latest_pending
