from __future__ import annotations

import json
import sqlite3
from datetime import datetime


def insert(conn: sqlite3.Connection, mission_id: int, payload: dict,
           now: datetime, *, action: str) -> int:
    cur = conn.execute(
        "INSERT INTO trade_intents "
        "(mission_id,payload_json,action,created_at) VALUES (?,?,?,?)",
        (mission_id, json.dumps(payload, ensure_ascii=False),
         action, now.isoformat()))
    conn.commit()
    return cur.lastrowid


def set_gate_result(conn: sqlite3.Connection, intent_id: int, *,
                    accepted: bool, reject_reason: str | None,
                    reject_category: str | None) -> None:
    conn.execute(
        "UPDATE trade_intents SET gate_result=?,reject_reason=?,"
        "reject_category=? WHERE id=?",
        ("accepted" if accepted else "rejected", reject_reason,
         reject_category, intent_id))
    conn.commit()


def get(conn: sqlite3.Connection, intent_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM trade_intents WHERE id=?",
                       (intent_id,)).fetchone()
    return dict(row) if row else None
