"""Durable records for plugin deployment operations."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Iterable, Mapping

from agentic_fx.store.db import _PLUGIN_DEPLOY_OPEN_PHASE_SQL

OPEN_PHASE_SQL = _PLUGIN_DEPLOY_OPEN_PHASE_SQL


def is_open(row: Mapping[str, object]) -> bool:
    return row["phase"] == "intent" or (
        row["phase"] == "needs_retry" and row["fail_reason"] == "syscall_failed")


def is_closed(row: Mapping[str, object]) -> bool:
    return not is_open(row)


def _next_updated_at(conn: sqlite3.Connection, name: str, now: datetime) -> datetime:
    rows = conn.execute(
        "SELECT updated_at FROM plugin_deploy_ops WHERE name=? "
        "UNION ALL SELECT updated_at FROM plugin_switch_journal WHERE name=?",
        (name, name),
    ).fetchall()
    latest = max((datetime.fromisoformat(row[0]) for row in rows), default=None)
    if latest is None:
        return now
    return max(now, latest + timedelta(microseconds=1))


def insert_intent(
        conn: sqlite3.Connection, *, op_kind: str, name: str, from_kind: str,
        to_kind: str, from_target: str | None, to_target: str | None,
        retired_path: str | None, subject_content_hash: str | None,
        subject_artifact_hash: str | None, target_content_hash: str | None,
        target_artifact_hash: str | None, approval_id: int | None, revoke: bool,
        revoke_row_id: int | None, force: bool, actor: str, now: datetime,
        commit: bool = True) -> int:
    updated_at = _next_updated_at(conn, name, now)
    cur = conn.execute(
        "INSERT INTO plugin_deploy_ops (op_kind,name,from_kind,to_kind,from_target,"
        "to_target,retired_path,subject_content_hash,subject_artifact_hash,"
        "target_content_hash,target_artifact_hash,approval_id,revoke,revoke_row_id,"
        "force,phase,fail_reason,notify_attempted,actor,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (op_kind, name, from_kind, to_kind, from_target, to_target, retired_path,
         subject_content_hash, subject_artifact_hash, target_content_hash,
         target_artifact_hash, approval_id, int(revoke), revoke_row_id, int(force),
         "intent", None, 0, actor, now.isoformat(), updated_at.isoformat()),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def set_phase(conn: sqlite3.Connection, op_id: int, phase: str, *, now: datetime,
              fail_reason: str | None = None, notify_attempted: bool | None = None,
              commit: bool = True) -> bool:
    row = conn.execute("SELECT name FROM plugin_deploy_ops WHERE op_id=?", (op_id,)).fetchone()
    if row is None:
        return False
    if notify_attempted is None:
        notify_attempted = False if phase == "intent" else False
    updated_at = _next_updated_at(conn, row["name"], now)
    cur = conn.execute(
        "UPDATE plugin_deploy_ops SET phase=?, fail_reason=?, notify_attempted=?, "
        "updated_at=? WHERE op_id=?",
        (phase, fail_reason, int(notify_attempted), updated_at.isoformat(), op_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount == 1


def get_open_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM plugin_deploy_ops WHERE name=? AND (" + OPEN_PHASE_SQL + ") "
        "ORDER BY op_id DESC LIMIT 1", (name,)).fetchone()
    return dict(row) if row is not None else None


def list_by_name(conn: sqlite3.Connection, name: str) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM plugin_deploy_ops WHERE name=? ORDER BY op_id", (name,))]


def mark_notify_attempted(conn: sqlite3.Connection, op_id: int, *,
                          commit: bool = True) -> bool:
    cur = conn.execute(
        "UPDATE plugin_deploy_ops SET notify_attempted=1 WHERE op_id=?", (op_id,))
    if commit:
        conn.commit()
    return cur.rowcount == 1


def link_signals(conn: sqlite3.Connection, op_id: int,
                 links: Iterable[tuple[int, str]], *, commit: bool = True) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO plugin_deploy_op_signals "
        "(op_id,signal_id,status_at_link) VALUES (?,?,?)",
        ((op_id, signal_id, status) for signal_id, status in links),
    )
    if commit:
        conn.commit()


def linked_signal_ids(conn: sqlite3.Connection, op_id: int) -> set[int]:
    return {row[0] for row in conn.execute(
        "SELECT signal_id FROM plugin_deploy_op_signals WHERE op_id=?", (op_id,))}


def plugin_live_history(conn: sqlite3.Connection, name: str) -> list[dict]:
    events: list[dict] = []
    for row in conn.execute(
            "SELECT op_id,kind,old_kind,old_target,new_target,updated_at "
            "FROM plugin_switch_journal WHERE name=? AND phase='decided' "
            "AND switch_required=1", (name,)):
        events.append({
            "source": "plugin_switch_journal", "id": row["op_id"],
            "kind": row["kind"],
            "from": row["old_target"] if row["old_kind"] != "absent" else "absent",
            "to": row["new_target"], "at": row["updated_at"],
        })
    for row in conn.execute(
            "SELECT * FROM plugin_deploy_ops WHERE name=? AND ("
            "(phase='done' AND op_kind IN ('disable_symlink','rollback_symlink','retire_plain')) "
            "OR (phase='needs_retry' AND fail_reason='verify_failed' AND from_kind='symlink'))",
            (name,)):
        to_value = "absent" if row["phase"] == "needs_retry" else (
            row["retired_path"] if row["op_kind"] == "retire_plain" else
            (row["to_target"] if row["to_kind"] == "symlink" else "absent"))
        events.append({
            "source": "plugin_deploy_ops", "id": row["op_id"],
            "kind": row["op_kind"],
            "from": row["from_target"] if row["from_kind"] == "symlink" else row["from_kind"],
            "to": to_value, "at": row["updated_at"],
        })
    events.sort(key=lambda event: (datetime.fromisoformat(event["at"]), event["source"], event["id"]))
    previous: datetime | None = None
    for event in events:
        instant = datetime.fromisoformat(event["at"])
        effective = instant if previous is None else max(instant, previous + timedelta(microseconds=1))
        event["effective_at"] = effective.isoformat()
        previous = effective
    return events
