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
        "SELECT * FROM reflections ORDER BY created_at DESC LIMIT ?", (n,))]
