"""plugin_switch_journal 行レベル CRUD (設計書 §5.1-1)。

**この層が保証する範囲**: テーブルの素の CRUD (INSERT・phase 更新・
name ごとの open 検索・非終端一覧)。`begin_switch_journal`/
`advance_switch_journal`/`reconcile_switch_journals` の高レベル意味論
(temp_path 命名規則の確定・収束規則の適用) は `plugin/switch.py`
(Task 11) が本モジュールの上に積む。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

_TERMINAL_PHASES = frozenset({"decided", "reverted"})


def insert(conn: sqlite3.Connection, *, kind: str, approval_id: int, name: str,
          old_kind: str, old_target: str | None, temp_path: str,
          new_target: str, switch_required: bool, actor: str,
          now: datetime, commit: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO plugin_switch_journal (kind, approval_id, name, "
        "old_kind, old_target, temp_path, new_target, switch_required, "
        "phase, actor, updated_at) VALUES (?,?,?,?,?,?,?,?, 'preparing', ?,?)",
        (kind, approval_id, name, old_kind, old_target, temp_path, new_target,
         1 if switch_required else 0, actor, now.isoformat()))
    if commit:
        conn.commit()
    return cur.lastrowid


def get(conn: sqlite3.Connection, op_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM plugin_switch_journal WHERE op_id=?", (op_id,)).fetchone()
    return dict(row) if row is not None else None


def set_phase(conn: sqlite3.Connection, op_id: int, phase: str, *,
             now: datetime, commit: bool = True) -> bool:
    """戻り値: `True` = `op_id` に該当する行を更新した。`False` = 該当行
    が存在しなかった (rowcount=0、fail-open 防止)。"""
    cur = conn.execute(
        "UPDATE plugin_switch_journal SET phase=?, updated_at=? WHERE op_id=?",
        (phase, now.isoformat(), op_id))
    if commit:
        conn.commit()
    return cur.rowcount == 1


def _non_terminal_placeholders() -> str:
    return ",".join("?" * len(_TERMINAL_PHASES))


def get_open_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM plugin_switch_journal WHERE name=? "
        f"AND phase NOT IN ({_non_terminal_placeholders()})",
        (name, *_TERMINAL_PHASES)).fetchone()
    return dict(row) if row is not None else None


def list_non_terminal(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM plugin_switch_journal WHERE phase NOT IN "
        f"({_non_terminal_placeholders()}) ORDER BY op_id",
        tuple(_TERMINAL_PHASES))]


def set_temp_path(conn: sqlite3.Connection, op_id: int, temp_path: str) -> None:
    """確定した op_id から導出した temp_path で UPDATE する (Task 11 が追加)。

    begin_switch_journal は insert() で op_id を先に割り当て、その直後に
    この関数で正しい temp_path 値を UPDATE する 2 段構成。
    """
    conn.execute(
        "UPDATE plugin_switch_journal SET temp_path=? WHERE op_id=?",
        (temp_path, op_id))
