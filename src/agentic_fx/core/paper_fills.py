"""ペーパー約定判定 (純関数) — 1 分足 high/low、SL 優先、ギャップ滑り。設計書 §5。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Sequence

from agentic_fx.core.contracts import Bar
from agentic_fx.core import market_hours
from agentic_fx.store import ohlcv_slices, signal_shadow


def check_limit_fill(order: dict, bar: Bar, spread: float = 0.0) -> float | None:
    price = order["requested_price"]
    half = spread / 2
    if order["direction"] == "long":
        # long のエントリーは ask 側 (bar + half) で判定 — mid/bid 系列に対して保守側
        if bar.open + half <= price:      # ギャップ: 有利な open で約定
            return bar.open + half
        if bar.low + half <= price:
            return price
    else:
        # short のエントリーは bid 側 (bar - half) で判定
        if bar.open - half >= price:
            return bar.open - half
        if bar.high - half >= price:
            return price
    return None


def check_exit(order: dict, bar: Bar, spread: float, *,
               entry_same_bar: bool = False) -> tuple[str, float] | None:
    sl, tp = order["stop_loss"], order["take_profit"]
    long = order["direction"] == "long"
    half = spread / 2

    if long:
        sl_hit = bar.low - half <= sl or bar.open - half <= sl
        tp_hit = tp is not None and bar.high - half >= tp
    else:
        sl_hit = bar.high + half >= sl or bar.open + half >= sl
        tp_hit = tp is not None and bar.low + half <= tp

    if sl_hit:
        # ギャップ: open が SL を超えていれば open 基準で滑る
        if long:
            base = min(bar.open, sl)
            return ("sl", base - half)
        base = max(bar.open, sl)
        return ("sl", base + half)
    if tp_hit:
        if entry_same_bar:
            # 同一バー内でのエントリー成立と TP 到達の順序は判定不能
            # (SL 未到達のためタイブレークにも該当しない) → この足では確定させない
            return None
        return ("tp", tp - half if long else tp + half)
    return None


@dataclass(frozen=True, slots=True)
class LimitEntrySelection:
    fill_bar: object | None
    entry_price: float | None
    range_end: datetime
    indeterminate: bool = False


@dataclass(frozen=True, slots=True)
class ShadowResolution:
    state: str
    result_code: str
    entry_price: float | None
    entry_at: datetime | None
    exit_price: float | None
    exit_at: datetime | None
    range_end: datetime
    pnl_pips: float | None = None


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc)


def _as_bar(row) -> Bar:
    if isinstance(row, Bar):
        return row
    return Bar("econ", "1m", row.bar_time, row.open, row.high, row.low,
               row.close, row.volume or 0.0)


def _is_marker(row) -> bool:
    return (not isinstance(row, Bar)
            and row.quality == ohlcv_slices.QUALITY_OUTAGE)


def day_close_deadline(origin: datetime) -> datetime:
    """起点の次の rollover から day close の buffer を引いた時刻 (shadow の期限)。"""
    return market_hours.next_rollover(origin) - market_hours.DAY_CLOSE_BUFFER


def _range_rows(rows, start: datetime, end: datetime) -> list:
    """区間 [start,end) の開場 minute が 1 行ずつ揃っていることを確かめ、区間内の行を返す。

    正規化結果 (`range_start` / `range_end` を持つもの) は区間が要求を覆うことも
    確かめる。読み戻した行の列などの素の列も同じ網羅を要求する。
    """
    if hasattr(rows, "range_start") or hasattr(rows, "range_end"):
        if rows.range_start is None or rows.range_end is None:
            raise ValueError("normalized bars must include range_start and range_end")
        if _utc(rows.range_start) > start or _utc(rows.range_end) < end:
            raise ValueError("normalized range does not cover requested interval")
    raw = rows.rows if hasattr(rows, "rows") else rows
    if start >= end:
        return []
    selected = [row for row in raw if start <= _utc(
        row.ts if isinstance(row, Bar) else row.bar_time) < end]
    stamps = [_utc(row.ts if isinstance(row, Bar) else row.bar_time)
              for row in selected]
    present = set(stamps)
    if len(present) != len(stamps):
        duplicate = next(at for at in stamps if stamps.count(at) > 1)
        raise ValueError(f"duplicate minute {duplicate.isoformat()}")
    missing = next((minute for minute in ohlcv_slices.open_minutes(start, end)
                    if minute not in present), None)
    if missing is not None:
        raise ValueError(f"missing open minute {missing.isoformat()}")
    return selected


def select_limit_entry(rows: Sequence, *, t0: datetime, requested_price: float,
                       direction: str, spread: float) -> LimitEntrySelection:
    """[t0,t0+4h) の開場 minute を順に見て、最初の約定か marker を返す。

    入力は約定 (または marker) の足までの開場 minute を 1 行ずつ全部持つこと。約定も
    marker も無ければ [t0,t0+4h) 全体を要求する。欠け・重複は `ValueError`。
    """
    t0 = _utc(t0)
    if direction not in ("long", "short"):
        raise ValueError(f"unknown direction: {direction!r}")
    expiry = t0 + timedelta(hours=4)
    if hasattr(rows, "range_start") or hasattr(rows, "range_end"):
        if rows.range_start is None or rows.range_end is None:
            raise ValueError("normalized bars must include range_start and range_end")
        if _utc(rows.range_start) > t0:
            raise ValueError("normalized range does not cover requested interval")
        covered_until = _utc(rows.range_end)
    else:
        covered_until = None
    raw = rows.rows if hasattr(rows, "rows") else rows
    by_minute: dict[datetime, object] = {}
    for row in raw:
        at = _utc(row.ts if isinstance(row, Bar) else row.bar_time)
        if not t0 <= at < expiry:
            continue
        if at in by_minute:
            raise ValueError(f"duplicate minute {at.isoformat()}")
        by_minute[at] = row
    order = {"direction": direction, "requested_price": requested_price}
    for at in ohlcv_slices.open_minutes(t0, expiry):
        if covered_until is not None and at >= covered_until:
            raise ValueError("normalized range does not cover requested interval")
        row = by_minute.get(at)
        if row is None:
            raise ValueError(f"missing open minute {at.isoformat()}")
        if _is_marker(row):
            return LimitEntrySelection(None, None, expiry, True)
        price = check_limit_fill(order, _as_bar(row), spread)
        if price is not None:
            deadline = day_close_deadline(at)
            bar_end = at + timedelta(minutes=1)
            return LimitEntrySelection(row, price,
                                       bar_end if bar_end > deadline else deadline)
    return LimitEntrySelection(None, None, expiry)


def _result(result_code: str, *, direction: str, entry_price: float | None,
            entry_at: datetime | None, exit_price: float | None,
            exit_at: datetime | None, range_end: datetime,
            pip_size: float) -> ShadowResolution:
    pnl = None
    if exit_price is not None and entry_price is not None:
        pnl = signal_shadow.pnl_pips(direction, entry=entry_price,
                                      exit_price=exit_price, pip_size=pip_size)
    state = (signal_shadow.STATE_EXPIRED if result_code == signal_shadow.RESULT_NO_FILL
             else signal_shadow.STATE_RESOLVED)
    return ShadowResolution(state, result_code, entry_price, entry_at,
                            exit_price, exit_at, range_end, pnl)


def _insufficient(direction: str, pip_size: float, range_end: datetime, *,
                  entry_price: float | None = None,
                  entry_at: datetime | None = None) -> ShadowResolution:
    return _result(signal_shadow.RESULT_DATA_INSUFFICIENT, direction=direction,
                   entry_price=entry_price, entry_at=entry_at, exit_price=None,
                   exit_at=None, range_end=range_end, pip_size=pip_size)


def resolve_shadow(rows: Sequence, *, direction: str, kind: str,
                   requested_price: float | None, stop_loss: float,
                   take_profit: float | None, spread: float, t0: datetime,
                   pip_size: float) -> ShadowResolution:
    """前段 (`select_limit_entry`) は [t0,t0+4h)、解決は [t0,区間終端) の正規化結果を渡す。

    limit の区間終端が t0+4h より前 (約定後の day close が早い) なら、解決の入力は
    [t0,区間終端) だけでよい。
    """
    t0 = signal_shadow.observation_start(_utc(t0))
    if kind not in ("market", "limit"):
        raise ValueError(f"unknown entry kind: {kind!r}")
    if direction not in ("long", "short"):
        raise ValueError(f"unknown direction: {direction!r}")
    if (not isinstance(pip_size, (int, float)) or not math.isfinite(pip_size)
            or pip_size <= 0):
        raise ValueError("pip_size must be finite and > 0")
    if kind == "limit" and requested_price is None:
        raise ValueError("limit entry requires requested_price")
    unrecoverable = bool(getattr(rows, "unrecoverable", False))
    if unrecoverable:
        end = day_close_deadline(t0) if kind == "market" else t0 + timedelta(hours=4)
        return _insufficient(direction, pip_size, end)
    raw_rows = rows.rows if hasattr(rows, "rows") else rows
    ordered = sorted(raw_rows, key=lambda row: _utc(
        row.ts if isinstance(row, Bar) else row.bar_time))
    times = lambda row: _utc(row.ts if isinstance(row, Bar) else row.bar_time)
    marker_times = {times(row) for row in ordered if _is_marker(row)}

    entry_row = None
    entry_price = None
    if kind == "market":
        deadline = day_close_deadline(t0)
        ordered = sorted(_range_rows(rows, t0, deadline), key=times)
        marker_times = {times(row) for row in ordered if _is_marker(row)}
        for row in ordered:
            at = times(row)
            if at < t0 or at >= deadline:
                continue
            if at in marker_times:
                return _insufficient(direction, pip_size, deadline)
            entry_row = row
            half = spread / 2
            bar = _as_bar(row)
            entry_price = bar.open + half if direction == "long" else bar.open - half
            break
        if entry_row is None:
            return _result(signal_shadow.RESULT_NO_FILL, direction=direction,
                           entry_price=None, entry_at=None, exit_price=None,
                           exit_at=None, range_end=deadline, pip_size=pip_size)
    else:
        selection = select_limit_entry(rows, t0=t0,
                                       requested_price=requested_price,
                                       direction=direction, spread=spread)
        if selection.indeterminate:
            return _insufficient(direction, pip_size, selection.range_end)
        if selection.fill_bar is None:
            return _result(signal_shadow.RESULT_NO_FILL, direction=direction,
                           entry_price=None, entry_at=None, exit_price=None,
                           exit_at=None, range_end=selection.range_end,
                           pip_size=pip_size)
        entry_row, entry_price = selection.fill_bar, selection.entry_price
        deadline = selection.range_end
        ordered = sorted(_range_rows(rows, t0, deadline), key=times)
        marker_times = {times(row) for row in ordered if _is_marker(row)}

    entry_at = times(entry_row)
    order = {"direction": direction, "requested_price": requested_price,
             "stop_loss": stop_loss, "take_profit": take_profit}
    eligible = [row for row in ordered if entry_at <= times(row) < deadline
                and times(row) + timedelta(minutes=1) <= deadline]
    if entry_row not in eligible:
        eligible.insert(0, entry_row)
    for index, row in enumerate(eligible):
        at = times(row)
        if at in marker_times:
            return _insufficient(direction, pip_size, deadline,
                                 entry_price=entry_price, entry_at=entry_at)
        hit = check_exit(order, _as_bar(row), spread,
                         entry_same_bar=(index == 0 and kind == "limit"))
        if hit is not None:
            code, price = hit
            return _result(code, direction=direction, entry_price=entry_price,
                           entry_at=entry_at, exit_price=price,
                           exit_at=at + timedelta(minutes=1),
                           range_end=deadline, pip_size=pip_size)
    last = eligible[-1]
    close = _as_bar(last).close
    half = spread / 2
    close_price = close - half if direction == "long" else close + half
    return _result(signal_shadow.RESULT_DAY_CLOSE, direction=direction,
                   entry_price=entry_price, entry_at=entry_at,
                   exit_price=close_price,
                   exit_at=times(last) + timedelta(minutes=1),
                   range_end=deadline, pip_size=pip_size)
