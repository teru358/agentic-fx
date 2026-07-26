from __future__ import annotations

import sqlite3
from datetime import datetime


def add(conn: sqlite3.Connection, idea: str, source: str, now: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO improvement_backlog (idea, source, created_at, updated_at) "
        "VALUES (?,?,?,?)", (idea, source, now.isoformat(), now.isoformat()))
    conn.commit()
    return cur.lastrowid


def list_open(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM improvement_backlog WHERE status='open' ORDER BY id")]


def set_status(conn: sqlite3.Connection, backlog_id: int, status: str,
               now: datetime) -> None:
    conn.execute(
        "UPDATE improvement_backlog SET status=?, updated_at=? WHERE id=?",
        (status, now.isoformat(), backlog_id))
    conn.commit()
