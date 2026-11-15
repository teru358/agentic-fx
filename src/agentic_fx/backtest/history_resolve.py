"""履歴の衝突記録の人による解決 (停止中の local 操作)。

`keep_existing` は既存値を維持する判断を記録するだけで、履歴も epoch も変えない。
`replace_with_incoming` は衝突の incoming 値で既存の足を置き換える。daemon 停止中 (呼出し側が
instance lock を保持) に限り、backup を取り、epoch と既存値の digest を照合 (CAS) し、
履歴の変更・revision の追記・解決の監査を 1 つの transaction で確定する。
置換は global epoch を変えるため、旧 epoch に結びついた pending の承認・rollback は
照合不一致になり得る (売買・配備は自動では止まらない)。
live 比較由来の記録は置換できない。incoming への置換は、その衝突が検査を通過した MT5 取得 (sync) で
最後に観測されたと証明できる (`bridge_verified_run_id` が同 source・足幅の同期 run を指す) ときだけ許す。
証明のない記録 (手動 import・旧い記録・未検証の再観測) は拒否し、keep は許す。理由 (自由文) は人間専用でログ・event へ出さない。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentic_fx.backtest import time_repair
from agentic_fx.core.history_epoch import history_epoch
from agentic_fx.store import history_conflicts
from agentic_fx.store.history_lease import HistoryLeaseBusy, history_mutation_lease

ACTIONS = ("keep-existing", "replace-with-incoming")
ACTOR = "human:cli"
REASON_MAX = 500


class ResolveError(Exception):
    """解決を拒否・失敗した。code は固定語、rc は終了コード (引数不備 2、状態・競合 1)。"""

    def __init__(self, code: str, rc: int = 1, hint: str | None = None):
        super().__init__(code)
        self.code = code
        self.rc = rc
        self.hint = hint


UNVERIFIED_HINT = (
    "この衝突は、検査を通過した MT5 取得 (afx history sync) として再観測されていないため、"
    "incoming への置換はできません。`afx history sync` で同じ足を再観測すれば証明が付きます。"
    "同期の取得範囲外の足は後続の対応が必要です。既存値の維持 (keep-existing) は選べます。")


@dataclass(frozen=True)
class Resolved:
    conflict_id: int
    action: str
    state: str
    changed: bool
    revision_id: int | None = None
    backup: str | None = None
    epoch_before: str | None = None
    epoch_after: str | None = None


_VALUE_COLUMNS = ("open", "high", "low", "close", "volume", "spread")


def _clean_reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if not text:
        raise ResolveError("reason_required", 2)
    if len(text) > REASON_MAX or any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        raise ResolveError("reason_invalid", 2)
    return text


def _bridge_verified(conn, row) -> bool:
    """証明の run が、この衝突と同じ source・足幅の同期 run として残っているか。"""
    run_id = row["bridge_verified_run_id"]
    if not run_id or row["source"] != "mt5" or row["last_origin"] != "sync":
        return False
    run = conn.execute("SELECT source, interval FROM history_sync_runs WHERE id=?",
                       (run_id,)).fetchone()
    return run is not None and run["source"] == row["source"] and run["interval"] == row["interval"]


def _row(conn, conflict_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM history_conflicts WHERE id=?", (conflict_id,)).fetchone()
    if row is None:
        raise ResolveError("conflict_not_found", 2)
    return row


def resolve(conn: sqlite3.Connection, db_path: Path, *, conflict_id: int, action: str,
            reason: str | None, now: datetime | None = None, hook=None) -> Resolved:
    """`conn` は instance lock 保持下の接続 (transaction の外)。"""
    if action not in ACTIONS:
        raise ResolveError("action_invalid", 2)
    text = _clean_reason(reason)
    if conn.in_transaction:
        raise ValueError("resolve: connection must not be inside a transaction")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    first = _row(conn, conflict_id)
    if action == "keep-existing":
        return _keep(conn, first, text, stamp)
    return _replace(conn, db_path, first, text, stamp, hook)


def _keep(conn, first, reason: str, stamp: str) -> Resolved:
    conflict_id = first["id"]
    try:
        with history_mutation_lease(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = _row(conn, conflict_id)
                if row["state"] == "kept":
                    conn.rollback()
                    return Resolved(conflict_id, "keep-existing", "kept", False)
                if row["state"] != "open":
                    raise ResolveError("conflict_closed")
                conn.execute(
                    "UPDATE history_conflicts SET state='kept', resolved_at=?, resolved_by=?, "
                    "resolution_action='keep_existing', reason_ref=? "
                    "WHERE id=? AND state='open'", (stamp, ACTOR, reason, conflict_id))
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
    except HistoryLeaseBusy as exc:
        raise ResolveError("database_busy") from exc
    return Resolved(conflict_id, "keep-existing", "kept", True)


def _replace(conn, db_path: Path, first, reason: str, stamp: str, hook) -> Resolved:
    conflict_id = first["id"]
    if "live_compare" in (first["origin"], first["last_origin"]):
        raise ResolveError("replace_refused_live_compare")
    if first["state"] not in ("open", "kept"):
        raise ResolveError("conflict_closed")
    if not _bridge_verified(conn, first):
        raise ResolveError("replace_refused_unverified", hint=UNVERIFIED_HINT)
    if shutil.disk_usage(db_path.parent).free < db_path.stat().st_size * 2:
        raise ResolveError("insufficient_free_space")
    try:
        with history_mutation_lease(conn):
            backup = db_path.with_name(
                f"{db_path.name}.bak-history-replace-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
                f"-{conflict_id}")
            try:
                time_repair._backup_database(conn, backup)
            except Exception as exc:
                raise ResolveError("backup_failed") from exc
            if hook is not None:
                hook("after_backup")
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = _row(conn, conflict_id)
                if row["state"] not in ("open", "kept"):
                    raise ResolveError("conflict_closed")
                if not _bridge_verified(conn, row):
                    raise ResolveError("replace_refused_unverified", hint=UNVERIFIED_HINT)
                epoch_before = history_epoch(conn)
                if row["history_epoch"] != epoch_before:
                    raise ResolveError("epoch_changed")
                current = conn.execute(
                    "SELECT open, high, low, close, volume, spread FROM ohlcv_history "
                    "WHERE symbol=? AND interval=? AND source=? AND bar_time=?",
                    (row["symbol"], row["interval"], row["source"], row["bar_time"])
                ).fetchone()
                if current is None:
                    raise ResolveError("history_row_missing")
                _json, current_digest = history_conflicts.canonical_values(tuple(current))
                if current_digest != row["existing_digest"]:
                    raise ResolveError("existing_changed")
                incoming = json.loads(row["incoming_json"])
                values = tuple(incoming[name] for name in _VALUE_COLUMNS)
                cur = conn.execute(
                    "INSERT INTO history_revisions (kind, source, interval, symbol, bar_time, "
                    "history_epoch, conflict_id, actor, created_at, reason_ref) "
                    "VALUES ('replace_with_incoming',?,?,?,?,?,?,?,?,?)",
                    (row["source"], row["interval"], row["symbol"], row["bar_time"],
                     epoch_before, conflict_id, ACTOR, stamp, reason))
                revision_id = int(cur.lastrowid)
                if hook is not None:
                    hook("after_revision")
                updated = conn.execute(
                    "UPDATE ohlcv_history SET open=?, high=?, low=?, close=?, volume=?, "
                    "spread=? WHERE symbol=? AND interval=? AND source=? AND bar_time=?",
                    (*values, row["symbol"], row["interval"], row["source"], row["bar_time"]))
                if updated.rowcount != 1:
                    raise ResolveError("history_update_failed")
                conn.execute(
                    "UPDATE history_conflicts SET state='replaced', resolved_at=?, "
                    "resolved_by=?, resolution_action='replace_with_incoming', reason_ref=?, "
                    "revision_id=? WHERE id=?", (stamp, ACTOR, reason, str(revision_id),
                                                 conflict_id))
                conn.execute(
                    "UPDATE history_conflicts SET state='superseded', resolved_at=?, "
                    "resolved_by=?, resolution_action='supersede', revision_id=? "
                    "WHERE source=? AND interval=? AND symbol=? AND bar_time=? AND id<>? "
                    "AND state='open'", (stamp, ACTOR, str(revision_id), row["source"],
                                          row["interval"], row["symbol"], row["bar_time"],
                                          conflict_id))
                if hook is not None:
                    hook("before_commit")
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
            epoch_after = history_epoch(conn)
    except HistoryLeaseBusy as exc:
        raise ResolveError("database_busy") from exc
    return Resolved(conflict_id, "replace-with-incoming", "replaced", True, revision_id,
                    str(backup), epoch_before, epoch_after)
