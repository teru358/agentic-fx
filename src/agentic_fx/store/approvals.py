"""approval_requests CRUD。決定は冪等 (pending 以外への decide は拒否) — 設計書 §7。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Literal


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


def decide(conn: sqlite3.Connection, approval_id: int, *,
           status: Literal["approved", "rejected"],
           decided_by: str, now: datetime, reason: str | None = None) -> None:
    if status not in ("approved", "rejected"):
        raise ValueError(
            f"status must be 'approved' or 'rejected', got {status!r}")
    now_iso = now.isoformat()
    cur = conn.execute(
        "UPDATE approval_requests SET status=?, decided_by=?, decided_at=?, reason=? "
        "WHERE id=? AND status='pending' AND (expires_at IS NULL OR expires_at >= ?)",
        (status, decided_by, now_iso, reason, approval_id, now_iso))
    conn.commit()
    if cur.rowcount == 0:
        row = conn.execute(
            "SELECT status, expires_at FROM approval_requests WHERE id=?",
            (approval_id,)).fetchone()
        if row is not None and row["status"] == "pending":
            # pending だが期限切れ (expires_at <= now) だったため更新対象外だった。
            # この場で expired に確定させる (fail closed: 期限切れの承認は成立させない)。
            conn.execute(
                "UPDATE approval_requests SET status='expired' WHERE id=?",
                (approval_id,))
            conn.commit()
            raise AlreadyDecidedError(
                f"approval {approval_id} has expired and cannot be decided")
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
