"""Startup reconcile of durable deployment operations; intentionally not a tools API."""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agentic_fx.activity import Category
from agentic_fx.plugin import deploy_ops
from agentic_fx.plugin import deploy_rollback
from agentic_fx.plugin.deploy_deps import enumerate_dependents
from agentic_fx.plugin.switch import PluginBusyError
from agentic_fx.store import plugin_deploy_ops as deploy_store
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
    leaves_syscall_failure = (row["phase"] == "needs_retry"
                              and row["fail_reason"] == "syscall_failed"
                              and phase != "needs_retry")
    deploy_store.set_phase(conn, op_id, phase, now=now, fail_reason=fail_reason,
                           notify_attempted=False if leaves_syscall_failure else None)
    deploy_ops.try_notify(conn, deploy_store.get_by_id(conn, op_id), deploy_ops.activity_or_null(activity), now=now)
    return ReconcileResult(row["name"], op_id, row["op_kind"], phase, fail_reason)


def _syscall_failed(conn, row, activity, *, now) -> ReconcileResult:
    return _set(conn, row, "needs_retry", activity, now=now, fail_reason="syscall_failed")


def _readlink_or_none(live: Path) -> str | None:
    try:
        return os.readlink(live)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _reconcile_disable(conn, plugins_root: Path, row: dict, activity, *, now) -> ReconcileResult:
    live = plugins_root / row["name"]
    if live.is_symlink():
        try:
            raw = _readlink_or_none(live)
        except OSError:
            return _syscall_failed(conn, row, activity, now=now)
        if raw is None:
            return _set(conn, row, "done", activity, now=now)
        if raw != row["from_target"]:
            return _set(conn, row, "foreign", activity, now=now)
        try:
            os.unlink(live)
        except FileNotFoundError:
            pass
        except OSError:
            return _syscall_failed(conn, row, activity, now=now)
        return _set(conn, row, "done", activity, now=now)
    if not os.path.lexists(live):
        return _set(conn, row, "done", activity, now=now)
    return _set(conn, row, "foreign", activity, now=now)


def _subject_kind(conn, plugins_root: Path, row: dict) -> str | None:
    artifact = row["subject_artifact_hash"]
    if row["from_kind"] == "symlink" and artifact:
        meta = deploy_rollback.discover_version_dir(
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
        try:
            raw = _readlink_or_none(live)
        except OSError:
            return _syscall_failed(conn, row, activity, now=now)
        if raw is None:
            at_from = False
        else:
            if raw == to_target:
                return _set(conn, row, "done", activity, now=now)
            at_from = (row["from_kind"] == "symlink"
                       and deploy_ops.live_matches_from(live, row["from_target"]))
            if not at_from:
                return _set(conn, row, "foreign", activity, now=now)
    elif os.path.lexists(live):
        return _set(conn, row, "foreign", activity, now=now)
    else:
        at_from = False
    force = bool(row["force"])
    verified = deploy_rollback.verify_rollback_target(
        conn, plugins_root, name, row["target_artifact_hash"], settings=settings, force=force,
        current_kind=_subject_kind(conn, plugins_root, row))
    if (isinstance(verified, deploy_rollback.VerificationFailure)
            or verified.to_target != to_target):
        if at_from:
            try:
                os.unlink(live)
            except FileNotFoundError:
                pass
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
        current_identity = deploy_store.live_identity(plugins_root, row["name"])
        if (row["subject_content_hash"] is not None and (
                current_identity is None
                or current_identity.content_hash != row["subject_content_hash"])):
            return _set(conn, row, "foreign", activity, now=now)
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


def _reconcile_one(conn, plugins_root: Path, row: dict, *, now, settings, activity,
                   deadline: float, stop_event) -> ReconcileResult | None:
    name = row["name"]
    try:
        with deploy_ops.acquire_op_locks(
                plugins_root, name,
                enumerate_dependents=lambda: _row_dependents(conn, plugins_root, row, settings),
                deadline=deadline,
                stop_event=stop_event) as locked:
            current = deploy_store.get_by_id(conn, int(row["op_id"]))
            if current is None or not deploy_store.is_open(current):
                return None
            try:
                deploy_ops.check_can_start_op(conn, name, dependents=locked - {name})
            except deploy_ops.DeployOpBlockedError as exc:
                deploy_ops.activity_or_null(activity).write(
                    Category.APPROVAL, "plugin_deploy_op_conflict",
                    f"op_id={current['op_id']} name={name} {exc}: both unresolved, left untouched")
                return ReconcileResult(name, int(current["op_id"]), current["op_kind"], "conflict")
            if current["op_kind"] == "disable_symlink":
                return _reconcile_disable(conn, plugins_root, current, activity, now=now)
            if current["op_kind"] == "rollback_symlink":
                return _reconcile_rollback(conn, plugins_root, current, activity,
                                           settings=settings, now=now)
            if current["op_kind"] == "retire_plain":
                return _reconcile_retire(conn, plugins_root, current, activity, now=now)
            return None
    except PluginBusyError:
        deploy_ops.activity_or_null(activity).write(
            Category.APPROVAL, "plugin_deploy_op_lock_timeout",
            f"op_id={row['op_id']} name={name}: plugin lock not acquired, left for the next start")
        return ReconcileResult(name, int(row["op_id"]), row["op_kind"], "lock_timeout")


def reconcile_deploy_ops(conn: sqlite3.Connection, plugins_root: Path, *, now: datetime,
                         settings, activity=None, lock_timeout_sec: float = 30.0,
                         stop_event=None,
                         monotonic: Callable[[], float] = time.monotonic) -> list[ReconcileResult]:
    """Close every blocking/open deploy op once, then recover missed notifications.

    ``lock_timeout_sec`` is the lock-wait budget for the whole pass: the deadline is
    fixed once on entry, so an op whose plugin locks are not acquired by then is
    skipped and left open for the next start.
    """
    deadline = monotonic() + lock_timeout_sec
    results: list[ReconcileResult] = []
    open_rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM plugin_deploy_ops WHERE {OPEN_PHASE_SQL} ORDER BY name, op_id")]
    for row in open_rows:
        try:
            result = _reconcile_one(conn, plugins_root, row, now=now, settings=settings,
                                    activity=activity,
                                    deadline=deadline, stop_event=stop_event)
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
        if deploy_ops.try_notify(conn, row, deploy_ops.activity_or_null(activity), now=now):
            results.append(ReconcileResult(
                row["name"], int(row["op_id"]), row["op_kind"], "notified", row["fail_reason"]))
    return results
