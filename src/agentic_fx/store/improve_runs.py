"""improvement_runs CRUD (設計書 §4.1、裁定7)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime


def start(conn: sqlite3.Connection, backlog_id: int | None, now: datetime, *,
          mission_id: int | None = None, commit: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO improvement_runs (backlog_id, mission_id, started_at) "
        "VALUES (?,?,?)", (backlog_id, mission_id, now.isoformat()))
    if commit:
        conn.commit()
    return cur.lastrowid


def bind_backlog(conn: sqlite3.Connection, run_id: int, backlog_id: int, *,
                 commit: bool = True) -> None:
    conn.execute("UPDATE improvement_runs SET backlog_id=? WHERE id=?",
                (backlog_id, run_id))
    if commit:
        conn.commit()


def finish(conn: sqlite3.Connection, run_id: int, *, result: str | None,
           now: datetime, approval_id: int | None = None,
           report_path: str | None = None, report_state: str = "none",
           commit: bool = True) -> bool:
    """戻り値: `True` = この呼び出しが対象行を更新した。`False` = `run_id`
    に該当する行が存在しなかった (rowcount=0、fail-open 防止)。"""
    cur = conn.execute(
        "UPDATE improvement_runs SET result=?, approval_id=?, "
        "report_path=?, report_state=?, finished_at=? WHERE id=?",
        (result, approval_id, report_path, report_state, now.isoformat(), run_id))
    if commit:
        conn.commit()
    return cur.rowcount == 1


def set_report_state(conn: sqlite3.Connection, run_id: int, report_state: str,
                     *, report_path: str | None = None,
                     commit: bool = True) -> None:
    if report_path is not None or report_state == "failed":
        conn.execute(
            "UPDATE improvement_runs SET report_state=?, report_path=? "
            "WHERE id=?", (report_state, report_path, run_id))
    else:
        conn.execute(
            "UPDATE improvement_runs SET report_state=? WHERE id=?",
            (report_state, run_id))
    if commit:
        conn.commit()
