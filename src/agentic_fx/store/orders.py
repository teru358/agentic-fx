"""orders 永続化 CRUD。状態遷移の許可判定は core (プラン 2) の責務。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from agentic_fx.core.protection_contact import NON_TERMINAL_STATUSES

_OPTIONAL = {
    "intent_id", "approval_id", "client_order_id", "quantity",
    "filled_quantity", "remaining_quantity", "avg_fill_price",
    "requested_price", "close_price", "stop_loss", "take_profit",
    "fees_swap", "realized_pnl", "close_reason", "broker_order_id",
    "broker_position_id", "broker_synced_at", "expires_at",
    "filled_at", "closed_at",
}
# 発注の保護条件 (価格・SL・TP) は insert で決まり、その後は変えない。direction は
# insert の集合に無いので update も受けない。停止中の接触検査はこの不変を前提にする
_IMMUTABLE_AFTER_INSERT = {"requested_price", "stop_loss", "take_profit"}
_UPDATABLE = _OPTIONAL - _IMMUTABLE_AFTER_INSERT


def insert(conn: sqlite3.Connection, *, pair: str, direction: str,
           entry_type: str, horizon: str, status: str,
           now: datetime, **optional) -> int:
    unknown = set(optional) - _OPTIONAL
    if unknown:
        raise ValueError(f"unknown order fields: {unknown}")
    cols = ["pair", "direction", "entry_type", "horizon", "status",
            "created_at", "updated_at", *optional]
    vals = [pair, direction, entry_type, horizon, str(status),
            now.isoformat(), now.isoformat(), *optional.values()]
    q = f"INSERT INTO orders ({','.join(cols)}) VALUES ({','.join('?' * len(vals))})"
    cur = conn.execute(q, vals)
    conn.commit()
    return cur.lastrowid


def get(conn: sqlite3.Connection, order_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    return dict(row) if row else None


def update_fields(conn: sqlite3.Connection, order_id: int, *,
                  now: datetime, **fields) -> None:
    frozen = set(fields) & _IMMUTABLE_AFTER_INSERT
    if frozen:
        raise ValueError(f"immutable order fields: {frozen}")
    unknown = set(fields) - _UPDATABLE - {"status"}
    if unknown:
        raise ValueError(f"unknown order fields: {unknown}")
    fields = {k: (str(v) if k == "status" else v) for k, v in fields.items()}
    sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
    conn.execute(f"UPDATE orders SET {sets} WHERE id=?",
                 [*fields.values(), now.isoformat(), order_id])
    conn.commit()


def list_by_status(conn: sqlite3.Connection, *statuses) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT * FROM orders WHERE status IN ({marks}) ORDER BY id",
        [str(s) for s in statuses]).fetchall()
    return [dict(r) for r in rows]


def list_overlapping(conn: sqlite3.Connection, pairs: list[str], start: datetime,
                     end: datetime) -> list[dict]:
    """[start, end) と生存期間が重なり得る注文 (全状態、id 順)。

    作成が end 以前で、非終端か終了 (closed_at、無ければ updated_at) が start 以後のもの。
    保存の時刻表記が UTC 以外でも取りこぼさないよう上下を 1 日広げる。厳密な重なりの判定は
    呼び出し側 (接触の評価) が窓で行う。"""
    if not pairs:
        return []
    pair_marks = ",".join("?" * len(pairs))
    live_marks = ",".join("?" * len(NON_TERMINAL_STATUSES))
    day = timedelta(days=1)
    rows = conn.execute(
        f"SELECT * FROM orders WHERE pair IN ({pair_marks}) AND created_at < ? AND "
        f"(status IN ({live_marks}) OR COALESCE(closed_at, updated_at) > ?) ORDER BY id",
        [*pairs, (end + day).isoformat(), *NON_TERMINAL_STATUSES,
         (start - day).isoformat()]).fetchall()
    return [dict(r) for r in rows]
