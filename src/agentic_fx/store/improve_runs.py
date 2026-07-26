from __future__ import annotations

import sqlite3
from datetime import datetime


def start(conn: sqlite3.Connection, backlog_id: int | None, now: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO improvement_runs (backlog_id, started_at) VALUES (?,?)",
        (backlog_id, now.isoformat()))
    conn.commit()
    return cur.lastrowid


def finish(conn: sqlite3.Connection, run_id: int, *, result: str, now: datetime,
           pr_url: str | None = None, approval_id: int | None = None,
           report_path: str | None = None) -> None:
    conn.execute(
        "UPDATE improvement_runs SET result=?, pr_url=?, approval_id=?, "
        "report_path=?, finished_at=? WHERE id=?",
        (result, pr_url, approval_id, report_path, now.isoformat(), run_id))
    conn.commit()
