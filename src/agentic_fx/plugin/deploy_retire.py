"""Retirement deployment operations for legacy plain plugin directories."""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agentic_fx.plugin import deploy_disable, deploy_ops, deploy_reconcile, loader
from agentic_fx.plugin.deploy_deps import DependentsReport, enumerate_dependents
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store import plugin_switch_journal as journal_store
from agentic_fx.store import signals as signals_store


@dataclass(frozen=True)
class RetireOutcome:
    outcome: str
    name: str
    op_id: int | None = None
    retired_path: str | None = None
    notified_op_ids: tuple[int, ...] = ()
    cascade_strategies: tuple[str, ...] = ()
    dependents: tuple[tuple[str, str, str], ...] = ()


def _plain(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _retired_path(name: str, now: datetime, op_id: int) -> str:
    return f"_retired/{name}-{now.strftime('%Y%m%dT%H%M%SZ')}-{op_id}"


def _recover_notifications(conn: sqlite3.Connection, name: str, activity, *, now: datetime
                           ) -> tuple[int, ...]:
    notified: list[int] = []
    for row in reversed(deploy_store.list_by_name(conn, name)):
        if row["notify_attempted"] or deploy_ops.activity_event(row) is None:
            continue
        if deploy_ops.try_notify(conn, row, activity, now=now):
            notified.append(int(row["op_id"]))
    return tuple(notified)


def retire_plugin(conn: sqlite3.Connection, plugins_root: Path, name: str, *, force: bool,
                  now: datetime, actor: str = "human_cli", settings, activity=None,
                  deadline: float | None = None, stop_event=None,
                  dry_run: bool = False) -> RetireOutcome | deploy_disable.DisableOutcome:
    """Retire a plain live directory, or run the disable state machine for symlinks."""
    if not loader.is_canonical_plugin_name(name):
        raise ValueError(f"invalid plugin name: {name!r}")
    live_path = Path(plugins_root) / name
    journal = journal_store.get_open_by_name(conn, name)
    if journal is not None:
        from agentic_fx.plugin import switch
        raise switch.UnresolvedJournalError(
            f"plugin {name!r} has an unresolved switch journal "
            f"(op_id={journal['op_id']}) — resolve it first "
            "(reconcile or approval retry)")
    open_before_lock = deploy_store.get_open_by_name(conn, name)
    if live_path.is_symlink() and (open_before_lock is None
                                   or open_before_lock["op_kind"] != "retire_plain"):
        live = deploy_store.live_identity(plugins_root, name)
        if live is None or not live.content_hash or not live.artifact_hash:
            raise ValueError(f"plugins/{name} is not a plain directory "
                             "(retire only applies to legacy plain live)")
        return deploy_disable.disable_plugin(
            conn, plugins_root, name, revoke=False, force=force, now=now, actor=actor,
            settings=settings, activity=activity, deadline=deadline, stop_event=stop_event,
            dry_run=dry_run)

    report_box: list[DependentsReport] = []

    def dependents() -> set[str]:
        report = enumerate_dependents(conn, plugins_root, name, settings=settings)
        report_box[:] = [report]
        return report.dependent_names

    with deploy_ops.acquire_op_locks(plugins_root, name, enumerate_dependents=dependents,
                                     deadline=deadline, stop_event=stop_event):
        report = report_box[-1]
        journal = journal_store.get_open_by_name(conn, name)
        if journal is not None:
            from agentic_fx.plugin import switch
            raise switch.UnresolvedJournalError(
                f"plugin {name!r} has an unresolved switch journal "
                f"(op_id={journal['op_id']}) — resolve it first "
                "(reconcile or approval retry)")
        open_row = deploy_ops.check_can_start_op(conn, name, dependents=report.dependent_names)
        live_plain = _plain(live_path)
        live_identity = deploy_store.live_identity(plugins_root, name) if live_plain else None
        subject_content_hash = (
            live_identity.content_hash if live_identity is not None else None)
        if open_row is not None and open_row["op_kind"] != "retire_plain":
            raise deploy_ops.DeployOpBlockedError(
                f"plugin {name!r} has open deploy op op_id={open_row['op_id']} "
                f"op_kind={open_row['op_kind']}")
        if open_row is not None:
            op_id = int(open_row["op_id"])
            retired_path = str(open_row["retired_path"])
            retired = Path(plugins_root) / retired_path
            if dry_run:
                return RetireOutcome("dry_run", name, op_id, retired_path,
                                     cascade_strategies=tuple(sorted(report.dependent_names)),
                                     dependents=report.unresolvable)
            if not live_plain:
                if not os.path.lexists(live_path) and retired.exists():
                    deploy_store.set_phase(conn, op_id, "done", now=now)
                    row = deploy_store.get_by_id(conn, op_id)
                    deploy_ops.try_notify(conn, row, deploy_ops.activity_or_null(activity), now=now)
                    return RetireOutcome("done", name, op_id, retired_path)
                deploy_store.set_phase(conn, op_id, "foreign", now=now)
                row = deploy_store.get_by_id(conn, op_id)
                deploy_ops.try_notify(conn, row, deploy_ops.activity_or_null(activity), now=now)
                return RetireOutcome("foreign", name, op_id, retired_path)
            requested = deploy_ops.Signature(
                "retire_plain", name, subject_content_hash, None, None, False, force)
            if deploy_ops.row_signature(open_row) != requested:
                raise deploy_ops.SignatureMismatchError(
                    f"open deploy op op_id={open_row['op_id']} has a different signature")
            op_id = deploy_ops.match_or_reject(
                open_row, requested, live_identity,
                conn=conn, now=now)
        else:
            if not live_plain:
                if os.path.lexists(live_path):
                    raise ValueError(f"plugins/{name} is not a plain directory "
                                     "(retire only applies to legacy plain live)")
                notified = _recover_notifications(
                    conn, name, deploy_ops.activity_or_null(activity), now=now)
                return RetireOutcome("already_retired", name, notified_op_ids=notified)
            if report.unresolvable and not force:
                return RetireOutcome("dependents_blocked", name, dependents=report.unresolvable)
            if dry_run:
                return RetireOutcome("dry_run", name,
                                     cascade_strategies=tuple(sorted(report.dependent_names)),
                                     dependents=report.unresolvable)
            retired_root = Path(plugins_root) / "_retired"
            retired_root.mkdir(exist_ok=True)
            if os.stat(plugins_root).st_dev != os.stat(retired_root).st_dev:
                raise OSError("plugins/_retired must be on the same filesystem as plugins")

            def assign_destination(op_id: int) -> None:
                retired_path = _retired_path(name, now, op_id)
                if (Path(plugins_root) / retired_path).exists():
                    raise FileExistsError(f"retired destination already exists: {retired_path}")
                deploy_store.set_retired_path(conn, op_id, retired_path, commit=False)
                signals_store.abandon_pending_for_name(
                    conn, name, op_id=op_id, now=now, commit=False)

            op_id = deploy_ops.record_intent(
                conn, intent_row={
                    "op_kind": "retire_plain", "name": name, "from_kind": "plain",
                    "to_kind": "retired", "from_target": None, "to_target": None,
                    "retired_path": "_retired/.pending",
                    "subject_content_hash": subject_content_hash,
                    "subject_artifact_hash": None, "target_content_hash": None,
                    "target_artifact_hash": None, "approval_id": None, "force": force,
                }, revoke=None, cascade_identities=[], now=now, actor=actor,
                after_insert=assign_destination)
            retired_path = str(deploy_store.get_by_id(conn, op_id)["retired_path"])

        retired = Path(plugins_root) / retired_path
        current_identity = deploy_store.live_identity(plugins_root, name)
        recorded_identity = deploy_store.get_by_id(conn, op_id)["subject_content_hash"]
        if (not _plain(live_path) or os.path.lexists(retired)
                or (recorded_identity is not None and (
                    current_identity is None
                    or current_identity.content_hash != recorded_identity))):
            deploy_store.set_phase(conn, op_id, "foreign", now=now)
            row = deploy_store.get_by_id(conn, op_id)
            deploy_ops.try_notify(conn, row, deploy_ops.activity_or_null(activity), now=now)
            return RetireOutcome("foreign", name, op_id, retired_path)
        try:
            deploy_reconcile.rename_plain_dir(live_path, retired)
        except OSError:
            deploy_store.set_phase(conn, op_id, "needs_retry", now=now,
                                   fail_reason="syscall_failed")
            row = deploy_store.get_open_by_name(conn, name)
            deploy_ops.try_notify(conn, row, deploy_ops.activity_or_null(activity), now=now)
            return RetireOutcome("needs_retry", name, op_id, retired_path)
        deploy_store.set_phase(conn, op_id, "done", now=now)
        row = deploy_store.get_by_id(conn, op_id)
        deploy_ops.try_notify(conn, row, deploy_ops.activity_or_null(activity), now=now)
        return RetireOutcome("done", name, op_id, retired_path,
                             cascade_strategies=tuple(sorted(report.dependent_names)))
