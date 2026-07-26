from __future__ import annotations

import sqlite3
from datetime import datetime


def add(conn: sqlite3.Connection, *, ts: datetime, balance: float, equity: float,
        hwm: float, cashflow: float = 0.0, source: str = "paper") -> int:
    cur = conn.execute(
        "INSERT INTO account_snapshots (ts, balance, equity, hwm, cashflow, source) "
        "VALUES (?,?,?,?,?,?)",
        (ts.isoformat(), balance, equity, hwm, cashflow, source))
    conn.commit()
    return cur.lastrowid


def latest(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None
