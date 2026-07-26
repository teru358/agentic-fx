from __future__ import annotations

import sqlite3
from datetime import datetime


def add(conn: sqlite3.Connection, *, name: str, fetcher: str, url: str,
        added_by: str, now: datetime, enabled: bool = False) -> int:
    cur = conn.execute(
        "INSERT INTO news_sources (name, fetcher, url, enabled, added_by, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (name, fetcher, url, int(enabled), added_by, now.isoformat()))
    conn.commit()
    return cur.lastrowid


def list_all(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM news_sources ORDER BY id")]


def list_enabled(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM news_sources WHERE enabled=1 ORDER BY id")]


def set_enabled(conn: sqlite3.Connection, source_id: int, enabled: bool) -> None:
    conn.execute("UPDATE news_sources SET enabled=? WHERE id=?",
                 (int(enabled), source_id))
    conn.commit()
