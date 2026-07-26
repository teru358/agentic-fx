"""approval_requests CRUD。決定は冪等 (pending 以外への decide は拒否) — 設計書 §7。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime


class AlreadyDecidedError(Exception):
    pass


def create(conn: sqlite3.Connection, kind: str, payload: dict, now: datetime,
           expires_at: datetime | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, expires_at, created_at) "
        "VALUES (?,?,?,?)",
        (kind, json.dumps(payload, ensure_ascii=False),
         expires_at.isoformat() if expires_at else None, now.isoformat()))
    conn.commit()
    return cur.lastrowid


def decide(conn: sqlite3.Connection, approval_id: int, *, status: str,
           decided_by: str, now: datetime, reason: str | None = None) -> None:
    cur = conn.execute(
        "UPDATE approval_requests SET status=?, decided_by=?, decided_at=?, reason=? "
        "WHERE id=? AND status='pending'",
        (status, decided_by, now.isoformat(), reason, approval_id))
    conn.commit()
    if cur.rowcount == 0:
        raise AlreadyDecidedError(f"approval {approval_id} is not pending")


def pending(conn: sqlite3.Connection, kind: str | None = None) -> list[dict]:
    q = "SELECT * FROM approval_requests WHERE status='pending'"
    args: list = []
    if kind:
        q += " AND kind=?"
        args.append(kind)
    return [dict(r) for r in conn.execute(q + " ORDER BY id", args)]


def expire_due(conn: sqlite3.Connection, now: datetime) -> int:
    cur = conn.execute(
        "UPDATE approval_requests SET status='expired' "
        "WHERE status='pending' AND expires_at IS NOT NULL AND expires_at < ?",
        (now.isoformat(),))
    conn.commit()
    return cur.rowcount


def set_message_id(conn: sqlite3.Connection, approval_id: int,
                   message_id: str) -> None:
    conn.execute("UPDATE approval_requests SET message_id=? WHERE id=?",
                 (message_id, approval_id))
    conn.commit()
