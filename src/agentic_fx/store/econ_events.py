from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta


def upsert(conn: sqlite3.Connection, *, ts: datetime, country: str, name: str,
           importance: int, actual: str | None = None,
           forecast: str | None = None, previous: str | None = None) -> None:
    conn.execute(
        "INSERT INTO econ_events (ts, country, name, importance, actual, "
        "forecast, previous) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(ts, country, name) DO UPDATE SET importance=excluded.importance, "
        "actual=excluded.actual, forecast=excluded.forecast, previous=excluded.previous",
        (ts.isoformat(), country, name, importance, actual, forecast, previous))
    conn.commit()


def upcoming(conn: sqlite3.Connection, now: datetime, hours: int) -> list[dict]:
    end = now + timedelta(hours=hours)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM econ_events WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (now.isoformat(), end.isoformat()))]
