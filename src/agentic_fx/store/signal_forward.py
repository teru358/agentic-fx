"""戦略ごとの前方成績 (機械採点) の集計。

signal の採否ごとに作る仮想約定 (shadow) の数値終端を、採否時点からの仮想成績として
R で集計する。実約定を含まず、採否に至らなかった signal は成績に含めない (穴として
件数だけ数える)。読取専用の接続と純関数だけを使い、何も書かない。agent に渡す面を
持たない。
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agentic_fx._safe_error import safe_error_text
from agentic_fx.core import market_hours
from agentic_fx.store import signals as signal_store

_log = logging.getLogger(__name__)

WINDOW_WEEKS = 12
RECENT_WEEKS = 4
MIN_COUNT = 5
REFERENCE_COUNT = 30

STATE_OK = "ok"
STATE_UNDETERMINED = "undetermined"
STATE_FAILED = "failed"

# 配備世代 (source, op_id) の source の番号と、応答に出す名前。番号の出所はここ 1 か所
GENERATION_SOURCE_JOURNAL = 0
GENERATION_SOURCE_DEPLOY_OP = 1
GENERATION_SOURCE_NAMES = {GENERATION_SOURCE_JOURNAL: "journal",
                           GENERATION_SOURCE_DEPLOY_OP: "deploy_op"}

LEVEL_NONE = "none"
LEVEL_REFERENCE = "reference"
LEVEL_NORMAL = "normal"

PF_COMPUTED = "computed"
PF_NO_LOSSES = "no_losses"
PF_UNKNOWN = "unknown"

_NUMERIC = ("sl", "tp", "day_close")
_OUTCOMES = (*_NUMERIC, "data_insufficient", "no_fill")
_WEEK = timedelta(weeks=1)


@dataclass(frozen=True, slots=True)
class ForwardTarget:
    """集計の対象 1 件。配備中の戦略の (名前, 内容 hash, 配備世代)。

    世代の開始時刻が読めない対象は `reason` を持ち、数値を出さない。"""
    name: str
    content_hash: str
    started_at: datetime | None
    generation: tuple[int, int] | None    # (source, op_id)
    reason: str | None = None


class ForwardTargets(list):
    """対象の一覧 (名前の順)。`unreadable_entries` は、種別を読めず戦略かどうか決められない
    配備の entry の数で、あれば一覧に載らない戦略があるかもしれない。"""

    def __init__(self, items=(), *, unreadable_entries: int = 0) -> None:
        super().__init__(items)
        self.unreadable_entries = unreadable_entries


def r_multiple(pnl_pips: object, *, entry: object, stop_loss: object,
               pip_size: object) -> float | None:
    """`pnl_pips ÷ (|entry − SL| ÷ pip_size)`。分母が 0 以下・非有限・欠落は None。"""
    values = (pnl_pips, entry, stop_loss, pip_size)
    if any(type(v) not in (int, float) for v in values):
        return None
    if not all(math.isfinite(v) for v in values) or pip_size <= 0:
        return None
    width = abs(entry - stop_loss) / pip_size
    if not width > 0 or not math.isfinite(width):
        return None
    value = pnl_pips / width
    return value if math.isfinite(value) else None


def _level(n: int) -> str:
    if n < MIN_COUNT:
        return LEVEL_NONE
    return LEVEL_NORMAL if n >= REFERENCE_COUNT else LEVEL_REFERENCE


def _series(rows: list[dict]) -> dict:
    """数値終端の行から、R の値 (既知の行だけ) と pips を集計する。"""
    scored = [row for row in rows if row["result_code"] in _NUMERIC]
    rs = [row["r"] for row in scored if row["r"] is not None]
    wins = sum(1 for r in rs if r > 0)
    losses = sum(1 for r in rs if r < 0)
    profit = sum(r for r in rs if r > 0)
    loss = -sum(r for r in rs if r < 0)
    if not rs or not math.isfinite(profit) or not math.isfinite(loss):
        pf, pf_state = None, PF_UNKNOWN
    elif losses == 0:
        pf, pf_state = (None, PF_NO_LOSSES) if profit > 0 else (None, PF_UNKNOWN)
    else:
        pf, pf_state = profit / loss, PF_COMPUTED
    avg = sum(rs) / len(rs) if rs else None
    return {"n": len(rs), "scored": len(scored), "r_unknown": len(scored) - len(rs),
            "wins": wins, "losses": losses, "flats": len(rs) - wins - losses,
            "avg_r": avg if avg is not None and math.isfinite(avg) else None,
            "pf": pf, "pf_state": pf_state,
            "pips_sum": float(sum(row["pnl_pips"] for row in scored)),
            "level": _level(len(rs))}


def _rate(population: list[dict]) -> dict:
    decided = sum(1 for row in population if row["decided"])
    total = len(population)
    return {"signals": total, "decided": decided, "rate": decided / total if total else None}


def _parse(value: object) -> datetime | None:
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def summarize(decided: list[dict], population: list[dict], *, target: ForwardTarget,
              start: datetime, end: datetime, time_unreadable: int = 0) -> dict:
    """集計の純関数。`decided` は窓内の採否 1 行ずつ、`population` は窓内に出た signal。
    `time_unreadable` は時刻を読めず、窓に入るか決められなかった行の数 (穴として数える)。"""
    scored = [row for row in decided if row["result_code"] in _NUMERIC]
    abandoned = Counter(row["abandon_reason"] for row in population
                        if row["status"] == "abandoned")
    unknown = [row for row in decided if row["as_of_unknown"]]
    unterminated = [row for row in decided
                    if row["result_code"] is None and not row["as_of_unknown"]]
    days = Counter(market_hours.trading_date(row["decided_at"]) for row in scored)
    top = max(days.values(), default=0)
    recent_from = max(start, end - RECENT_WEEKS * _WEEK)
    previous_from = max(start, end - 2 * RECENT_WEEKS * _WEEK)
    recent = _rate([row for row in population if recent_from <= row["created_at"] <= end])
    previous = _rate([row for row in population
                      if previous_from <= row["created_at"] < recent_from])
    accepts = [row for row in decided if row["decision"] == "accept"]
    return {
        "name": target.name, "content_hash": target.content_hash,
        "state": STATE_OK, "reason": None,
        "generation": {"started_at": target.started_at.isoformat(),
                       "source": GENERATION_SOURCE_NAMES.get(target.generation[0], "unknown"),
                       "op_id": target.generation[1]},
        "window": {"start": start.isoformat(), "end": end.isoformat(),
                   "weeks": WINDOW_WEEKS},
        "thresholds": {"min_count": MIN_COUNT, "reference_count": REFERENCE_COUNT},
        "series": {"all": _series(decided),
                   "accept": _series(accepts),
                   "reject": _series([r for r in decided if r["decision"] == "reject"])},
        "decisions": {"total": len(decided), "accept": len(accepts),
                      "reject": len(decided) - len(accepts),
                      "gate_passed": sum(1 for r in accepts
                                         if r["gate_result"] == "accepted")},
        "outcomes": {**{code: sum(1 for r in decided if r["result_code"] == code)
                        for code in _OUTCOMES},
                     "unterminated": len(unterminated)},
        "holes": {
            "abandoned": dict(sorted(abandoned.items())),
            "no_fill": sum(1 for r in decided if r["result_code"] == "no_fill"),
            "data_insufficient": sum(1 for r in decided
                                     if r["result_code"] == "data_insufficient"),
            "unterminated": len(unterminated),
            "as_of_unknown": len(unknown),
            "oldest_unterminated_sec": max(
                ((end - r["decided_at"]).total_seconds() for r in unterminated),
                default=None),
            "r_unknown": sum(1 for r in scored if r["r"] is None),
            "time_unreadable": time_unreadable},
        "reach": _rate(population),
        "concentration": {"days": len(days), "max_day_count": top,
                          "max_day_share": top / len(scored) if scored else None},
        "comparison": {"recent": recent, "previous": previous,
                       "comparable": recent["rate"] is not None
                       and previous["rate"] is not None},
    }


def _undetermined(target: ForwardTarget) -> dict:
    return {"name": target.name, "content_hash": target.content_hash,
            "state": STATE_UNDETERMINED, "reason": target.reason or "no_generation_record",
            "thresholds": {"min_count": MIN_COUNT, "reference_count": REFERENCE_COUNT}}


def _payload_stop(payload_json: object) -> object:
    try:
        body = json.loads(payload_json)
    except (TypeError, ValueError):
        return None
    return body.get("stop_loss") if isinstance(body, dict) else None


def forward_summary(conn: sqlite3.Connection, target: ForwardTarget, *,
                    now: datetime) -> dict:
    """配備中の戦略 1 本の、現世代内・最大 12 週の機械採点。

    `now` は評価時刻で、窓の終端であり、採否・abandon・shadow の終端のうち、これより
    後に起きたものは「まだ起きていない」ものとして数える。同じ DB を別の時刻で評価すると
    その時点の姿になる (後から確定する集計も同じ関数を使う)。世代の開始時刻が読めない対象は数値を持たない (「判定不能」)。"""
    if target.started_at is None or target.generation is None or target.reason is not None:
        return _undetermined(target)
    end = now.astimezone(timezone.utc)
    begin = target.started_at.astimezone(timezone.utc)
    start = max(begin, end - WINDOW_WEEKS * _WEEK)
    # SQL の datetime() は秒未満を落とす。単調なので、これで取りこぼしなく絞り、
    # 境界の秒の内側は秒未満まで保った Python の比較で決める。
    args = {"name": target.name, "hash": target.content_hash,
            "start": start.isoformat(), "end": end.isoformat(), "begin": begin.isoformat()}
    decided = []
    unreadable: set[tuple[str, int]] = set()     # 時刻を読めない signal / 採否 (重複して数えない)
    for row in conn.execute(
            "SELECT d.id AS decision_id, s.id AS signal_id, d.decision, d.decided_at, "
            "ti.gate_result, sh.result_code, sh.entry_price, sh.pnl_pips, sh.pip_size, "
            "sh.resolved_at, sh.updated_at, s.created_at, s.payload_json "
            "FROM signal_decisions d JOIN signals s ON s.id = d.signal_id "
            "LEFT JOIN signal_shadow sh ON sh.decision_id = d.id "
            "LEFT JOIN trade_intents ti ON ti.id = d.trade_intent_id "
            "WHERE s.plugin = :name AND s.content_hash = :hash "
            "AND (datetime(s.created_at) IS NULL "
            "OR datetime(s.created_at) >= datetime(:begin)) "
            "AND (datetime(d.decided_at) IS NULL OR (datetime(d.decided_at) >= datetime(:start) "
            "AND datetime(d.decided_at) <= datetime(:end))) "
            "ORDER BY d.decided_at, d.id", args):
        when = _parse(row["decided_at"])
        created = _parse(row["created_at"])
        if created is None:
            unreadable.add(("signal", row["signal_id"]))
            continue
        if when is None:
            unreadable.add(("decision", row["decision_id"]))
            continue
        when = when.astimezone(timezone.utc)
        if not (start <= when <= end) or created.astimezone(timezone.utc) < begin:
            continue
        result, known = _result_as_of(row, end)
        pips = row["pnl_pips"] if result in _NUMERIC else None
        decided.append({
            "decision": row["decision"], "decided_at": when, "gate_result": row["gate_result"],
            "result_code": result, "pnl_pips": pips, "as_of_unknown": not known,
            "r": (r_multiple(pips, entry=row["entry_price"],
                             stop_loss=_payload_stop(row["payload_json"]),
                             pip_size=row["pip_size"]) if result in _NUMERIC else None)})
    population = []
    for row in conn.execute(
            "SELECT s.id AS signal_id, s.created_at, s.status, s.abandon_reason, s.abandoned_at, "
            "d.decided_at AS decided_at "
            "FROM signals s LEFT JOIN signal_decisions d ON d.signal_id = s.id "
            "WHERE s.plugin = :name AND s.content_hash = :hash "
            "AND (datetime(s.created_at) IS NULL OR (datetime(s.created_at) >= datetime(:start) "
            "AND datetime(s.created_at) <= datetime(:end))) "
            f"AND ({signal_store.eligibility_case('s.')}) IN ('eligible', 'normalization_failed') "
            "ORDER BY s.id", args):
        created = _parse(row["created_at"])
        if created is None:
            unreadable.add(("signal", row["signal_id"]))
            continue
        created = created.astimezone(timezone.utc)
        if not (start <= created <= end):
            continue
        decided_at = _parse(row["decided_at"])
        status, reason = row["status"], row["abandon_reason"]
        if status == "abandoned":
            abandoned_at = _parse(row["abandoned_at"])
            if abandoned_at is not None and abandoned_at.astimezone(timezone.utc) > end:
                status, reason = "pending", None
        population.append({
            "created_at": created, "status": status, "abandon_reason": reason,
            "decided": decided_at is not None
            and decided_at.astimezone(timezone.utc) <= end})
    return summarize(decided, population, target=target, start=start, end=end,
                     time_unreadable=len(unreadable))


def _result_as_of(row: sqlite3.Row, end: datetime) -> tuple[str | None, bool]:
    """shadow の終端を、評価時刻の時点の姿にする。(結果, 時点を決められたか)。

    終端が評価時刻より後なら、その時点ではまだ終端していない (結果なし)。終端の時刻が
    読めないものは時点に戻せないので、結果なし・時点不明にする。"""
    result = row["result_code"]
    if result is None:
        return None, True
    stamps = [_parse(row["resolved_at"]), _parse(row["updated_at"])]
    stamps = [s.astimezone(timezone.utc) for s in stamps if s is not None]
    if not stamps:
        return None, False
    return (result if max(stamps) <= end else None), True


def summary_or_failed(conn: sqlite3.Connection, target: ForwardTarget, *, now: datetime,
                      on_error: Callable[[Exception], None] | None = None) -> dict:
    """1 本の集計。失敗してもほかの戦略を隠さないよう、失敗は `failed` の行にする。

    `on_error` は失敗を握りつぶす前に呼ばれ、送出すれば失敗は呼び出し元へ伝わる (中断・
    busy など、行の不明にしてはいけないもの)。"""
    try:
        return forward_summary(conn, target, now=now)
    except Exception as exc:  # noqa: BLE001 - 1 本の失敗でほかの戦略を隠さない
        if on_error is not None:
            on_error(exc)
        _log.warning("forward summary unavailable: %s", safe_error_text(exc))
        return {"name": target.name, "content_hash": target.content_hash,
                "state": STATE_FAILED, "reason": "summary_failed",
                "thresholds": {"min_count": MIN_COUNT, "reference_count": REFERENCE_COUNT}}
