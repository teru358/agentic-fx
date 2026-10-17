"""Startup reconcile of durable deployment operations; intentionally not a tools API."""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agentic_fx.activity import Category
from agentic_fx.plugin import deploy_ops
from agentic_fx.plugin import deploy_rollback
from agentic_fx.plugin.deploy_deps import enumerate_dependents
from agentic_fx.plugin.deploy_disable import _activity
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store import plugin_switch_journal as journal_store
from agentic_fx.store.db import OPEN_PHASE_SQL

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileResult:
    name: str
    op_id: int
    op_kind: str
    outcome: str
    fail_reason: str | None = None


def rename_plain_dir(live: Path, retired: Path) -> None:
    """The single filesystem call that retires a plain plugin directory."""
    os.rename(live, retired)


def _set(conn: sqlite3.Connection, row: dict, phase: str, activity, *, now: datetime,
         fail_reason: str | None = None) -> ReconcileResult:
    op_id = int(row["op_id"])
    deploy_store.set_phase(conn, op_id, phase, now=now, fail_reason=fail_reason)
    deploy_ops.try_notify(conn, deploy_store.get_by_id(conn, op_id), _activity(activity), now=now)
    return ReconcileResult(row["name"], op_id, row["op_kind"], phase, fail_reason)


def _syscall_failed(conn, row, activity, *, now) -> ReconcileResult:
    return _set(conn, row, "needs_retry", activity, now=now, fail_reason="syscall_failed")


def _reconcile_disable(conn, plugins_root: Path, row: dict, activity, *, now) -> ReconcileResult:
    live = plugins_root / row["name"]
    if live.is_symlink():
        if os.readlink(live) != row["from_target"]:
            return _set(conn, row, "foreign", activity, now=now)
        try:
            if live.is_symlink():
                os.unlink(live)
        except OSError:
            return _syscall_failed(conn, row, activity, now=now)
        return _set(conn, row, "done", activity, now=now)
    if not os.path.lexists(live):
        return _set(conn, row, "done", activity, now=now)
    return _set(conn, row, "foreign", activity, now=now)


def _subject_kind(conn, plugins_root: Path, row: dict) -> str | None:
    artifact = row["subject_artifact_hash"]
    if row["from_kind"] == "symlink" and artifact:
        meta = deploy_rollback._discover_dir(
            plugins_root / ".versions" / row["name"] / artifact, row["name"])
        if meta is not None:
            return meta.kind
    return deploy_rollback.current_deployed_kind(conn, plugins_root, row["name"])


def _reconcile_rollback(conn, plugins_root: Path, row: dict, activity, *, settings,
                        now) -> ReconcileResult:
    name = row["name"]
    live = plugins_root / name
    to_target = row["to_target"]
    if live.is_symlink():
        raw = os.readlink(live)
        if raw == to_target:
            return _set(conn, row, "done", activity, now=now)
        at_from = row["from_kind"] == "symlink" and raw == row["from_target"]
        if not at_from:
            return _set(conn, row, "foreign", activity, now=now)
    elif os.path.lexists(live):
        return _set(conn, row, "foreign", activity, now=now)
    else:
        at_from = False
    force = bool(row["force"])
    verified, _reasons, _report = deploy_rollback._verify(
        conn, plugins_root, name, row["target_artifact_hash"], settings=settings, force=force,
        current_kind=_subject_kind(conn, plugins_root, row))
    if verified is not None and verified.to_target != to_target:
        verified = None
    if verified is None:
        if at_from:
            try:
                if live.is_symlink():
                    os.unlink(live)
            except OSError:
                return _syscall_failed(conn, row, activity, now=now)
        return _set(conn, row, "needs_retry", activity, now=now, fail_reason="verify_failed")
    if force:
        deploy_rollback.supplement_cascade(
            conn, plugins_root, int(row["op_id"]), verified.dependents.dependent_names, now=now)
    try:
        deploy_rollback.swap_to_target(plugins_root, name, int(row["op_id"]), to_target)
    except OSError:
        return _syscall_failed(conn, row, activity, now=now)
    return _set(conn, row, "done", activity, now=now)


def _reconcile_retire(conn, plugins_root: Path, row: dict, activity, *, now) -> ReconcileResult:
    live = plugins_root / row["name"]
    retired = plugins_root / row["retired_path"]
    if live.is_symlink():
        return _set(conn, row, "foreign", activity, now=now)
    if live.is_dir():
        if os.path.lexists(retired):
            return _set(conn, row, "foreign", activity, now=now)
        try:
            rename_plain_dir(live, retired)
        except OSError:
            return _syscall_failed(conn, row, activity, now=now)
        return _set(conn, row, "done", activity, now=now)
    if os.path.lexists(live):
        return _set(conn, row, "foreign", activity, now=now)
    if os.path.lexists(retired):
        return _set(conn, row, "done", activity, now=now)
    return _set(conn, row, "foreign", activity, now=now)


def _row_dependents(conn, plugins_root: Path, row: dict, settings) -> set[str]:
    if row["op_kind"] == "rollback_symlink" and row["target_artifact_hash"]:
        target_dir = plugins_root / ".versions" / row["name"] / row["target_artifact_hash"]
        return deploy_rollback.dependents_with_target(
            conn, plugins_root, row["name"], target_dir, settings=settings).dependent_names
    return enumerate_dependents(conn, plugins_root, row["name"], settings=settings).dependent_names


def _reconcile_one(conn, plugins_root: Path, row: dict, *, now, settings,
                   activity) -> ReconcileResult | None:
    name = row["name"]
    with deploy_ops.acquire_op_locks(
            plugins_root, name,
            enumerate_dependents=lambda: _row_dependents(conn, plugins_root, row, settings)):
        current = deploy_store.get_by_id(conn, int(row["op_id"]))
        if current is None or not deploy_store.is_open(current):
            return None
        journal = journal_store.get_open_by_name(conn, name)
        if journal is not None:
            _activity(activity).write(
                Category.APPROVAL, "plugin_deploy_op_conflict",
                f"op_id={current['op_id']} name={name} "
                f"switch_journal_op_id={journal['op_id']}: both unresolved, left untouched")
            return ReconcileResult(name, int(current["op_id"]), current["op_kind"], "conflict")
        if current["op_kind"] == "disable_symlink":
            return _reconcile_disable(conn, plugins_root, current, activity, now=now)
        if current["op_kind"] == "rollback_symlink":
            return _reconcile_rollback(conn, plugins_root, current, activity,
                                       settings=settings, now=now)
        if current["op_kind"] == "retire_plain":
            return _reconcile_retire(conn, plugins_root, current, activity, now=now)
        return None


def reconcile_deploy_ops(conn: sqlite3.Connection, plugins_root: Path, *, now: datetime,
                         settings, activity=None) -> list[ReconcileResult]:
    """Close every blocking/open deploy op once, then recover missed notifications."""
    results: list[ReconcileResult] = []
    open_rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM plugin_deploy_ops WHERE {OPEN_PHASE_SQL} ORDER BY name, op_id")]
    for row in open_rows:
        try:
            result = _reconcile_one(conn, plugins_root, row, now=now, settings=settings,
                                    activity=activity)
        except Exception as exc:
            _log.warning("deploy op reconcile failed op_id=%s name=%s: %r",
                         row["op_id"], row["name"], exc)
            result = ReconcileResult(row["name"], int(row["op_id"]), row["op_kind"], "error")
        if result is not None:
            results.append(result)
    pending = [dict(r) for r in conn.execute(
        "SELECT * FROM plugin_deploy_ops WHERE notify_attempted=0 "
        "AND phase IN ('done','needs_retry','foreign') ORDER BY op_id")]
    for row in pending:
        if deploy_ops.try_notify(conn, row, _activity(activity), now=now):
            results.append(ReconcileResult(
                row["name"], int(row["op_id"]), row["op_kind"], "notified", row["fail_reason"]))
    return results
