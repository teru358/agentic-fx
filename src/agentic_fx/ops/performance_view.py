"""資金推移と戦略ごとの成績を、読取専用の 1 本の transaction で集計する。

web の Performance 画面が使う読取 API の本体。DB を書かず、ops レーンも使わない。
図は判定の根拠にしない。今の資金保護の状態 (``current``) は daemon の判定と同じ関数で出す。
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field, replace
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_fx.core import accounting
from agentic_fx.datafeed.price_provider import _SPECS
from agentic_fx.store import snapshots
from agentic_fx.store.db import connect_readonly

from . import events as ops_events
from .contracts import ErrorCode, OpsError
from . import trade_events

RANGES = {"7d": 7, "30d": 30, "90d": 90, "all": None}
DEFAULT_RANGE = "30d"

MAX_POINTS = 280
MAX_STEPS = 320
MAX_SEGS = 32
MAX_MARKS = 20
MAX_STRATEGY_ROWS = 32
SERIES_EVENTS = 120
MAX_SERIES = 4
GAP_SEC = 600.0
BUCKET_DIVISOR = 279
MIN_COUNT = 5
REFERENCE_COUNT = 30

SPECIAL_LIMIT = 2000      # 切れ目・印の候補として読む行数の上限
REGION_LIMIT = 400        # 図のために集計する区間の上限
EVENT_LIMIT = 5000

# unixepoch(..., 'subsec') が入った版。これ未満では読めない。
MIN_SQLITE = (3, 42, 0)
CHECK_EVERY = 256  # Python 側の行ごとの処理で、期限・停止を確かめる間隔 (行数)
PROGRESS_OPS = 20000      # 打ち切りの確認の間隔 (SQLite の VM 命令数)

_SOURCES = ("paper", "manual_ks_rebase")
_REBASE_SOURCE = "manual_ks_rebase"
_MAX_PLAIN = 1e300

_KIND_RANK = {"latched": 0, "released": 1, "rebase": 2, "cashflow": 3, "unknown_hwm_drop": 4}


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: object) -> datetime | None:
    """aware な ISO 8601 だけを UTC の datetime にする。読めなければ None。"""
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _number(value: object) -> float | None:
    """有限の数 (bool 不可) だけを float にする。"""
    if type(value) not in (int, float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _int(value: object) -> int | None:
    return value if type(value) is int else None


# --- 行の検査 (SQL と Python の両方で同じ規則) ------------------------------------------

def _bad_time(column: str) -> str:
    """UTC の ``YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00`` として読めない時刻 (形と暦と時刻の範囲)。

    型・長さ・接尾辞・区切り・小数部の桁を安い順に見て、SQLite の ``unixepoch`` が読めない
    もの (月・日・分・秒の範囲外) を外す。``unixepoch`` が通してしまう 24 時、30 日までの月の
    31 日、2 月 30・31 日、閏年でない年の 2 月 29 日は文字列の比較で外す (Python の読み方と一致)。
    行ごとに走るので、重い日付の関数は使わない。"""
    year = f"CAST(substr({column},1,4) AS INTEGER)"
    month_day = f"substr({column},6,5)"
    return (f"(typeof({column})<>'text' OR length({column}) NOT IN (25,32) "
            f"OR substr({column},-6)<>'+00:00' "
            f"OR substr({column},11,3) NOT BETWEEN 'T00' AND 'T23' "
            f"OR (length({column})=32 AND (substr({column},20,1)<>'.' "
            f"OR substr({column},21,6) GLOB '*[^0-9]*')) "
            f"OR unixepoch({column}) IS NULL OR {column}<'0001' "
            f"OR (substr({column},9,2)>'28' AND ("
            f"{month_day} IN ('02-30','02-31','04-31','06-31','09-31','11-31') "
            f"OR ({month_day}='02-29' AND NOT ({year}%4=0 AND ({year}%100<>0 OR {year}%400=0))))))")


def _bad_values(alias: str) -> str:
    """時刻・数値が使えない行 (UTC の時刻として読めない、数でない、有限でない)。

    数の検査は ``BETWEEN`` 1 回で、文字列・blob・無限大をまとめて外す
    (NULL は列の制約で無い)。時刻の検査は行ごとの安い条件の後ろに置く。"""
    parts = []
    for column in ("equity", "balance", "hwm", "cashflow"):
        parts.append(f"{alias}{column} NOT BETWEEN -{_MAX_PLAIN!r} AND {_MAX_PLAIN!r}")
    parts.append(_bad_time(f"{alias}ts"))
    return "(" + " OR ".join(parts) + ")"


_DD_SQL = ("(CASE WHEN hwm<=0 THEN 0.0 "
           "ELSE MAX(0.0,(hwm-equity)/hwm*100.0) END)")
_VALID_SQL = ("(NOT " + _bad_values("") + " AND source IN ('paper','manual_ks_rebase'))")


def _special_sql(prev: str, cur: str) -> str:
    """切れ目・印の候補。安い条件から並べ、10 分の差は先頭 15 文字 (10 分の桁まで) が
    変わった行だけ厳密に測る。10 分ちょうどの判定は呼出し側が datetime で行う。"""
    gap = (f"({cur}ts<{prev}ts OR (substr({cur}ts,1,15)<>substr({prev}ts,1,15) AND "
           f"unixepoch({cur}ts,'subsec')-unixepoch({prev}ts,'subsec')>{GAP_SEC - 1.0!r}))")
    return (f"({cur}cashflow<>0 OR {cur}source<>'paper' OR {cur}hwm<{prev}hwm OR {gap} "
            f"OR {_bad_values(cur)})")


_SPECIAL_COLS = ("{c}id AS id, {c}ts AS ts, {p}ts AS prev_ts, {p}hwm AS prev_hwm, "
                 "{c}hwm AS hwm, {c}cashflow AS cashflow, {c}source AS source, "
                 "{bad} AS bad")


def _special_rows_by_id(conn, lo: str, hi: str) -> list[sqlite3.Row]:
    cols = _SPECIAL_COLS.format(c="b.", p="a.", bad=_bad_values("b."))
    return conn.execute(
        f"SELECT {cols} FROM account_snapshots a JOIN account_snapshots b "
        "ON b.id=a.id+1 WHERE a.ts>=? AND a.ts<=? AND b.ts<=? AND "
        f"{_special_sql('a.', 'b.')} ORDER BY b.ts DESC, b.id DESC LIMIT ?",
        (lo, hi, hi, SPECIAL_LIMIT + 1)).fetchall()


def _special_rows_by_window(conn, lo: str, hi: str) -> list[sqlite3.Row]:
    cols = _SPECIAL_COLS.format(c="", p="p_", bad=_bad_values(""))
    inner = ("SELECT id, ts, equity, balance, hwm, cashflow, source, LAG(ts) OVER w AS p_ts, "
             "LAG(hwm) OVER w AS p_hwm FROM account_snapshots WHERE ts>=? AND ts<=? "
             "WINDOW w AS (ORDER BY ts, id)")
    return conn.execute(
        f"SELECT {cols} FROM ({inner}) WHERE p_ts IS NOT NULL AND "
        f"{_special_sql('p_', '')} ORDER BY ts DESC, id DESC LIMIT ?",
        (lo, hi, SPECIAL_LIMIT + 1)).fetchall()


def _ids_follow_time(conn, lo: str, hi: str) -> bool:
    """窓内の id が連続していて、隣り合う id のどの組も時刻が戻らないか。

    成り立てば id の順と ``(ts, id)`` の順が一致し、隣の id との結合が使える。1 回の走査で
    行数・id の幅・戻りの有無を見る。"""
    n, low, high, backwards = conn.execute(
        "SELECT COUNT(*),MIN(a.id),MAX(a.id),MAX(b.ts<a.ts) FROM account_snapshots a "
        "LEFT JOIN account_snapshots b ON b.id=a.id+1 AND b.ts>=? AND b.ts<=? "
        "WHERE a.ts>=? AND a.ts<=?", (lo, hi, lo, hi)).fetchone()
    if n == 0:
        return True
    return high - low + 1 == n and not backwards


# --- 区間と束 -------------------------------------------------------------------------

@dataclass(slots=True)
class _Cut:
    """この位置 (ts, id) の行から新しい区間・束が始まる。"""
    pos: tuple[str, int]
    seg_break: bool = False
    split: bool = False
    bad: bool = False


@dataclass(slots=True)
class _Part:
    seg: int
    region: int
    bucket: int
    count: int
    start_ts: str
    end_ts: str
    equity_min: float
    equity_max: float
    equity_last: float
    balance_min: float
    balance_max: float
    balance_last: float
    first_hwm: float
    last_hwm: float
    rise_ts: str | None
    dd_max: float
    first_equity: float
    first_balance: float
    first_id: int = 0
    last_id: int = 0


@dataclass(slots=True)
class _Mark:
    kind: str
    plot_at: datetime
    occurred_at: datetime | None = None
    recorded_at: datetime | None = None
    source_snapshot_ts: datetime | None = None
    applied_at: datetime | None = None
    generation: int | None = None
    time_source: str = "snapshot"
    order: int = 0

    def dto(self) -> dict:
        def text(value):
            return None if value is None else _iso(value)
        return {"kind": self.kind, "plot_at": _iso(self.plot_at),
                "occurred_at": text(self.occurred_at), "recorded_at": text(self.recorded_at),
                "source_snapshot_ts": text(self.source_snapshot_ts),
                "applied_at": text(self.applied_at), "generation": self.generation,
                "time_source": self.time_source}


@dataclass(slots=True)
class _Quality:
    partial: bool = False
    invalid_snapshots: int = 0
    invalid_marks: int = 0
    invalid_orders: int = 0
    unknown_hwm_drops: int = 0
    unplaced_closed_count: int = 0

    def dto(self) -> dict:
        return {"partial": self.partial, "invalid_snapshots": self.invalid_snapshots,
                "invalid_marks": self.invalid_marks, "invalid_orders": self.invalid_orders,
                "unknown_hwm_drops": self.unknown_hwm_drops,
                "unplaced_closed_count": self.unplaced_closed_count}


def _bucket_width(start: datetime, end: datetime) -> int:
    span = max(0, int((end - start).total_seconds()))
    return max(1, math.ceil(span / BUCKET_DIVISOR))


def _bucket_bounds(index: int, width: int) -> tuple[str, str]:
    low = datetime.fromtimestamp(index * width, tz=timezone.utc)
    high = datetime.fromtimestamp((index + 1) * width, tz=timezone.utc)
    return _iso(low), _iso(high)


def _epoch(value: str) -> int | None:
    parsed = _parse(value)
    return None if parsed is None else int(parsed.timestamp() // 1)


_FAR = "9999-12-31T23:59:59+00:00"


def _range_where(region_start: tuple[str, int], region_end: tuple[str, int] | None,
                 bucket: tuple[str, str], hi: str) -> tuple[str, tuple]:
    """区間 ∩ 束 ∩ 窓の行。時刻の範囲を先に index に渡し、同じ時刻の id の境だけを後で見る。"""
    end = region_end if region_end is not None else (_FAR, 0)
    low = max(region_start[0], bucket[0])
    high = min(end[0], bucket[1])
    return ("ts>=? AND ts<=? AND ts<=? AND (ts>? OR id>=?) AND (ts<? OR id<?) "
            "AND ts<?",
            (low, high, hi, region_start[0], region_start[1], end[0], end[1], bucket[1]))


def _aggregate_part(conn, where: str, args: tuple, *, seg: int, region: int,
                    bucket: int) -> _Part | None:
    agg = conn.execute(
        "SELECT COUNT(*),MIN(equity),MAX(equity),MIN(balance),MAX(balance),MAX(hwm),"
        f"MAX({_DD_SQL}) FROM account_snapshots WHERE {where}", args).fetchone()
    if agg[0] == 0:
        return None
    first = conn.execute(
        "SELECT id,ts,equity,balance,hwm FROM account_snapshots "
        f"WHERE {where} ORDER BY ts,id LIMIT 1", args).fetchone()
    last = conn.execute(
        "SELECT id,ts,equity,balance,hwm FROM account_snapshots "
        f"WHERE {where} ORDER BY ts DESC,id DESC LIMIT 1", args).fetchone()
    rise_ts = None
    if agg[5] > first["hwm"]:
        rise = conn.execute(
            "SELECT ts FROM account_snapshots "
            f"WHERE {where} AND hwm>? ORDER BY ts,id LIMIT 1", (*args, first["hwm"])).fetchone()
        rise_ts = rise[0]
    return _Part(
        seg=seg, region=region, bucket=bucket, count=agg[0], start_ts=first["ts"],
        end_ts=last["ts"], equity_min=agg[1], equity_max=agg[2], equity_last=last["equity"],
        balance_min=agg[3], balance_max=agg[4], balance_last=last["balance"],
        first_hwm=first["hwm"], last_hwm=agg[5], rise_ts=rise_ts, dd_max=agg[6],
        first_equity=first["equity"], first_balance=first["balance"],
        first_id=first["id"], last_id=last["id"])


def _merge(left: _Part, right: _Part, bucket: int) -> _Part:
    rise = left.rise_ts
    if rise is None:
        if right.first_hwm > left.first_hwm:
            rise = right.start_ts
        else:
            rise = right.rise_ts
    return _Part(
        seg=left.seg, region=left.region, bucket=bucket, count=left.count + right.count,
        start_ts=left.start_ts, end_ts=right.end_ts,
        equity_min=min(left.equity_min, right.equity_min),
        equity_max=max(left.equity_max, right.equity_max), equity_last=right.equity_last,
        balance_min=min(left.balance_min, right.balance_min),
        balance_max=max(left.balance_max, right.balance_max), balance_last=right.balance_last,
        first_hwm=left.first_hwm, last_hwm=right.last_hwm, rise_ts=rise,
        dd_max=max(left.dd_max, right.dd_max), first_equity=left.first_equity,
        first_balance=left.first_balance, first_id=left.first_id, last_id=right.last_id)


def _widen(parts: list[_Part], factor: int) -> list[_Part]:
    """隣り合う同じ区間の束を、幅を ``factor`` 倍にして束ね直す (束の境は epoch に揃ったまま)。"""
    merged: list[_Part] = []
    for part in parts:
        bucket = part.bucket // factor
        if merged and merged[-1].region == part.region and merged[-1].bucket == bucket:
            merged[-1] = _merge(merged[-1], part, bucket)
        else:
            merged.append(replace(part, bucket=bucket))
    return merged


def _steps(parts: list[_Part]) -> list[tuple[str, float, int]]:
    """HWM の階段線。束内の上昇は最初に変わった位置に末尾の値を置く (実際より早く高く描く安全側)。
    下降は束を割ってあるので正確な位置と値を残し、区間の始点と終点も必ず返す。"""
    out: list[tuple[str, float, int]] = []
    previous: float | None = None
    for index, part in enumerate(parts):
        if index == 0 or parts[index - 1].seg != part.seg:
            previous = None
        rises = part.last_hwm > part.first_hwm and part.rise_ts is not None
        if previous is None:
            out.append((part.start_ts, part.first_hwm, part.seg))
            if rises:
                out.append((part.rise_ts, part.last_hwm, part.seg))
        elif part.first_hwm > previous:
            out.append((part.start_ts, part.last_hwm, part.seg))
        elif part.first_hwm < previous:
            out.append((part.start_ts, part.first_hwm, part.seg))
            if rises:
                out.append((part.rise_ts, part.last_hwm, part.seg))
        elif rises:
            out.append((part.rise_ts, part.last_hwm, part.seg))
        previous = part.last_hwm
        closing = index + 1 == len(parts) or parts[index + 1].seg != part.seg
        if closing and (out[-1][0] != part.end_ts or out[-1][2] != part.seg):
            out.append((part.end_ts, part.last_hwm, part.seg))
    return out


def _collapse(parts: list[_Part]) -> list[_Part]:
    """同じ区間の束を 1 つにまとめる (束の幅では収まらないとき)。"""
    merged: list[_Part] = []
    for part in parts:
        if merged and merged[-1].region == part.region:
            merged[-1] = _merge(merged[-1], part, 0)
        else:
            merged.append(replace(part, bucket=0))
    return merged


def _fit(parts: list[_Part], width: int, span: int) -> tuple[list[_Part], int, int, int]:
    """代表点・段・区間の上限に収める。幅を倍にして束ね直し、収まらなければ区間ごとに 1 点へ、
    それでも足りなければ古い区間から省く。返り値は (残した束, 最終の幅, 省いた代表点数, 省いた段数)。"""
    def segs(items):
        return len({p.seg for p in items})

    def too_many(items):
        return len(items) > MAX_POINTS or len(_steps(items)) > MAX_STEPS

    factor = 1
    while too_many(parts) and width * factor < 2 * max(span, 1):
        parts, factor = _widen(parts, 2), factor * 2
    width *= factor
    if too_many(parts):
        parts, width = _collapse(parts), max(width, span)
    total_points, total_steps = len(parts), len(_steps(parts))
    while segs(parts) > MAX_SEGS:
        oldest = parts[0].seg
        parts = [p for p in parts if p.seg != oldest]
    while too_many(parts) and len(parts) > 1:
        parts = parts[1:]
    return (parts, width, total_points - len(parts),
            max(0, total_steps - len(_steps(parts))))


# --- 資金推移 -------------------------------------------------------------------------

@dataclass(slots=True)
class _Equity:
    parts: list[_Part] = field(default_factory=list)
    width: int = 1
    stats: dict = field(default_factory=dict)
    marks: list[_Mark] = field(default_factory=list)
    other_sources: int = 0
    omitted_points: int = 0
    omitted_steps: int = 0


def _first_valid(conn, lo: str, hi: str) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT id,ts FROM account_snapshots WHERE ts>=? AND ts<=? AND {_VALID_SQL} "
        "ORDER BY ts,id LIMIT 1", (lo, hi)).fetchone()


def _cuts(conn, lo: str, hi: str, ledger_ids: frozenset[int], quality: _Quality
          ) -> tuple[list[_Cut], list[_Mark], int, tuple[str, int] | None]:
    """窓の中の切れ目と、snapshot 由来の印 (入出金・不明な下降)。

    返り値は (切れ目、印、paper / 付け直し以外の source の行数、読み切れなかった側の先頭位置)。"""
    fast = _ids_follow_time(conn, lo, hi)
    rows = (_special_rows_by_id if fast else _special_rows_by_window)(conn, lo, hi)
    clipped = len(rows) > SPECIAL_LIMIT
    if clipped:
        rows = rows[:SPECIAL_LIMIT]
        quality.partial = True
    rows = list(reversed(rows))
    first = conn.execute(
        "SELECT id,ts,NULL AS prev_ts,NULL AS prev_hwm,hwm,cashflow,source,"
        f"{_bad_values('')} AS bad FROM account_snapshots "
        "WHERE ts>=? AND ts<=? ORDER BY ts,id LIMIT 1", (lo, hi)).fetchone()
    if first is not None and not clipped:
        rows = [first, *rows]
    cuts: dict[tuple[str, int], _Cut] = {}
    marks: list[_Mark] = []
    bad_rows: list[tuple[str, int]] = []
    other_sources = 0

    def cut(pos: tuple[str, int]) -> _Cut:
        return cuts.setdefault(pos, _Cut(pos))

    for row in rows:
        pos = (row["ts"], row["id"])
        here = None if row["bad"] else _parse(row["ts"])
        if row["bad"] or here is None or row["source"] not in _SOURCES:
            bad_rows.append(pos)
            if row["bad"] or here is None:
                quality.invalid_snapshots += 1
            else:
                other_sources += 1
            continue
        before = _parse(row["prev_ts"]) if row["prev_ts"] is not None else None
        if before is not None:
            if here < before or (here - before).total_seconds() > GAP_SEC:
                cut(pos).seg_break = True
                quality.partial = quality.partial or here < before
        known = row["id"] in ledger_ids
        if row["cashflow"] != 0:
            cut(pos).split = True
            marks.append(_Mark("cashflow", here, occurred_at=here, order=row["id"]))
        prev_hwm = _number(row["prev_hwm"])
        dropped = prev_hwm is not None and row["hwm"] < prev_hwm
        if (dropped and row["cashflow"] == 0 and not known) or (
                row["source"] == _REBASE_SOURCE and not known):
            quality.unknown_hwm_drops += 1
            cut(pos).split = True
            marks.append(_Mark("unknown_hwm_drop", here, occurred_at=here, order=row["id"]))
        if known:
            cut(pos).split = True
    for pos in bad_rows:
        cut(pos).bad = True
        cut(pos).seg_break = True
        successor = conn.execute(
            "SELECT ts,id FROM account_snapshots WHERE (ts,id)>(?,?) AND ts<=? "
            "ORDER BY ts,id LIMIT 1", (pos[0], pos[1], hi)).fetchone()
        if successor is not None:
            cut((successor[0], successor[1])).seg_break = True
    older_end = (rows[0]["ts"], rows[0]["id"]) if clipped and rows else None
    return sorted(cuts.values(), key=lambda c: c.pos), marks, other_sources, older_end


def _equity_section(conn, lo: str, hi: str, *, width: int, span: int,
                    ledger_ids: frozenset[int], quality: _Quality) -> _Equity:
    result = _Equity(width=width)
    cuts, marks, other_sources, older_end = _cuts(conn, lo, hi, ledger_ids, quality)
    result.marks, result.other_sources = marks, other_sources
    first = conn.execute("SELECT id,ts FROM account_snapshots WHERE ts>=? AND ts<=? "
                         "ORDER BY ts,id LIMIT 1", (lo, hi)).fetchone()
    if first is None:
        result.stats = _empty_stats()
        return result
    start_pos = (first["ts"], first["id"])
    if older_end is not None and older_end > start_pos:
        start_pos = older_end
    regions: list[tuple[tuple[str, int], bool, bool]] = []
    seg = -1
    starts = [start_pos] + [c.pos for c in cuts if c.pos > start_pos]
    by_pos = {c.pos: c for c in cuts}
    for index, pos in enumerate(starts):
        cut = by_pos.get(pos)
        if index == 0 or (cut is not None and cut.seg_break):
            seg += 1
        regions.append((pos, cut is not None and cut.bad, seg))
    parts: list[_Part] = []
    kept = regions[-REGION_LIMIT:]
    cut_off = starts[len(regions) - len(kept)] if len(kept) < len(regions) else None
    if cut_off is not None:
        quality.partial = True
    for offset, (pos, bad, seg_no) in enumerate(kept):
        if bad:
            continue
        index = len(regions) - len(kept) + offset
        end = starts[index + 1] if index + 1 < len(starts) else None
        last = conn.execute(
            "SELECT ts FROM account_snapshots WHERE (ts,id)>=(?,?) AND (ts,id)<(?,?) AND ts<=? "
            "ORDER BY ts DESC,id DESC LIMIT 1",
            (pos[0], pos[1], *(end if end is not None else (_FAR, 0)), hi)).fetchone()
        if last is None:
            continue
        low_epoch, high_epoch = _epoch(pos[0]), _epoch(last[0])
        if low_epoch is None or high_epoch is None:
            quality.partial = True
            continue
        for bucket in range(low_epoch // width, high_epoch // width + 1):
            where, args = _range_where(pos, end, _bucket_bounds(bucket, width), hi)
            part = _aggregate_part(conn, where, args, seg=seg_no, region=index, bucket=bucket)
            if part is not None:
                parts.append(part)
    older = None
    older_pos = cut_off if cut_off is not None else (older_end if older_end is not None
                                                     and older_end > (first["ts"], first["id"])
                                                     else None)
    if older_pos is not None:
        older = _older_stats(conn, lo, older_pos)
    result.stats = _stats(conn, parts, older, lo, hi)
    fitted, final_width, omitted_points, omitted_steps = _fit(
        parts, width, span)
    result.parts, result.width = fitted, final_width
    result.omitted_points, result.omitted_steps = omitted_points, omitted_steps
    return result


def _older_stats(conn, lo: str, end: tuple[str, int]) -> dict | None:
    where = f"ts>=? AND (ts,id)<(?,?) AND {_VALID_SQL}"
    args = (lo, end[0], end[1])
    agg = conn.execute(
        "SELECT COUNT(*),MIN(equity),MAX(equity),MIN(balance),MAX(balance),"
        f"MAX({_DD_SQL}) FROM account_snapshots WHERE {where}", args).fetchone()
    if agg[0] == 0:
        return None
    first = conn.execute("SELECT id,ts,equity,balance FROM account_snapshots "
                         f"WHERE {where} ORDER BY ts,id LIMIT 1", args).fetchone()
    last = conn.execute("SELECT id,ts,equity,balance FROM account_snapshots "
                        f"WHERE {where} ORDER BY ts DESC,id DESC LIMIT 1", args).fetchone()
    return {"agg": tuple(agg), "first": first, "last": last, "where": where, "args": args}


def _empty_stats() -> dict:
    return {key: None for key in (
        "first_at", "last_at", "equity_first", "equity_last", "balance_first", "balance_last",
        "equity_min", "equity_max", "balance_min", "balance_max", "dd_max", "dd_max_at")}


def _stats(conn, parts: list[_Part], older: dict | None, lo: str, hi: str) -> dict:
    if not parts and older is None:
        return _empty_stats()
    first_ts = first_equity = first_balance = None
    equity_mins, equity_maxs, balance_mins, balance_maxs = [], [], [], []
    last_ts = last_equity = last_balance = None
    dd_best, dd_where = None, None
    if older is not None:
        agg = older["agg"]
        first_ts, first_equity, first_balance = (older["first"]["ts"], older["first"]["equity"],
                                                  older["first"]["balance"])
        last_ts, last_equity, last_balance = (older["last"]["ts"], older["last"]["equity"],
                                               older["last"]["balance"])
        equity_mins.append(agg[1])
        equity_maxs.append(agg[2])
        balance_mins.append(agg[3])
        balance_maxs.append(agg[4])
        dd_best, dd_where = agg[5], (older["where"], older["args"])
    for part in parts:
        if first_ts is None:
            first_ts, first_equity, first_balance = (part.start_ts, part.first_equity,
                                                      part.first_balance)
        last_ts, last_equity, last_balance = part.end_ts, part.equity_last, part.balance_last
        equity_mins.append(part.equity_min)
        equity_maxs.append(part.equity_max)
        balance_mins.append(part.balance_min)
        balance_maxs.append(part.balance_max)
        if dd_best is None or part.dd_max > dd_best:
            dd_best, dd_where = part.dd_max, ("part", part)
    dd_at = None
    if dd_best is not None and dd_where is not None:
        if dd_where[0] == "part":
            part = dd_where[1]
            where = "(ts,id)>=(?,?) AND (ts,id)<=(?,?)"
            args = (part.start_ts, part.first_id, part.end_ts, part.last_id)
        else:
            where, args = dd_where
        row = conn.execute(
            f"SELECT ts FROM account_snapshots WHERE {where} AND {_DD_SQL}=? "
            "ORDER BY ts,id LIMIT 1", (*args, dd_best)).fetchone()
        dd_at = row[0] if row is not None else None

    def at(value):
        parsed = _parse(value)
        return None if parsed is None else _iso(parsed)
    return {"first_at": at(first_ts), "last_at": at(last_ts), "equity_first": first_equity,
            "equity_last": last_equity, "balance_first": first_balance,
            "balance_last": last_balance, "equity_min": min(equity_mins),
            "equity_max": max(equity_maxs), "balance_min": min(balance_mins),
            "balance_max": max(balance_maxs), "dd_max": dd_best, "dd_max_at": at(dd_at)}


def _point_dto(part: _Part) -> dict:
    def at(value):
        return _iso(_parse(value))
    return {"start_at": at(part.start_ts), "end_at": at(part.end_ts), "last_at": at(part.end_ts),
            "equity_min": part.equity_min, "equity_max": part.equity_max,
            "equity_last": part.equity_last, "balance_last": part.balance_last,
            "seg": part.seg}


# --- 印 -------------------------------------------------------------------------------

def _ledger(conn, quality: _Quality) -> tuple[list[dict], frozenset[int]]:
    rows = conn.execute(
        "SELECT l.id,l.applied_at,l.generation,l.latched_at,l.rebase_snapshot_id,"
        "l.source_snapshot_id,s.ts AS source_ts FROM kill_switch_hwm_rebases l "
        "LEFT JOIN account_snapshots s ON s.id=l.source_snapshot_id ORDER BY l.id").fetchall()
    ids = frozenset(row["rebase_snapshot_id"] for row in rows)
    return [dict(row) for row in rows], ids


def _kill_switch_marks(history: list[tuple], ledger: list[dict],
                       kill_switch: dict | None, quality: _Quality) -> list[_Mark]:
    """作動・解除の印。同じ世代・状態の候補を、期間で絞る前に 1 つへ統合する。"""
    latched: dict[int, dict] = {}
    released: dict[int, dict] = {}
    for event_id, ts, state, generation in history:
        stamp = _parse(ts)
        gen = _int(generation)
        if stamp is None or gen is None or gen <= 0 or state not in ("latched", "released"):
            quality.invalid_marks += 1
            continue
        slot = (latched if state == "latched" else released).setdefault(
            gen, {"recorded": stamp, "order": event_id})
        if (stamp, event_id) < (slot["recorded"], slot["order"]):
            slot["recorded"], slot["order"] = stamp, event_id
    for row in ledger:
        gen = _int(row["generation"])
        occurred = _parse(row["latched_at"])
        if gen is None or gen <= 0 or occurred is None:
            continue
        slot = latched.setdefault(gen, {"recorded": None, "order": -row["id"]})
        slot["ledger"] = occurred
    if isinstance(kill_switch, dict):
        gen = _int(kill_switch.get("generation"))
        occurred = _parse(kill_switch.get("latched_at"))
        if kill_switch.get("latched") is True and gen is not None and gen > 0 and occurred:
            slot = latched.setdefault(gen, {"recorded": None, "order": 0})
            slot["state"] = occurred
    marks: list[_Mark] = []
    for gen, slot in latched.items():
        state_at, ledger_at = slot.get("state"), slot.get("ledger")
        if state_at is not None and ledger_at is not None and state_at != ledger_at:
            quality.partial = True
        occurred = state_at or ledger_at
        recorded = slot.get("recorded")
        plot = occurred or recorded
        source = "state" if state_at else "ledger" if ledger_at else "event_ts"
        marks.append(_Mark("latched", plot, occurred_at=occurred, recorded_at=recorded,
                           generation=gen, time_source=source, order=slot["order"]))
    for gen, slot in released.items():
        marks.append(_Mark("released", slot["recorded"], recorded_at=slot["recorded"],
                           generation=gen, time_source="event_ts", order=slot["order"]))
    return marks


def _rebase_marks(ledger: list[dict], quality: _Quality) -> list[_Mark]:
    marks = []
    for row in ledger:
        source_ts, applied = _parse(row["source_ts"]), _parse(row["applied_at"])
        gen = _int(row["generation"])
        if source_ts is None or applied is None or gen is None:
            quality.invalid_marks += 1
            continue
        marks.append(_Mark("rebase", source_ts, source_snapshot_ts=source_ts, applied_at=applied,
                           generation=gen, time_source="ledger", order=row["id"]))
    return marks


def _select_marks(marks: list[_Mark], start: datetime, end: datetime
                  ) -> tuple[list[_Mark], int]:
    inside = [m for m in marks if start <= m.plot_at <= end]
    inside.sort(key=lambda m: (m.plot_at, _KIND_RANK[m.kind], m.order), reverse=True)
    return inside[:MAX_MARKS], max(0, len(inside) - MAX_MARKS)


# --- 今の状態 -------------------------------------------------------------------------

def _state_hold(kill_switch: object) -> str | None:
    """state が約定を止める理由。ラッチ中 (marker の投影を含む) は ``kill_switch``、
    読めない・形が違うは ``unknown``、止めないなら None。"""
    if not isinstance(kill_switch, dict):
        return "unknown"
    latched, pending = kill_switch.get("latched"), kill_switch.get("reconcile_required")
    if type(latched) is not bool or type(pending) is not bool:
        return "unknown"
    return "kill_switch" if latched or pending else None


def current_block(conn: sqlite3.Connection, now: datetime, *, kill_pct: float,
                  kill_switch: dict | None, state_fetched_at: datetime) -> dict:
    """今の資金保護の状態。daemon と同じ最新行・同じ DD の関数で出し、読めなければ不明に倒す。

    ``fill_hold`` は daemon が約定を止める理由の総合 (ラッチ > state 不明 > 口座)。
    ``none`` は、state が読めてラッチも途中の marker も無く、口座が新鮮で equity が正で
    DD が閾値未満のときだけ。"""
    row = snapshots.latest(conn)
    stamp = _parse(row["ts"]) if row is not None else None
    equity = _number(row["equity"]) if row is not None else None
    hwm = _number(row["hwm"]) if row is not None else None
    usable = stamp is not None and equity is not None and hwm is not None
    out = {"kill_switch": kill_switch, "state_fetched_at": _iso(state_fetched_at),
           "snapshot_at": None if stamp is None else _iso(stamp),
           "snapshot_fetched_at": _iso(now), "snapshot_age_sec": None,
           "snapshot_freshness": "unknown", "dd_pct": None, "kill_level": None,
           "headroom": None, "kill_pct": kill_pct, "fill_hold": "unknown",
           "snapshot_max_age_min": accounting.SNAPSHOT_MAX_AGE_MIN}
    account_hold = "unknown"
    if usable:
        out["snapshot_age_sec"] = max(0.0, (now - stamp).total_seconds())
        out["dd_pct"] = accounting.drawdown_pct(equity, hwm)
        out["kill_level"] = hwm * (1.0 - kill_pct / 100.0)
        out["headroom"] = equity - out["kill_level"]
        if accounting.is_stale(stamp, now, max_age_min=accounting.SNAPSHOT_MAX_AGE_MIN):
            out["snapshot_freshness"], account_hold = "stale", "stale"
        elif equity <= 0:
            out["snapshot_freshness"] = "fresh"
        elif accounting.drawdown_kill_reached(equity, hwm, kill_pct):
            out["snapshot_freshness"], account_hold = "fresh", "drawdown"
        else:
            out["snapshot_freshness"], account_hold = "fresh", "none"
    out["fill_hold"] = _state_hold(kill_switch) or account_hold
    return out


# --- 戦略ごとの成績 ---------------------------------------------------------------------

_HOLDING = frozenset({"open", "protection_pending"})
_HOLDING_IF_FILLED = frozenset({"cancelling", "cancel_unknown"})
_PENDING_CLOSE = frozenset({"closing", "close_unknown"})
_ORDER_STATUSES = tuple(sorted(_HOLDING | _HOLDING_IF_FILLED | _PENDING_CLOSE | {"closed"}))
_REASONS = ("llm_close", "sl", "tp", "other")
CRON_ORIGIN = "cron"
UNKNOWN_ORIGIN = "unknown"
OTHER_ORIGIN = "other"


@dataclass(slots=True)
class _Acc:
    origin: str
    count: int = 1
    closed: int = 0
    pnl_known: int = 0
    r_known: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    total: float = 0.0
    r_sum: float = 0.0
    holding: int = 0
    pending: int = 0
    reasons: dict = field(default_factory=lambda: dict.fromkeys(_REASONS, 0))
    events: list = field(default_factory=list)

    def absorb(self, other: "_Acc") -> None:
        for name in ("closed", "pnl_known", "r_known", "wins", "losses", "flats",
                     "gross_profit", "gross_loss", "total", "r_sum", "holding", "pending"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for key in _REASONS:
            self.reasons[key] += other.reasons[key]


def _origin_of(trigger: object) -> str:
    """建てた判断の起動理由。文法に合わない・結合できない注文は「帰属不明」。"""
    code = trade_events.origin_code(trigger)
    return UNKNOWN_ORIGIN if code is None else code


def _risk_multiple(row, account_currency: str, payload: object) -> tuple[float | None, bool]:
    """金額で見た R (手数料込み)。返り値は (R / None, 値が壊れていたか)。

    quote 通貨が口座通貨の注文だけ計算する。初期の SL は注文と判断の payload が一致したものだけ使う。"""
    spec = _SPECS.get(row["pair"])
    qty, avg, stop, pnl = (_number(row[k]) for k in
                           ("quantity", "avg_fill_price", "stop_loss", "realized_pnl"))
    broken = any(row[k] is not None and _number(row[k]) is None
                 for k in ("quantity", "avg_fill_price", "stop_loss", "realized_pnl"))
    try:
        body = json.loads(payload) if type(payload) is str else None
    except (ValueError, RecursionError):
        body = None
    planned = _number(body.get("stop_loss")) if isinstance(body, dict) else None
    if planned is None or stop is None or planned != stop:
        return None, True
    if spec is None or spec.quote_currency != account_currency:
        return None, broken
    if qty is None or qty <= 0 or avg is None or pnl is None:
        return None, True
    risk = abs(avg - stop) * qty * spec.contract_size
    if not math.isfinite(risk) or risk == 0:
        return None, broken
    value = pnl / risk
    return (value if math.isfinite(value) else None), broken


def _series(acc: _Acc, start: datetime, end: datetime) -> list[dict]:
    """決済のたびに段が動く累積損益。時刻を置ける全決済のうち最新 120 件だけを使い、
    それ以前の損益は始点 1 点にまとめる。損益が不明の決済は累計を動かさず、件数にだけ入る。"""
    events = sorted(acc.events, key=lambda e: (e[0], e[1]))
    if len(events) > SERIES_EVENTS:
        earlier = sum(pnl for _, _, pnl in events[:-SERIES_EVENTS] if pnl is not None)
        events = events[-SERIES_EVENTS:]
        points = [(events[0][0], earlier)]
        total = earlier
    else:
        points = [(start, 0.0)]
        total = 0.0
    by_time: dict[datetime, float] = {}
    for stamp, _, pnl in events:
        if pnl is not None:
            by_time[stamp] = by_time.get(stamp, 0.0) + pnl
    for stamp in sorted(by_time):
        total += by_time[stamp]
        points.append((stamp, total))
    points.append((end, total))
    if not all(math.isfinite(value) for _, value in points):
        return []
    return [{"t": _iso(t), "pnl": value} for t, value in points]


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _row_dto(acc: _Acc, series: list[dict]) -> dict:
    known = acc.pnl_known
    return {
        "origin": acc.origin, "origin_count": acc.count, "closed_count": acc.closed,
        "pnl_known_count": known, "r_known_count": acc.r_known,
        "pnl_unknown_count": acc.closed - known, "r_unknown_count": acc.closed - acc.r_known,
        "open_count": acc.holding, "close_pending_count": acc.pending,
        "wins": acc.wins, "losses": acc.losses, "flats": acc.flats,
        "win_rate": acc.wins / known if known else None,
        "pf": _finite(acc.gross_profit / acc.gross_loss
                      if known and acc.gross_loss > 0 else None),
        "avg_r": _finite(acc.r_sum / acc.r_known if acc.r_known else None),
        "total_pnl": _finite(acc.total if known else None),
        "close_reasons": dict(acc.reasons), "cumulative": series}


def _strategy_section(conn, start: datetime, end: datetime, account_currency: str,
                      quality: _Quality, check: Callable[[], None] | None = None
                      ) -> tuple[list[dict], int, int]:
    """起動理由ごとの成績。返り値は (行, 合算した元行の数, 図に出さない系列の数)。"""
    marks = ",".join("?" * len(_ORDER_STATUSES))
    rows = conn.execute(
        "SELECT o.id,o.pair,o.status,o.quantity,o.filled_quantity,o.avg_fill_price,"
        "o.stop_loss,o.realized_pnl,o.close_reason,o.closed_at,m.trigger,i.payload_json "
        "FROM orders o LEFT JOIN trade_intents i ON i.id=o.intent_id "
        "LEFT JOIN missions m ON m.id=i.mission_id "
        f"WHERE o.status IN ({marks}) AND (o.status<>'closed' OR o.closed_at IS NULL "
        f"OR o.closed_at>=? OR {_bad_time('o.closed_at')}) ORDER BY o.id",
        (*_ORDER_STATUSES, _iso(start))).fetchall()
    accs: dict[str, _Acc] = {}
    for number, row in enumerate(rows):
        if check is not None and number % CHECK_EVERY == 0:
            check()
        status = row["status"]
        origin = _origin_of(row["trigger"])
        if status == "closed":
            closed_at = _parse(row["closed_at"])
            if closed_at is None or closed_at > end:
                quality.unplaced_closed_count += 1
                continue
            if closed_at < start:
                continue
            acc = accs.setdefault(origin, _Acc(origin))
            acc.closed += 1
            reason = row["close_reason"] if row["close_reason"] in _REASONS else "other"
            acc.reasons[reason] += 1
            pnl = _number(row["realized_pnl"])
            acc.events.append((closed_at, row["id"], pnl))
            if pnl is None:
                quality.invalid_orders += row["realized_pnl"] is not None
                continue
            acc.pnl_known += 1
            acc.total += pnl
            if pnl > 0:
                acc.wins += 1
                acc.gross_profit += pnl
            elif pnl < 0:
                acc.losses += 1
                acc.gross_loss += -pnl
            else:
                acc.flats += 1
            r, broken = _risk_multiple(row, account_currency, row["payload_json"])
            quality.invalid_orders += 1 if broken else 0
            if r is not None:
                acc.r_known += 1
                acc.r_sum += r
        elif status in _PENDING_CLOSE:
            accs.setdefault(origin, _Acc(origin)).pending += 1
        elif status in _HOLDING or (status in _HOLDING_IF_FILLED
                                    and (_number(row["filled_quantity"]) or 0) > 0):
            accs.setdefault(origin, _Acc(origin)).holding += 1
    if check is not None:
        check()
    ordered = sorted(accs.values(), key=lambda a: (-a.closed, a.origin))
    must = [a for a in ordered if a.origin in (CRON_ORIGIN, UNKNOWN_ORIGIN)]
    rest = [a for a in ordered if a.origin not in (CRON_ORIGIN, UNKNOWN_ORIGIN)]
    merged = 0
    if len(ordered) > MAX_STRATEGY_ROWS:
        room = MAX_STRATEGY_ROWS - 1 - len(must)
        kept, dropped = rest[:max(0, room)], rest[max(0, room):]
        other = _Acc(OTHER_ORIGIN, count=len(dropped))
        for acc in dropped:
            other.absorb(acc)
        merged = len(dropped)
        shown = sorted(must + kept, key=lambda a: (-a.closed, a.origin))
        tail = [other]
    else:
        shown, tail = ordered, []
    drawn = {a.origin for a in rest[:MAX_SERIES]}
    drawn.add(CRON_ORIGIN)
    out, hidden = [], 0
    for acc in shown + tail:
        if check is not None:
            check()
        draw = acc.origin in drawn and acc.closed > 0
        hidden += acc.closed > 0 and not draw
        out.append(_row_dto(acc, _series(acc, start, end) if draw else []))
    return out, merged, hidden


# --- 入口 -----------------------------------------------------------------------------

def read_performance(db_path: Path, *, range_key: str, now: datetime, kill_pct: float,
                     account_currency: str, kill_switch: dict | None,
                     state_fetched_at: datetime,
                     interrupt: Callable[[], OpsError | None] | None = None) -> dict:
    """短命の読取専用の接続で 1 本の transaction を張り、集計して閉じる。

    ``interrupt`` は問合せの途中で定期的に呼ばれ、エラーを返すと読取を打ち切ってそれを送出する
    (部分の応答は返さない)。"""
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise OpsError(
            ErrorCode.UNAVAILABLE,
            detail="sqlite_version_too_old: need "
                   f"{'.'.join(map(str, MIN_SQLITE[:2]))}, have "
                   f"{'.'.join(map(str, sqlite3.sqlite_version_info))}")
    if interrupt is not None:
        early = interrupt()
        if early is not None:
            raise early
    check = None
    if interrupt is not None:
        def check() -> None:
            error = interrupt()
            if error is not None:
                raise error

    conn = connect_readonly(db_path)
    stopped: list[OpsError] = []
    if interrupt is not None:
        def watch() -> int:
            error = interrupt()
            if error is None:
                return 0
            stopped.append(error)
            return 1

        conn.set_progress_handler(watch, PROGRESS_OPS)
    try:
        conn.execute("BEGIN")
        try:
            return build(conn, range_key=range_key, now=now, kill_pct=kill_pct,
                         account_currency=account_currency, kill_switch=kill_switch,
                         state_fetched_at=state_fetched_at, check=check)
        except sqlite3.OperationalError:
            if stopped:
                raise stopped[0] from None
            raise
        finally:
            conn.rollback()
    finally:
        conn.close()


def build(conn: sqlite3.Connection, *, range_key: str, now: datetime, kill_pct: float,
          account_currency: str, kill_switch: dict | None,
          state_fetched_at: datetime,
          check: Callable[[], None] | None = None) -> dict:
    """``check`` は SQL の外の長い処理の途中と、応答を返す直前に呼ばれる。送出すれば読取は打ち切り。"""
    if range_key not in RANGES:
        raise ValueError("range")
    to = now.astimezone(timezone.utc)
    quality = _Quality()

    def poll() -> None:
        if check is not None:
            check()

    ledger, ledger_ids = _ledger(conn, quality)
    poll()
    history, marks_since, clipped = ops_events.kill_switch_event_history(
        conn, limit=EVENT_LIMIT)
    if clipped:
        quality.partial = True
    since = None
    if marks_since is not None:
        since_at = _parse(marks_since)
        if since_at is None:
            quality.partial = True
        else:
            since = _iso(since_at)
    marks = (_kill_switch_marks(history, ledger, kill_switch, quality)
             + _rebase_marks(ledger, quality))
    days = RANGES[range_key]
    if days is not None:
        start = to - timedelta(days=days)
        lo = _iso(start)
    else:
        candidates = [m.plot_at for m in marks]
        row = _first_valid(conn, "0001-01-01T00:00:00+00:00", _iso(to))
        lo = row["ts"] if row is not None else None
        if lo is not None and _parse(lo) is not None:
            candidates.append(_parse(lo))
        for number, r in enumerate(conn.execute(
                "SELECT closed_at FROM orders WHERE status='closed' AND closed_at IS NOT NULL")):
            if number % CHECK_EVERY == 0:
                poll()
            stamp = _parse(r[0])
            if stamp is not None and stamp <= to:
                candidates.append(stamp)
        candidates = [stamp for stamp in candidates if stamp <= to]
        start = min(candidates) if candidates else to
        lo = lo if lo is not None and _parse(lo) == start else _iso(start)
    hi = _iso(to)
    width = _bucket_width(start, to)
    equity = _equity_section(conn, lo, hi, width=width,
                             span=max(1, int((to - start).total_seconds())),
                             ledger_ids=ledger_ids, quality=quality)
    poll()
    marks += equity.marks
    shown, omitted = _select_marks(marks, start, to)
    strategies, merged_rows, hidden_series = _strategy_section(
        conn, start, to, account_currency, quality, check)
    steps = _steps(equity.parts)
    poll()
    result = {
        "range": range_key, "from": _iso(start), "to": hi, "bucket_sec": equity.width,
        "account_currency": account_currency,
        "current": current_block(conn, to, kill_pct=kill_pct, kill_switch=kill_switch,
                                 state_fetched_at=state_fetched_at),
        "points": [_point_dto(part) for part in equity.parts],
        "hwm_steps": [{"t": _iso(_parse(t)), "hwm": hwm, "seg": seg} for t, hwm, seg in steps],
        "stats": equity.stats,
        "marks": [m.dto() for m in shown], "marks_omitted": omitted,
        "marks_since": since,
        "other_sources": equity.other_sources,
        "strategies": strategies,
        "thresholds": {"min_count": MIN_COUNT, "reference_count": REFERENCE_COUNT},
        "omissions": {"equity_points": equity.omitted_points, "hwm_steps": equity.omitted_steps,
                      "strategy_rows": merged_rows, "strategy_series": hidden_series},
        "quality": quality.dto(),
    }
    poll()
    return result
