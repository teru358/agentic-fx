"""improvement_backlog CRUD + 状態機械 (設計書 §4.3、§8.1-27)。

status: open | observation | note | selected | done | rejected。`observation` は
「1 回の結果で課題を悪いと判断しない」(R8) の受け皿 — 失敗・却下・標本不足は
ここへ落ち、`list_open` (= open|observation) に残り続ける。

`improvement_backlog.status` に CHECK 制約は無い (`db.py` 現物) — 新規
status 値の追加に migration は不要 (設計書 §4.3 逐語)。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

_log = logging.getLogger("agentic_fx.store.backlog")


def upsert_system_note(
        conn: sqlite3.Connection, *, idea: str, last_result: str,
        now: datetime) -> int:
    """Insert or refresh one system note. Transaction ownership stays with caller."""
    idea_norm = idea.strip().lower()
    row = conn.execute(
        "SELECT id FROM improvement_backlog WHERE idea_norm=? "
        "AND status='note' AND source='system' ORDER BY id LIMIT 1",
        (idea_norm,)).fetchone()
    if row is not None:
        note_id = int(row["id"])
        conn.execute(
            "UPDATE improvement_backlog SET last_result=?, updated_at=? WHERE id=?",
            (last_result, now.isoformat(), note_id))
        return note_id
    cur = conn.execute(
        "INSERT INTO improvement_backlog "
        "(idea, source, status, created_at, updated_at, idea_norm, last_result) "
        "VALUES (?, 'system', 'note', ?, ?, ?, ?)",
        (idea, now.isoformat(), now.isoformat(), idea_norm, last_result))
    return int(cur.lastrowid)


def add(conn: sqlite3.Connection, idea: str, source: str, now: datetime) -> int:
    # round2 #8 是正 (2026-08-29、verified-round2.md #8): 正規化は
    # improve_loop._select_and_bind と同じ Python 側 1 箇所
    # (`idea.strip().lower()`) に閉じる — この CLI/手動追加経路でも
    # `idea_norm` を書いておかないと、discovery/selected 側の
    # `idea_norm=?` 重複検出がこの行を見つけられない。
    idea_norm = idea.strip().lower()
    cur = conn.execute(
        "INSERT INTO improvement_backlog (idea, source, created_at, updated_at, "
        "idea_norm) VALUES (?,?,?,?,?)",
        (idea, source, now.isoformat(), now.isoformat(), idea_norm))
    conn.commit()
    return cur.lastrowid


def list_open(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM improvement_backlog WHERE status IN ('open','observation') "
        "ORDER BY id")]


def list_notes(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM improvement_backlog WHERE status='note' ORDER BY id")]


def set_status(conn: sqlite3.Connection, backlog_id: int, status: str,
               now: datetime, *, last_result: str | None = None,
               commit: bool = True) -> bool:
    """m1: `last_result` は常に上書きする (既定 `None`)。`last_result` を
    渡さない呼び出しは既存の `last_result` を NULL でクリアする —
    `select_for_mission` (下記) は `last_result` に触れないため非対称
    (意図的: 「試行開始」は結果を持たないが「終端」は必ず結果を書く)。

    戻り値: `True` = `backlog_id` に該当する行を更新した。`False` = 該当行
    が存在しなかった (rowcount=0、fail-open 防止)。"""
    cur = conn.execute(
        "UPDATE improvement_backlog SET status=?, last_result=?, updated_at=? "
        "WHERE id=?", (status, last_result, now.isoformat(), backlog_id))
    if commit:
        conn.commit()
    return cur.rowcount == 1


def select_for_mission(conn: sqlite3.Connection, backlog_id: int, *,
                       now: datetime, commit: bool = True) -> bool:
    """§4.1 Tx-1・裁定7: `open|observation` からの CAS。rowcount=1 が
    このミッションを唯一の勝者にする。"""
    cur = conn.execute(
        "UPDATE improvement_backlog SET status='selected', "
        "attempts=attempts+1, updated_at=? "
        "WHERE id=? AND status IN ('open','observation')",
        (now.isoformat(), backlog_id))
    if commit:
        conn.commit()
    return cur.rowcount == 1


# §4.3 状態機械表: outcome -> (次 status, last_result テンプレート)。
# last_result テンプレートは `.format(reason=...)` で埋める (reason が
# None のものはそのまま定数)。
_OUTCOME_TABLE: dict[str, tuple[str, str]] = {
    "done": ("done", "report:{reason}"),
    "report_failed": ("observation", "report_failed:{reason}"),
    "report_publish_failed": ("observation", "report_failed:{reason}"),
    "gate_failed": ("observation", "gate_failed:{reason}"),
    "insufficient_trades": ("observation", "insufficient_trades:{reason}"),
    "unsupported_in_plan10": ("observation", "unsupported_in_plan10:{reason}"),
    "approved": ("done", "approved:{reason}"),
    "rejected": ("observation", "rejected:{reason}"),
    "expired": ("observation", "expired"),
    "invalidated": ("observation", "invalidated"),
    "mission_failed": ("observation", "mission_failed:{reason}"),
    "commit_failed": ("observation", "commit_failed"),
    "interrupted": ("observation", "interrupted"),
}


def apply_approval_outcome(
        conn: sqlite3.Connection, *, backlog_id: int | None, outcome: str,
        reason: str | None, now: datetime, commit: bool = True) -> None:
    """§4.3 の状態機械表に従って backlog.status + last_result を更新する。
    `backlog_id=None` (legacy payload・手動 wave の bind 前) は no-op。"""
    if backlog_id is None:
        if commit:
            conn.commit()
        return
    if outcome not in _OUTCOME_TABLE:
        raise ValueError(f"unknown outcome: {outcome!r}")
    status, template = _OUTCOME_TABLE[outcome]
    last_result = template.format(reason=reason) if "{reason}" in template else template
    # round2 #10 是正 (2026-08-29、verified-round2.md #10): `set_status` の
    # docstring は戻り値の意味を「True=更新した。False=該当行が存在
    # しなかった (fail-open 防止)」と自ら宣言しているが、従来ここでは
    # 戻り値を捨てていた — 兄弟の `missions.finish_improve_mission` は
    # `backlog_ok = backlog_mod.set_status(...)` を消費して RuntimeError
    # を raise する非対称があった (同一 store 内)。ここで raise すると
    # `apply_decision` (approvals.py) の 1 tx が rollback し、backlog 行を
    # 失った approval は approve/reject/expire のいずれもできず pending に
    # 固着する (#4 で「durable stuck」として認定した状態と同型を新設して
    # しまう) — 到達性は限定的 (backlog_id は payload_json 内で FK が無く、
    # 手動 DB 手術後にしか通常起きない) なので、raise せず「決定は止めない」
    # まま可視化のみ行う (fail closed にしたい場合は store 層ではなく
    # 呼び出し元だけが raise する形を取ること — expire/invalidate 経路は
    # raise させないこと)。
    ok = set_status(conn, backlog_id, status, now, last_result=last_result,
                    commit=commit)
    if not ok:
        _log.error(
            "apply_approval_outcome: improvement_backlog row %s not found "
            "— approval decision committed with a no-op backlog transition "
            "(audit trails have diverged)", backlog_id)
