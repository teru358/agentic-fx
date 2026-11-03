"""History generation used by approval payload compare-and-swap checks."""
from __future__ import annotations

import sqlite3


def history_epoch(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT name FROM data_migrations WHERE name LIKE 'time_repair_%' "
                       "ORDER BY applied_at DESC, name DESC LIMIT 1").fetchone()
    return str(row[0]) if row else "none"
