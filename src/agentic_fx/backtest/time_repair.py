"""One-off repair for the DST-mislabelled MT5 history.

History imports are immutable by contract.  This module is the sole exception:
it changes only the fingerprinted rows after a dry-run plan has been explicitly
confirmed, and records an idempotency marker in the same transaction.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPAIR_ID = "time_repair_dst_v1"


class RepairError(RuntimeError):
    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class HistoryFingerprint:
    start: str
    end: str
    counts: dict[str, int]
    key_sha256: str
    value_sha256: str


@dataclass(frozen=True)
class CacheSet:
    name: str
    interval: str
    start: str
    end: str
    count: int
    key_sha256: str
    value_sha256: str


HISTORY_FINGERPRINT = HistoryFingerprint(
    "2025-11-02T21:00:00+00:00", "2026-03-06T20:55:00+00:00",
    {"EURUSD": 25344, "USDJPY": 25344, "JP225": 23906, "US500": 23923,
     "USOIL": 23987, "XAUUSD": 24128},
    "08ce136a3d8c2f98ccae9e039b2a9566559f3a0c5d414b5c57c5b4292ee6fddf",
    "9df5d76f50417d0672c61b7c730d88a2c29657ba7ffa345180f3293c2d8f242b")
CACHE_SETS: tuple[CacheSet, ...] = ()


def _rows_digest(rows, *, values: bool) -> str:
    lines = []
    for row in rows:
        line = "|".join(str(row[name]) for name in ("symbol", "interval", "bar_time", "source"))
        if values:
            line += "|" + "|".join(repr(float(row[name])) for name in
                                      ("open", "high", "low", "close", "volume"))
        lines.append(line)
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def plan_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _history_rows(conn: sqlite3.Connection):
    fp = HISTORY_FINGERPRINT
    rows = conn.execute(
        "SELECT * FROM ohlcv_history WHERE source='mt5' AND bar_time>=? AND bar_time<=? "
        "ORDER BY symbol, interval, bar_time", (fp.start, fp.end)).fetchall()
    wrong_interval = conn.execute(
        "SELECT 1 FROM ohlcv_history WHERE source='mt5' AND bar_time>=? AND bar_time<=? "
        "AND interval!='5m' LIMIT 1", (fp.start, fp.end)).fetchone()
    counts = {r["symbol"]: 0 for r in rows}
    for row in rows:
        counts[row["symbol"]] += 1
    if wrong_interval or counts != fp.counts or _rows_digest(rows, values=False) != fp.key_sha256 \
            or _rows_digest(rows, values=True) != fp.value_sha256:
        raise RepairError("unknown history fingerprint", 3)
    return rows


def _cache_status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    answer = []
    for item in CACHE_SETS:
        rows = conn.execute("SELECT * FROM ohlcv_cache WHERE source='mt5-live' AND symbol='USDJPY' "
                            "AND interval=? AND bar_time>=? AND bar_time<=? ORDER BY bar_time",
                            (item.interval, item.start, item.end)).fetchall()
        if not rows:
            oldest = conn.execute("SELECT min(bar_time) FROM ohlcv_cache WHERE source='mt5-live' "
                                  "AND interval=?", (item.interval,)).fetchone()[0]
            if oldest is not None and oldest > item.end:
                answer.append({"name": item.name, "status": "absent_before_cache_window"})
                continue
            raise RepairError(f"cache set {item.name} is unexpectedly absent", 3)
        if len(rows) != item.count or _rows_digest(rows, values=False) != item.key_sha256 \
                or _rows_digest(rows, values=True) != item.value_sha256:
            raise RepairError(f"cache set {item.name} differs", 3)
        answer.append({"name": item.name, "status": "present", "key_sha256": item.key_sha256,
                       "value_sha256": item.value_sha256})
    return answer


def build_plan(conn: sqlite3.Connection, *, indicator_pending: str) -> dict[str, Any]:
    """Inspect only; no mutation is permitted in this dry-run path."""
    if indicator_pending not in {"keep", "invalidate"}:
        raise RepairError("--indicator-pending is required", 2)
    rows = _history_rows(conn)
    cache_sets = _cache_status(conn)
    approvals = []
    for row in conn.execute("SELECT id, payload_json FROM approval_requests WHERE status='pending'"):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        is_strategy = payload.get("eval_source") is not None
        is_indicator = not is_strategy
        if is_strategy or (indicator_pending == "invalidate" and is_indicator):
            approvals.append({"id": row["id"], "status": "invalidated"})
    targets = {"H1": {"count": len(rows), "key_sha256": _rows_digest(rows, values=False),
                       "value_sha256": _rows_digest(rows, values=True), "shift_seconds": 3600},
               "C1": {"delete": 0}, "C2": {"delete": 0}, "C3": {"delete": 0, "replace": 0}}
    plan: dict[str, Any] = {"schema": "afx.time_repair.plan/1", "repair_id": REPAIR_ID,
        "created_at": datetime.now(timezone.utc).isoformat(), "fingerprint": {"id": REPAIR_ID,
        "key_sha256": HISTORY_FINGERPRINT.key_sha256, "value_sha256": HISTORY_FINGERPRINT.value_sha256},
        "cache_sets": cache_sets, "anchors": [], "excluded_weeks": [], "bridge_check": None,
        "targets": targets, "samples": [], "collisions": 0, "unexplained": 0,
        "indicator_pending": indicator_pending, "approval_changes": approvals, "estimate_sec": 0}
    plan["plan_hash"] = plan_hash(plan)
    return plan


def _shift(iso: str) -> str:
    return (datetime.fromisoformat(iso) + timedelta(hours=1)).isoformat()


def apply_plan(conn: sqlite3.Connection, db_path: Path, plan: dict[str, Any], *, confirm: str) -> None:
    expected = plan.get("plan_hash")
    payload = {key: value for key, value in plan.items() if key != "plan_hash"}
    if not isinstance(expected, str) or confirm != expected or plan_hash(payload) != expected:
        raise RepairError("plan confirmation does not match", 4)
    marked = conn.execute("SELECT 1 FROM data_migrations WHERE name=?", (REPAIR_ID,)).fetchone()
    if marked:
        return
    current = build_plan(conn, indicator_pending=plan["indicator_pending"])
    if current["targets"] != plan["targets"] or current["approval_changes"] != plan["approval_changes"]:
        raise RepairError("database differs from plan", 4)
    v0 = conn.execute("PRAGMA data_version").fetchone()[0]
    backup = db_path.with_name(f"{db_path.name}.bak-time-repair-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    destination = sqlite3.connect(backup)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA data_version").fetchone()[0] != v0:
            raise RepairError("database changed during apply", 4)
        rows = _history_rows(conn)
        for row in rows:
            conn.execute("UPDATE ohlcv_history SET bar_time=? WHERE symbol=? AND interval=? "
                         "AND bar_time=? AND source=?", (_shift(row["bar_time"]), row["symbol"],
                         row["interval"], row["bar_time"], row["source"]))
        for change in plan["approval_changes"]:
            from agentic_fx.store.approvals import apply_decision
            apply_decision(conn, change["id"], "invalidated", decided_by="system:time_repair",
                           now=datetime.now(timezone.utc), reason="history_time_repaired", commit=False)
        conn.execute("INSERT INTO data_migrations(name, applied_at) VALUES (?, ?)",
                     (REPAIR_ID, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

