"""週次の確定 (`review_weeks` / `strategy_review_weekly` / `plugin_inventory_weekly`) の保存と読取。

週の記録と 2 つの集計表は 1 transaction で書き、後から変えない (UPDATE / DELETE は
trigger が拒む)。同じ週は 1 度しか確定できず、2 度目は何も書かずに `EXISTS` を返す。
評価の中身 (集計・判定・棚卸し) は `agentic_fx.weekly_review` が作り、ここは保存だけを担う。
金額は持たない。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

RULE_VERSION = 1

STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"
EVAL_NORMAL = "normal"
EVAL_DEGRADED = "degraded"
EVAL_UNEVALUATED = "unevaluated"
INVENTORY_NORMAL = "normal"
INVENTORY_INDETERMINATE = "indeterminate"
INVENTORY_NONE = "none"
ABORT_PERIOD_EXCEEDED = "period_exceeded"

COMMITTED = "committed"
EXISTS = "exists"


@dataclass(frozen=True, slots=True)
class WeekRecord:
    week: str
    window_end: datetime
    finalized_at: datetime
    evaluated_at: datetime | None
    status: str
    evaluation: str
    degraded: dict
    inventory_state: str
    inventory_reason: str | None
    stopped: list
    abort_reason: str | None = None


@dataclass(frozen=True, slots=True)
class StrategyRow:
    name: str
    content_hash: str
    generation_source: int
    generation_op_id: int
    generation_started_at: datetime | None
    state: str
    state_reason: str | None
    first_decided_at: datetime | str | None
    n: int
    wins: int
    losses: int
    flats: int
    r_profit: float
    r_loss: float
    series: dict
    holes: dict
    decisions: dict
    concentration: dict
    short_term: dict
    approval: dict | None
    sign: str
    reasons: list = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class InventoryRow:
    name: str
    state: str
    origin: str
    conditions: list
    proposable: bool
    not_proposable: str | None


def _iso(value: datetime | str | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime は保存しない")
    return value.astimezone(timezone.utc).isoformat()


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def week_exists(conn: sqlite3.Connection, label: str) -> bool:
    return conn.execute("SELECT 1 FROM review_weeks WHERE week=?", (label,)).fetchone() is not None


def recorded_weeks(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT week FROM review_weeks")}


def recent_weeks(conn: sqlite3.Connection, *, since: datetime) -> list[str]:
    """`since` 以後に確定 (または打切り) した週の印を古い順に返す。"""
    return [row[0] for row in conn.execute(
        "SELECT week FROM review_weeks WHERE finalized_at>=? ORDER BY week", (_iso(since),))]


def _previous_unused(conn: sqlite3.Connection, label: str) -> set[str] | None:
    """比較元の週の未使用の名前。比較元 (棚卸しが正常な直前の完了週) が無ければ None。"""
    base = conn.execute(
        "SELECT week FROM review_weeks WHERE status=? AND inventory_state=? AND week<? "
        "ORDER BY week DESC LIMIT 1", (STATUS_COMPLETED, INVENTORY_NORMAL, label)).fetchone()
    if base is None:
        return None
    return {row[0] for row in conn.execute(
        "SELECT name FROM plugin_inventory_weekly WHERE week=? AND state='unused'",
        (base[0],))}


def _insert_week(conn: sqlite3.Connection, record: WeekRecord) -> None:
    conn.execute(
        "INSERT INTO review_weeks (week, window_end, finalized_at, evaluated_at, rule_version, "
        "status, evaluation, degraded_json, inventory_state, inventory_reason, stopped_json, "
        "abort_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (record.week, _iso(record.window_end), _iso(record.finalized_at),
         _iso(record.evaluated_at), RULE_VERSION, record.status, record.evaluation,
         _dump(record.degraded), record.inventory_state, record.inventory_reason,
         _dump(record.stopped), record.abort_reason))


def _commit(conn: sqlite3.Connection, record: WeekRecord, strategies, inventory) -> str:
    if conn.in_transaction:
        raise RuntimeError("connection already has an open transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if week_exists(conn, record.week):
            conn.rollback()
            return EXISTS
        previous = (_previous_unused(conn, record.week)
                    if record.inventory_state == INVENTORY_NORMAL else None)
        _insert_week(conn, record)
        for row in strategies:
            conn.execute(
                "INSERT INTO strategy_review_weekly (week, name, content_hash, generation_source, "
                "generation_op_id, generation_started_at, state, state_reason, first_decided_at, "
                "n, wins, losses, flats, r_profit, r_loss, series_json, holes_json, "
                "decisions_json, concentration_json, short_term_json, approval_json, sign, "
                "reasons_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record.week, row.name, row.content_hash, row.generation_source,
                 row.generation_op_id, _iso(row.generation_started_at), row.state,
                 row.state_reason, _iso(row.first_decided_at), row.n, row.wins, row.losses,
                 row.flats, row.r_profit, row.r_loss, _dump(row.series), _dump(row.holes),
                 _dump(row.decisions), _dump(row.concentration), _dump(row.short_term),
                 None if row.approval is None else _dump(row.approval), row.sign,
                 _dump(row.reasons)))
        for row in inventory:
            is_new = int(previous is not None and row.state == "unused"
                         and row.name not in previous)
            conn.execute(
                "INSERT INTO plugin_inventory_weekly (week, name, state, origin, conditions_json, "
                "proposable, not_proposable, is_new) VALUES (?,?,?,?,?,?,?,?)",
                (record.week, row.name, row.state, row.origin, _dump(row.conditions),
                 int(row.proposable), row.not_proposable, is_new))
        conn.commit()
        return COMMITTED
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def commit_week(conn: sqlite3.Connection, record: WeekRecord,
                strategies: list[StrategyRow], inventory: list[InventoryRow]) -> str:
    """週の記録と 2 表の行を 1 transaction で確定する。どれかが失敗すれば全体を戻して送出する。"""
    return _commit(conn, record, strategies, inventory)


def commit_aborted(conn: sqlite3.Connection, label: str, *, window_end: datetime,
                   finalized_at: datetime, reason: str) -> str:
    """確定できないまま期間を過ぎた週の打切り記録。評価も集計も持たない。"""
    return _commit(conn, WeekRecord(
        week=label, window_end=window_end, finalized_at=finalized_at, evaluated_at=None,
        status=STATUS_ABORTED, evaluation=EVAL_UNEVALUATED, degraded={},
        inventory_state=INVENTORY_NONE, inventory_reason=None, stopped=[],
        abort_reason=reason), [], [])


def _week_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["degraded"] = json.loads(out.pop("degraded_json"))
    out["stopped"] = json.loads(out.pop("stopped_json"))
    return out


def get_week(conn: sqlite3.Connection, label: str) -> dict | None:
    row = conn.execute("SELECT * FROM review_weeks WHERE week=?", (label,)).fetchone()
    return None if row is None else _week_dict(row)


def latest_week(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM review_weeks ORDER BY week DESC LIMIT 1").fetchone()
    return None if row is None else _week_dict(row)


def strategies_of(conn: sqlite3.Connection, label: str) -> list[dict]:
    out = []
    for row in conn.execute("SELECT * FROM strategy_review_weekly WHERE week=? "
                            "ORDER BY name, generation_source, generation_op_id", (label,)):
        item = dict(row)
        for key in ("series", "holes", "decisions", "concentration", "short_term", "reasons"):
            item[key] = json.loads(item.pop(f"{key}_json"))
        approval = item.pop("approval_json")
        item["approval"] = None if approval is None else json.loads(approval)
        item["generation"] = (item["generation_source"], item["generation_op_id"])
        out.append(item)
    return out


def inventory_of(conn: sqlite3.Connection, label: str) -> list[dict]:
    out = []
    for row in conn.execute("SELECT * FROM plugin_inventory_weekly WHERE week=? ORDER BY name",
                            (label,)):
        item = dict(row)
        item["conditions"] = json.loads(item.pop("conditions_json"))
        out.append(item)
    return out
