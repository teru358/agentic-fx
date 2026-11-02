from __future__ import annotations
import sqlite3
from datetime import datetime

GATE_REJECT_STREAK_KEY = "gate_reject.last_notified_streak_id"
_KNOWN_KEYS = frozenset({GATE_REJECT_STREAK_KEY})


def get(conn: sqlite3.Connection, key: str) -> str | None:
    if key not in _KNOWN_KEYS:
        raise ValueError(f"unknown alert_state key: {key}")
    row = conn.execute("SELECT value FROM alert_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set(conn: sqlite3.Connection, key: str, value: str, *, now: datetime) -> None:
    if key not in _KNOWN_KEYS:
        raise ValueError(f"unknown alert_state key: {key}")
    conn.execute(
        "INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        "updated_at=excluded.updated_at", (key, value, now.isoformat()))
    conn.commit()
