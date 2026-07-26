from __future__ import annotations

import json
import sqlite3
from datetime import datetime


def start(conn: sqlite3.Connection, loop: str, runner: str, model: str,
          now: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO missions (loop, runner, model, status, started_at) "
        "VALUES (?,?,?,'running',?)", (loop, runner, model, now.isoformat()))
    conn.commit()
    return cur.lastrowid


def finish(conn: sqlite3.Connection, mission_id: int, status: str,
           output: dict | None, transcript: list, now: datetime) -> None:
    conn.execute(
        "UPDATE missions SET status=?, output_json=?, transcript_json=?, "
        "finished_at=? WHERE id=?",
        (status,
         json.dumps(output, ensure_ascii=False) if output is not None else None,
         json.dumps(transcript, ensure_ascii=False),
         now.isoformat(), mission_id))
    conn.commit()


def recent(conn: sqlite3.Connection, n: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM missions ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows]
