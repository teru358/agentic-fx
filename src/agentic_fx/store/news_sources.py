from __future__ import annotations

import sqlite3
from datetime import datetime
from agentic_fx.datafeed.news_probe import normalize_url


def add(conn: sqlite3.Connection, *, name: str, fetcher: str, url: str,
        added_by: str, now: datetime, enabled: bool = False,
        commit: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO news_sources (name, fetcher, url, enabled, added_by, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (name, fetcher, normalize_url(url), int(enabled), added_by, now.isoformat()))
    if commit:
        conn.commit()
    return cur.lastrowid


def list_all(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM news_sources ORDER BY id")]


def list_enabled(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM news_sources WHERE enabled=1 ORDER BY id")]


def find_duplicate(conn: sqlite3.Connection, name: str, url: str) -> tuple[str, str] | None:
    """Return duplicate kind/name, without returning a potentially secret URL."""
    normalized = normalize_url(url)
    for row in list_all(conn):
        if row["name"] == name:
            return "name", row["name"]
        try:
            if normalize_url(row["url"]) == normalized:
                return "url", row["name"]
        except ValueError:
            continue
    return None


def set_enabled(conn: sqlite3.Connection, source_id: int, enabled: bool, *, commit: bool = True) -> None:
    conn.execute("UPDATE news_sources SET enabled=? WHERE id=?",
                 (int(enabled), source_id))
    if commit:
        conn.commit()
