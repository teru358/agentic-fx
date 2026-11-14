"""SQLite accounting primitives for PaperBroker HWM rebases.

Callers own eligibility, state locking and preview value verification. The
mutation boundary accepts those checked values and commits the replacement
snapshot and immutable audit row together.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Callable

from agentic_fx.core.accounting import daily_start_equity
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.config import Settings
from agentic_fx.core.contracts import Clock


class RebaseOutcomeUnknown(RuntimeError):
    """COMMIT raised; callers must inspect the ledger to resolve the outcome."""


def read_rebase_totals(conn: sqlite3.Connection) -> dict[str, float | int | None]:
    """Read ledger SUM/COUNT and last id, joining an existing read transaction."""
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(cut),0) AS cut_sum, "
            "COALESCE(SUM(cut_pct),0) AS cut_pct_sum, COUNT(*) AS count, "
            "MAX(id) AS last_id FROM kill_switch_hwm_rebases").fetchone()
        result = {"cut": row["cut_sum"], "cut_pct": row["cut_pct_sum"],
                  "count": row["count"], "last_id": row["last_id"]}
        if owns_transaction:
            conn.commit()
        return result
    except BaseException:
        if owns_transaction:
            conn.rollback()
        raise


def _commit_rebase(conn: sqlite3.Connection) -> None:
    """Isolated commit boundary, intentionally replaceable for fault injection."""
    conn.commit()


def paper_equity(conn: sqlite3.Connection, settings: Settings,
                 clock: Clock) -> tuple[float, float]:
    """Evaluate the configured PaperBroker on the supplied ops connection."""
    return PaperBroker(conn, settings, clock).equity()


def apply_rebase(
        conn: sqlite3.Connection, *, source_snapshot_id: int,
        accepted_audit_id: int, applied_at: datetime, generation: int,
        latched_at: str | None, digest_version: int, preview_token: str,
        hwm_before: float, equity: float, balance: float,
        drawdown_pct: float, threshold_pct: float, cut: float,
        cut_pct: float, additional_headroom: float,
        now: datetime,
        validate: Callable[[sqlite3.Connection], None]) -> dict[str, Any]:
    """Atomically insert a rebase snapshot and its ledger row.

    ``validate`` rechecks caller-owned broker/source/flat/freshness, generation,
    DD threshold and token conditions while BEGIN IMMEDIATE is held. Exceptions
    roll back without writing. This function also checks source/latest and daily
    invariants before committing.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        validate(conn)
        source = conn.execute(
            "SELECT * FROM account_snapshots WHERE id=?",
            (source_snapshot_id,)).fetchone()
        latest = conn.execute(
            "SELECT * FROM account_snapshots ORDER BY ts DESC,id DESC LIMIT 1"
        ).fetchone()
        if source is None or latest is None or latest["id"] != source_snapshot_id:
            raise ValueError("source snapshot changed")
        if (source["balance"] != balance or source["equity"] != equity
                or source["hwm"] != hwm_before or source["cashflow"] != 0):
            raise ValueError("source account values changed")

        daily_before = daily_start_equity(conn, now)
        if daily_before is None:
            raise ValueError("daily reference unavailable")
        cur = conn.execute(
            "INSERT INTO account_snapshots "
            "(ts,balance,equity,hwm,cashflow,source) VALUES (?,?,?,?,?,?)",
            (source["ts"], source["balance"], source["equity"],
             source["equity"], 0.0, "manual_ks_rebase"))
        rebase_snapshot_id = cur.lastrowid
        ledger_cur = conn.execute(
            "INSERT INTO kill_switch_hwm_rebases "
            "(applied_at,generation,latched_at,source_snapshot_id,"
            "rebase_snapshot_id,accepted_audit_id,digest_version,preview_token,"
            "hwm_before,hwm_after,equity,balance,drawdown_pct,threshold_pct,"
            "cut,cut_pct,additional_headroom) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (applied_at.isoformat(), generation, latched_at, source_snapshot_id,
             rebase_snapshot_id, accepted_audit_id, digest_version, preview_token,
             hwm_before, equity, equity, balance, drawdown_pct, threshold_pct,
             cut, cut_pct, additional_headroom))

        after = conn.execute(
            "SELECT * FROM account_snapshots ORDER BY ts DESC,id DESC LIMIT 1"
        ).fetchone()
        daily_after = daily_start_equity(conn, now)
        if (after["id"] != rebase_snapshot_id or after["ts"] != source["ts"]
                or after["equity"] != after["hwm"]
                or daily_after != daily_before):
            raise RuntimeError("rebase accounting postcondition failed")
        result = {"rebase_id": ledger_cur.lastrowid,
            "snapshot_id": rebase_snapshot_id,
            "generation": generation, "latched": latched_at is not None}
        try:
            _commit_rebase(conn)
        except BaseException as exc:
            try:
                conn.rollback()
            except BaseException:
                pass
            raise RebaseOutcomeUnknown("rebase COMMIT outcome is unknown") from exc
        return result
    except BaseException:
        try:
            conn.rollback()
        except BaseException:
            pass
        raise
