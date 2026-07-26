from __future__ import annotations

import sqlite3
from datetime import datetime

from agentic_fx.core.timeutil import as_utc


def add(conn: sqlite3.Connection, *, ts: datetime, balance: float, equity: float,
        hwm: float, cashflow: float = 0.0, source: str = "paper") -> int:
    ts = as_utc(ts)
    cur = conn.execute(
        "INSERT INTO account_snapshots (ts, balance, equity, hwm, cashflow, source) "
        "VALUES (?,?,?,?,?,?)",
        (ts.isoformat(), balance, equity, hwm, cashflow, source))
    conn.commit()
    return cur.lastrowid


def latest(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM account_snapshots ORDER BY ts DESC, id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def first_since(conn: sqlite3.Connection, ts: datetime) -> dict | None:
    row = conn.execute(
        "SELECT * FROM account_snapshots WHERE ts >= ? ORDER BY ts LIMIT 1",
        (ts.isoformat(),)).fetchone()
    return dict(row) if row else None


def last_before(conn: sqlite3.Connection, ts: datetime) -> dict | None:
    row = conn.execute(
        "SELECT * FROM account_snapshots WHERE ts < ? ORDER BY ts DESC LIMIT 1",
        (ts.isoformat(),)).fetchone()
    return dict(row) if row else None
