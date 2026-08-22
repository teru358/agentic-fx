"""improve_waves / improve_wave_slots CRUD (設計書 §3.1、§8.1-18/19/20)。

**この層が保証する範囲**: wave/slot の SQL レベル状態遷移 (CAS)。`go` の
3-way プロトコル・実プロセス spawn は `core/improve_supervisor.py`
(Task 9) の責務。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

_TERMINAL_STATUSES = frozenset({"done", "failed"})


def create_wave_and_slots(conn: sqlite3.Connection, *, period_key: str,
                          now: datetime, expected: int,
                          commit: bool = True) -> bool:
    """1 つの短い tx で wave 行 + slot 行 (reserved, k=0..expected-1) を
    作る。`expected=0` は何も書かない。戻り値は「この呼び出しが起動権を
        return False
    now_iso = now.isoformat()
    cur = conn.execute(
        "INSERT OR IGNORE INTO improve_waves (period_key, created_at, expected) "
        "VALUES (?,?,?)", (period_key, now_iso, expected))
    if cur.rowcount == 0:
        if commit:
            conn.commit()
        return False
    for k in range(expected):
        conn.execute(
            "INSERT INTO improve_wave_slots (wave_period_key, k, status, "
            "created_at, updated_at) VALUES (?,?, 'reserved', ?, ?)",
            (period_key, k, now_iso, now_iso))
    if commit:
        conn.commit()
    return True


def list_slots(conn: sqlite3.Connection, *, period_key: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM improve_wave_slots WHERE wave_period_key=? ORDER BY k",
        (period_key,))]


def get_slot(conn: sqlite3.Connection, *, period_key: str, k: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM improve_wave_slots WHERE wave_period_key=? AND k=?",
        (period_key, k)).fetchone()
    return dict(row) if row is not None else None


def claim_slot(conn: sqlite3.Connection, *, period_key: str, k: int,
               mission_id: int, now: datetime, commit: bool = True) -> bool:
    """Tx-0 と同じ tx で呼ばれる CAS: reserved -> claimed。
    `spawn_attempts` を claim ごとに +1 する。"""
    cur = conn.execute(
        "UPDATE improve_wave_slots SET status='claimed', mission_id=?, "
        "spawn_attempts=spawn_attempts+1, updated_at=? "
        "WHERE wave_period_key=? AND k=? AND status='reserved'",
        (mission_id, now.isoformat(), period_key, k))
    if commit:
        conn.commit()
    return cur.rowcount == 1


def mark_running(conn: sqlite3.Connection, *, period_key: str, k: int,
                 now: datetime, commit: bool = True) -> bool:
    cur = conn.execute(
        "UPDATE improve_wave_slots SET status='running', updated_at=? "
        "WHERE wave_period_key=? AND k=? AND status='claimed'",
        (now.isoformat(), period_key, k))
    if commit:
        conn.commit()
    return cur.rowcount == 1


def revert_to_reserved(conn: sqlite3.Connection, *, period_key: str, k: int,
                       now: datetime, commit: bool = True) -> bool:
    """pre-ready 失敗の巻き戻し (CAS: claimed -> reserved、同じ tx で
    mission_id=NULL も戻す)。呼び出し元 (Task 9) が再試行上限未達と判定
    した場合にのみ呼ぶ。戻り値は CAS 成功可否。"""
    cur = conn.execute(
        "UPDATE improve_wave_slots SET status='reserved', mission_id=NULL, "
        "updated_at=? WHERE wave_period_key=? AND k=? AND status='claimed'",
        (now.isoformat(), period_key, k))
    if commit:
        conn.commit()
    return cur.rowcount == 1


def mark_slot_failed(conn: sqlite3.Connection, *, period_key: str, k: int,
                     now: datetime, commit: bool = True) -> None:
    """再試行上限に達した slot を無条件で failed へ落とす (mission_id も
    NULL に戻す)。呼び出し元 (Task 9) が再試行上限到達と判定した場合に呼ぶ。"""
    conn.execute(
        "UPDATE improve_wave_slots SET status='failed', mission_id=NULL, "
        "updated_at=? WHERE wave_period_key=? AND k=?",
        (now.isoformat(), period_key, k))
    if commit:
        conn.commit()


def count_open_slots(conn: sqlite3.Connection, *, period_key: str) -> int:
    """`reserved` 状態の slot 数を返す (§8.1-19 の空き数照会)。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM improve_wave_slots "
        "WHERE wave_period_key=? AND status='reserved'", (period_key,)).fetchone()
    return row["n"]


def mark_terminal(conn: sqlite3.Connection, *, period_key: str, k: int,
                  status: str, now: datetime, commit: bool = True) -> None:
    if status not in _TERMINAL_STATUSES:
        raise ValueError(f"status must be done|failed: {status!r}")
    conn.execute(
        "UPDATE improve_wave_slots SET status=?, updated_at=? "
        "WHERE wave_period_key=? AND k=?", (status, now.isoformat(), period_key, k))
    if commit:
        conn.commit()


def recover_stale_slots(conn: sqlite3.Connection, *, now: datetime,
                        commit: bool = True) -> int:
    """§8.1-20: 起動時、`reserved`/`claimed`/`running` の全 slot を `failed`
    へ収束する (再開しない。period は消費済みのまま — wave 行は残す)。
    手動 one-shot は slot を持たないため対象外 (このクエリの対象は
    improve_wave_slots のみ)。"""
    cur = conn.execute(
        "UPDATE improve_wave_slots SET status='failed', updated_at=? "
        "WHERE status IN ('reserved','claimed','running')", (now.isoformat(),))
    if commit:
        conn.commit()
    return cur.rowcount
