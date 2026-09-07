"""approval_requests CRUD。決定は冪等 (pending 以外への decide は拒否) — 設計書 §7。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime


class AlreadyDecidedError(Exception):
    """CAS 失敗 (approval は存在するが既に決定済み/期限切れ) の基底例外。"""
    pass


class ApprovalNotFoundError(AlreadyDecidedError):
    """E1 裁定 (2026-08-25): approval_id がそもそも存在しない場合の細分化した
    例外。`AlreadyDecidedError` のサブクラスにすることで、既存の broad
    `except AlreadyDecidedError` (commands.py 等) を変更せずに後方互換を
    保つ。"""
    pass


def create(conn: sqlite3.Connection, kind: str, payload: dict, now: datetime,
           expires_at: datetime | None = None, *, commit: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, expires_at, created_at) "
        "VALUES (?,?,?,?)",
        (kind, json.dumps(payload, ensure_ascii=False),
         expires_at.isoformat() if expires_at else None, now.isoformat()))
    if commit:
        conn.commit()
    return cur.lastrowid


def apply_decision(
        conn: sqlite3.Connection, approval_id: int, status: str, *,
        decided_by: str, now: datetime, reason: str | None = None,
        commit: bool = True) -> None:
    """§4.3 単一 API: approval 行の CAS + backlog 遷移を 1 tx で行う。

    m10: 許容 status は `approved|rejected|invalidated|expired` の 4 値。
    既存 `decide` (`approvals.py:26`) は `approved|rejected` の 2 値のみ。
    Task 8〜11 の間、同じ `approval_requests` テーブルに対して許容集合の
    異なる 2 つの決定 API (`decide`/`apply_decision`) が共存する (裁定1・
    申し送り③ — `decide` の削除は Task 11 の受入条件)。

    裁定1: CAS (`WHERE id=? AND status='pending'`) が rowcount=0 なら
    **副作用ゼロ**で `AlreadyDecidedError`。期限切れのフォールバック確定
    (旧 `decide` の 2 段 commit 挙動) はここでは行わない — 呼び出し元が
    決定 tx に入る前に `expire_due(commit=True)` を単独 tx で呼ぶ運用
    (骨格 裁定1 逐語)。
    CAS には既存 `decide` (`approvals.py:34`) と同じ `expires_at` 述語
    (`expires_at IS NULL OR expires_at >= ?`) を **`status in ("approved",
    "rejected")` のときだけ** 追加する (R9 — fail closed の維持: 期限切れ
    pending への approve/reject を拒否する)。`status in ("invalidated",
    "expired")` はクローズ系の遷移であり期限切れ行そのものを確定させる
    ための呼び出しなので、この述語を適用しない (適用すると
    `apply_decision(status='expired')` で「期限切れ pending を expired に
    する」こと自体ができなくなってしまう — Task 11 の
    `process_expired_approvals` が使う経路)。`expire_due` の既定
    `exclude_kinds=("plugin",)` により plugin kind は `expire_due` 側で
    expired 化されないため、この述語が唯一の「期限切れ pending への
    approve/reject を通さない」防御になる。
    """
    if status not in ("approved", "rejected", "invalidated", "expired"):
        raise ValueError(f"unsupported status for apply_decision: {status!r}")
    now_iso = now.isoformat()
    if status in ("approved", "rejected"):
        cur = conn.execute(
            "UPDATE approval_requests SET status=?, decided_by=?, decided_at=?, "
            "reason=? WHERE id=? AND status='pending' "
            "AND (expires_at IS NULL OR expires_at >= ?)",
            (status, decided_by, now_iso, reason, approval_id, now_iso))
    else:
        cur = conn.execute(
            "UPDATE approval_requests SET status=?, decided_by=?, decided_at=?, "
            "reason=? WHERE id=? AND status='pending'",
            (status, decided_by, now_iso, reason, approval_id))
    if cur.rowcount == 0:
        # m3: 副作用ゼロを謳うため commit せずに raise する (呼び出し元の
        # 同一 tx で先に書いた行を巻き込んで確定させない)。
        # E1 裁定: 「ID 不存在」と「CAS 失敗 (決定済み/期限切れ)」を区別する。
        exists = conn.execute(
            "SELECT 1 FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
        if exists is None:
            raise ApprovalNotFoundError(f"approval {approval_id} not found")
        raise AlreadyDecidedError(f"approval {approval_id} is not pending")
    row = conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    try:
        payload = json.loads(row["payload_json"]) if row is not None else {}
    except (TypeError, ValueError):
        # I4 是正で §5.5 migration がこの API を経由するようになった
        # ため、legacy 行の壊れた payload_json (json.loads 失敗) も
        # fail-safe に扱う必要がある — db.py の legacy migration と
        # 同じ流儀 (payload = {} フォールバック、backlog_id=None → no-op)。
        payload = {}
    backlog_id = payload.get("backlog_id")
    outcome = status  # m13: 旧 `status if status in (...) else status` は恒真式だったため簡約
    from agentic_fx.store import backlog as backlog_mod
    backlog_mod.apply_approval_outcome(
        conn, backlog_id=backlog_id, outcome=outcome,
        reason=(str(approval_id) if status == "approved"
               else (reason or "")),
        now=now, commit=False)
    # switch ジャーナルの 'decided' 化は approve のときのみ、Task 11 が
    # plugin/switch.py から呼ぶ拡張点 (ここでは何もしない — 申し送り⑤)。
    if commit:
        conn.commit()


def pending(conn: sqlite3.Connection, kind: str | None = None) -> list[dict]:
    q = "SELECT * FROM approval_requests WHERE status='pending'"
    args: list = []
    if kind:
        q += " AND kind=?"
        args.append(kind)
    return [dict(r) for r in conn.execute(q + " ORDER BY id", args)]


def expire_due(conn: sqlite3.Connection, now: datetime, *,
               exclude_kinds: tuple[str, ...] = ("plugin",),
               commit: bool = True) -> int:
    """行を 1 件ずつ列挙して処理する版 (裁定1: 呼び出し元 tx に混ぜられる形)。

    R-i8 (統合裁定、E 束と API 合意済み): `exclude_kinds` に含まれる kind の
    期限到来 pending は**直接 expired 化しない** (既定は `("plugin",)` —
    fail-safe 側)。plugin kind の expired 化は name ごとの plugin flock 下で
    「未完ジャーナル無し」を確認してから `apply_decision(status='expired')`
    を呼ぶ Task 11 の `process_expired_approvals(conn, *, plugins_root, now)`
    の責務 (未完ジャーナルがある行はスキップし次回再試行する)。対象行の
    列挙は下記 `list_due_for_expiry` を使う。非 plugin kind
    (`tech_plugin`/`live_trade`。`news_source` は 2026-09-07 に経路廃止) は従来どおりここで直接
    expired 化してよい (§4.3 の対象外・switch ジャーナルと無関係)。
    戻り値は「このコールで直接 expired 化した件数」(除外 kind の分は
    含まない)。"""
    now_iso = now.isoformat()
    rows = conn.execute(
        "SELECT id, kind FROM approval_requests WHERE status='pending' "
        "AND expires_at IS NOT NULL AND expires_at < ?", (now_iso,)).fetchall()
    expired_count = 0
    for row in rows:
        if row["kind"] in exclude_kinds:
            continue  # 列挙のみ — Task 11 の process_expired_approvals が扱う
        conn.execute("UPDATE approval_requests SET status='expired' WHERE id=?",
                     (row["id"],))
        expired_count += 1
    if commit:
        conn.commit()
    return expired_count


def list_due_for_expiry(conn: sqlite3.Connection, *, now: datetime,
                        kind: str | None = None) -> list[sqlite3.Row]:
    """期限到来 pending 行の**列挙のみ変種** (状態を変えない SELECT)。
    Task 11 の `process_expired_approvals` が `kind="plugin"` で使う
    (統合裁定 R-i8、E 束と API 合意済み)。"""
    now_iso = now.isoformat()
    sql = ("SELECT * FROM approval_requests WHERE status='pending' "
           "AND expires_at IS NOT NULL AND expires_at < ?")
    params: tuple = (now_iso,)
    if kind is not None:
        sql += " AND kind=?"
        params = (now_iso, kind)
    return conn.execute(sql, params).fetchall()


def set_reason(conn: sqlite3.Connection, approval_id: int, reason: str, *,
               commit: bool = True) -> None:
    """pending 行の reason 列を厳密一致の文字列で更新する (B-6、gc_roots ②
    /11e の `assert "legacy_plain_present" in (row["reason"] or "")` /
    M2 変異と三者を完全一致で揃える契約)。"""
    conn.execute(
        "UPDATE approval_requests SET reason=? WHERE id=? AND status='pending'",
        (reason, approval_id))
    if commit:
        conn.commit()
