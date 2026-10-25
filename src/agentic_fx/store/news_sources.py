from __future__ import annotations

import sqlite3
from datetime import datetime
from agentic_fx.datafeed.news_url import normalize_url


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
    """Pre-check for add: ("name" | "url", existing source name) or None.

    The URL is compared in normalized form against every row (enabled or not),
    each row normalized the same way; a row whose stored URL cannot be
    normalized is compared as written.  The returned name is the existing
    source's; no URL text is returned.  The UNIQUE constraints remain the final
    decision.  Raises NewsInputError when url itself is invalid.
    """
    wanted = normalize_url(url)
    rows = list_all(conn)
    for row in rows:
        if row["name"] == name:
            return "name", row["name"]
    for row in rows:
        try:
            stored = normalize_url(row["url"])
        except ValueError:
            stored = row["url"]
        if stored == wanted:
            return "url", row["name"]
    return None


def set_enabled(conn: sqlite3.Connection, source_id: int, enabled: bool, *, commit: bool = True) -> None:
    conn.execute("UPDATE news_sources SET enabled=? WHERE id=?",
                 (int(enabled), source_id))
    if commit:
        conn.commit()
