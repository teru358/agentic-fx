"""SQLite accounting primitives for PaperBroker HWM rebases.

Callers own eligibility, state locking and preview value verification. The
mutation boundary accepts those checked values and commits the replacement
snapshot and immutable audit row together.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from typing import Any, Callable

from agentic_fx.core.accounting import daily_start_equity
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.config import Settings
from agentic_fx.core.contracts import Clock
from agentic_fx.core.timeutil import as_utc


class RebaseOutcomeUnknown(RuntimeError):
    """COMMIT raised; callers must inspect the ledger to resolve the outcome."""


def read_rebase_totals(conn: sqlite3.Connection) -> dict[str, float | int | None]:
    """Read ledger SUM/COUNT and last id.

    With no open transaction this opens and closes its own read transaction.
    Otherwise it reads inside the caller's transaction as is, including rows
    that transaction has written but not committed; the caller owns which
    transaction that is.
    """
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(cut),0) AS cut_sum, "
            "COALESCE(SUM(cut_pct),0) AS cut_pct_sum, COUNT(*) AS count, "
            "MAX(id) AS last_id FROM kill_switch_hwm_rebases").fetchone()
        result = {"cut": row[0], "cut_pct": row[1],
                  "count": row[2], "last_id": row[3]}
        if owns_transaction:
            conn.commit()
        return result
    except BaseException:
        if owns_transaction:
            conn.rollback()
        raise


def _commit_rebase(conn: sqlite3.Connection) -> None:
    conn.commit()


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


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
    DD threshold and token conditions while BEGIN IMMEDIATE is held. It must
    only read: ending the transaction (commit / rollback) is rejected before
    anything is written. Its SQL authorizer allows only SELECT, READ, FUNCTION,
    and RECURSIVE actions and is removed even if validation raises. Exceptions
    roll back without writing. This function
    also checks source/latest and daily invariants before committing.

    An ``Exception`` from COMMIT becomes ``RebaseOutcomeUnknown`` (the caller
    resolves it from the ledger). A ``BaseException`` that is not an
    ``Exception`` (an interrupt) propagates unchanged. Before COMMIT completes,
    it triggers one best-effort rollback. A ``BaseException`` after COMMIT
    completes but before this function returns loses the result even though
    the rebase is durable; callers must resolve it from the ledger.
    """
    conn.execute("BEGIN IMMEDIATE")
    committed = False
    try:
        def read_only_authorizer(action, _arg1, _arg2, _database, _trigger):
            allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                       sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

        conn.set_authorizer(read_only_authorizer)
        try:
            validate(conn)
        finally:
            conn.set_authorizer(None)
        if not conn.in_transaction:
            raise RuntimeError("validate must not end the rebase transaction")
        numeric_values = (hwm_before, equity, balance, drawdown_pct,
                          threshold_pct, cut, cut_pct, additional_headroom)
        if not all(math.isfinite(value) for value in numeric_values):
            raise ValueError("rebase accounting values must be finite")
        if additional_headroom < 0:
            raise ValueError("additional headroom must be non-negative")
        if cut != hwm_before - equity:
            raise ValueError("cut does not match HWM and equity")
        if cut <= 0:
            raise ValueError("cut must be positive")
        if cut_pct != cut / hwm_before * 100:
            raise ValueError("cut percentage does not match cut and HWM")
        if drawdown_pct < threshold_pct:
            raise ValueError("drawdown is below threshold")
        applied_at = as_utc(applied_at)
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
        except Exception as exc:
            raise RebaseOutcomeUnknown("rebase COMMIT outcome is unknown") from exc
        committed = True
        return result
    except BaseException:
        if not committed:
            _rollback_quietly(conn)
        raise
