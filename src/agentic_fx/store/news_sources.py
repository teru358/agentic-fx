from __future__ import annotations

import sqlite3
from collections.abc import Iterable
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


def duplicate_of(rows: Iterable[tuple[str, str]], name: str, url: str) -> tuple[str, str] | None:
    """Decide whether (name, url) clashes with ``rows`` of (name, stored url).

    Returns ("name" | "url", existing source name) or None.  A name clash wins
    over a URL clash.  The URL is compared in normalized form, each stored URL
    normalized the same way; one that cannot be normalized is compared as
    written.  Raises NewsInputError when url itself is invalid.
    """
    wanted = normalize_url(url)
    rows = list(rows)
    for existing_name, _ in rows:
        if existing_name == name:
            return "name", existing_name
    for existing_name, stored_url in rows:
        try:
            stored = normalize_url(stored_url)
        except ValueError:
            stored = stored_url
        if stored == wanted:
            return "url", existing_name
    return None


def find_duplicate(conn: sqlite3.Connection, name: str, url: str) -> tuple[str, str] | None:
    """Pre-check for add against every stored row (enabled or not).

    The returned name is the existing source's; no URL text is returned.  The
    UNIQUE constraints remain the final decision.
    """
    return duplicate_of(((row["name"], row["url"]) for row in list_all(conn)), name, url)


def set_enabled(conn: sqlite3.Connection, source_id: int, enabled: bool, *, commit: bool = True) -> None:
    conn.execute("UPDATE news_sources SET enabled=? WHERE id=?",
                 (int(enabled), source_id))
    if commit:
        conn.commit()
