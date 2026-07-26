"""orders 永続化 CRUD。状態遷移の許可判定は core (プラン 2) の責務。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

_OPTIONAL = {
    "intent_id", "approval_id", "client_order_id", "quantity",
    "filled_quantity", "remaining_quantity", "avg_fill_price",
    "requested_price", "close_price", "stop_loss", "take_profit",
    "fees_swap", "realized_pnl", "close_reason", "broker_order_id",
    "broker_position_id", "broker_synced_at", "expires_at",
    "filled_at", "closed_at",
}


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
    unknown = set(fields) - _OPTIONAL - {"status"}
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
