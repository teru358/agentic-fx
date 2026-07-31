from __future__ import annotations

import sqlite3
from datetime import datetime


def save(conn: sqlite3.Connection, order_id: int, content: str,
         now: datetime) -> None:
    conn.execute(
        "INSERT INTO reflections (order_id, content, created_at) VALUES (?,?,?) "
        "ON CONFLICT(order_id) DO UPDATE SET content=excluded.content",
        (order_id, content, now.isoformat()))
    conn.commit()


def get(conn: sqlite3.Connection, order_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM reflections WHERE order_id=?",
                       (order_id,)).fetchone()
    return dict(row) if row else None


def recent(conn: sqlite3.Connection, n: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM reflections ORDER BY created_at DESC, order_id DESC LIMIT ?", (n,))]


def recent_for_pair(conn: sqlite3.Connection, pair: str, n: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT r.* FROM reflections r JOIN orders o ON o.id = r.order_id "
        "WHERE o.pair = ? ORDER BY r.created_at DESC, r.order_id DESC LIMIT ?", (pair, n))]
