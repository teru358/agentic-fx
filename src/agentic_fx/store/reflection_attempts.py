from __future__ import annotations

import sqlite3
from datetime import datetime


def bump(conn: sqlite3.Connection, order_id: int, *, now: datetime,
         reason: str | None) -> int:
    row = conn.execute(
        "INSERT INTO reflection_attempts "
        "(order_id, attempts, last_attempt_at, last_reason) VALUES (?,1,?,?) "
        "ON CONFLICT(order_id) DO UPDATE SET attempts=attempts+1, "
        "last_attempt_at=excluded.last_attempt_at, "
        "last_reason=excluded.last_reason RETURNING attempts",
        (order_id, now.isoformat(), reason)).fetchone()
    conn.commit()
    return int(row["attempts"])


def clear(conn: sqlite3.Connection, order_id: int) -> None:
    conn.execute("DELETE FROM reflection_attempts WHERE order_id=?", (order_id,))
    conn.commit()


def attempts_of(conn: sqlite3.Connection, order_id: int) -> int:
    row = conn.execute(
        "SELECT attempts FROM reflection_attempts WHERE order_id=?", (order_id,)
    ).fetchone()
    return int(row["attempts"]) if row else 0
