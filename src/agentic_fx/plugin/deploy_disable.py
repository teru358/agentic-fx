"""Disable and revoke deployment operations; intentionally not a tools API."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agentic_fx.activity import Category
from agentic_fx.plugin import deploy_ops
from agentic_fx.plugin.deploy_deps import DependentsReport, enumerate_dependents
from agentic_fx.plugin import loader
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store import signals as signals_store


@dataclass(frozen=True)
class DisableOutcome:
    outcome: str
    name: str
    filesystem_switched: bool = False
    runtime_deactivation: str | None = None
    cascade_strategies: tuple[str, ...] = ()
    dependents: tuple[tuple[str, str, str], ...] = ()
    version_candidate: str | None = None
    op_id: int | None = None
    notified_op_ids: tuple[int, ...] = ()
    expectation: deploy_ops.Expectation | None = None


@dataclass(frozen=True)
class RevokeOutcome:
    outcome: str
    name: str
    content_hash: str
    op_id: int | None = None
    notified_op_ids: tuple[int, ...] = ()
    expectation: deploy_ops.Expectation | None = None


class _RevokeActivity:
    def __init__(self, activity, content_hash: str) -> None:
        self._activity = deploy_ops.activity_or_null(activity)
        self._content_hash = content_hash

    def write(self, category, event: str, summary: str) -> None:
        self._activity.write(category, event, f"{summary} version={self._content_hash}")


def _latest_disable_candidate(conn: sqlite3.Connection, name: str) -> str | None:
    for row in reversed(deploy_store.list_by_name(conn, name)):
        if row["op_kind"] == "disable_symlink" and row["phase"] == "done":
            return row["subject_content_hash"]
    return None


def revoke_expectation(conn: sqlite3.Connection, name: str,
                       content_hash: str) -> deploy_ops.Expectation:
    """The latest approval decision of a version, as the state a revoke depends on."""
    decision = approvals_store.latest_decision_for_identity(conn, name, content_hash)
    return deploy_ops.Expectation(
        decision=(int(decision["id"]), str(decision["status"])) if decision else None)


def recover_revoke_notifications(conn: sqlite3.Connection, plugins_root: Path, name: str,
                                 content_hash: str, activity, *, now: datetime,
                                 expect: deploy_ops.Expectation | None,
                                 deadline: float | None = None) -> tuple[int, ...] | None:
    """Attempt missed revoke notifications while the version's decision is as previewed.

    Returns ``None`` (and does nothing) when the decision changed since the preview.
    """
    from agentic_fx.plugin import switch

    with switch._plugin_locks(plugins_root, [name], deadline=deadline):
        if expect is not None and revoke_expectation(conn, name, content_hash) != expect:
            return None
        return deploy_ops.recover_notifications(
            conn, name, _RevokeActivity(activity, content_hash), now=now,
            include=lambda row: (row["op_kind"] == "revoke"
                                 and row["subject_content_hash"] == content_hash))


def _find_version_identity(plugins_root: Path, name: str, content_hash: str):
    base = plugins_root / ".versions" / name
    if not base.is_dir():
        return None
    for candidate in base.iterdir():
        if not candidate.is_dir():
            continue
        try:
            if loader.content_hash(candidate) != content_hash:
                continue
            artifact = loader.artifact_hash_bytes(
                (candidate / "plugin.py").read_bytes(),
                (candidate / "config.yaml").read_bytes(),
                (candidate / "test_plugin.py").read_bytes())
        except OSError:
            continue
        return candidate, artifact
    return None


def disable_plugin(conn: sqlite3.Connection, plugins_root: Path, name: str, *, revoke: bool,
                   force: bool, now: datetime, actor: str = "human_cli", settings,
                   activity=None, deadline: float | None = None, stop_event=None,
                   dry_run: bool = False,
                   expect: deploy_ops.Expectation | None = None) -> DisableOutcome:
    """Journal-first unlink of a canonical live symlink.

    ``expect`` is the state a preview showed; a real run refuses with
    ``StateChangedError``, changing nothing, when the state under the lock differs.
    """
    if not loader.is_canonical_plugin_name(name):
        raise ValueError(f"invalid plugin name: {name!r}")

    report_box: list[DependentsReport] = []

    def dependents() -> set[str]:
        report = enumerate_dependents(conn, plugins_root, name, settings=settings)
        report_box[:] = [report]
        return report.dependent_names

    with deploy_ops.acquire_op_locks(plugins_root, name, enumerate_dependents=dependents,
                                     deadline=deadline, stop_event=stop_event):
        report = report_box[-1]
        open_row = deploy_ops.check_can_start_op(conn, name, dependents=report.dependent_names)
        live = deploy_store.live_identity(plugins_root, name)
        live_path = plugins_root / name
        observed = deploy_ops.observe(plugins_root, name, open_row=open_row,
                                      cascade=report.dependent_names, identity=live)
        if not dry_run:
            deploy_ops.require_expectation(expect, observed)
        if open_row is not None and open_row["op_kind"] != "disable_symlink":
            raise deploy_ops.DeployOpBlockedError(
                f"plugin {name!r} has open deploy op op_id={open_row['op_id']} "
                f"op_kind={open_row['op_kind']}")
        if (live is None and open_row is not None
                and open_row["op_kind"] == "disable_symlink"):
            if bool(open_row["revoke"]) != revoke or bool(open_row["force"]) != force:
                hint = ("; use `afx plugin revoke --version` to revoke a version"
                        if revoke and not open_row["revoke"] else "")
                raise deploy_ops.SignatureMismatchError(
                    f"open deploy op op_id={open_row['op_id']} was recorded with "
                    f"revoke={bool(open_row['revoke'])} force={bool(open_row['force'])}, "
                    f"requested revoke={revoke} force={force}{hint}")
            op_id = int(open_row["op_id"])
            if dry_run:
                return DisableOutcome("dry_run", name, op_id=op_id,
                                      cascade_strategies=tuple(sorted(report.dependent_names)),
                                      expectation=observed)
            deploy_ops.settle_op(conn, open_row, "done", activity, now=now)
            return DisableOutcome("disabled", name, True, "effective_on_next_signal_insert",
                                  tuple(sorted(report.dependent_names)), op_id=op_id)
        if live is None:
            notified = (() if dry_run else deploy_ops.recover_notifications(
                conn, name, deploy_ops.activity_or_null(activity), now=now))
            if revoke:
                return DisableOutcome("revoke_requires_version", name,
                                      version_candidate=_latest_disable_candidate(conn, name),
                                      notified_op_ids=notified)
            return DisableOutcome("already_disabled", name, notified_op_ids=notified,
                                  expectation=observed)
        if live.kind == "plain":
            raise ValueError(f"plugins/{name} is a plain directory; use afx plugin retire")
        raw_target = os.readlink(live_path)
        if not loader.is_canonical_symlink_target(name, raw_target) or not live.content_hash or not live.artifact_hash:
            raise ValueError(f"plugins/{name} is not a canonical verified symlink")
        requested = deploy_ops.Signature("disable_symlink", name, live.content_hash,
                                         live.artifact_hash, None, revoke, force)
        if open_row is not None:
            if deploy_ops.row_signature(open_row) != requested:
                raise deploy_ops.SignatureMismatchError(
                    f"open deploy op op_id={open_row['op_id']} has a different signature")
            op_id = int(open_row["op_id"])
        else:
            op_id = None
        if open_row is None and report.unresolvable and not force:
            return DisableOutcome("dependents_blocked", name, dependents=report.unresolvable)
        approved = deploy_ops.approved_row(conn, name, live.content_hash)
        takeover = open_row is not None and bool(open_row["revoke"])
        if revoke and approved is None and not takeover:
            raise ValueError(f"no approved version for {name!r} {live.content_hash!r}")
        if dry_run:
            return DisableOutcome("dry_run", name, cascade_strategies=tuple(sorted(report.dependent_names)),
                                  dependents=report.unresolvable, expectation=observed)
        if open_row is not None:
            op_id = deploy_ops.match_or_reject(open_row, requested, live, conn=conn, now=now)
        if op_id is None:
            identities = [(name, live.content_hash)]
            identities.extend(deploy_ops.cascade_identities(conn, plugins_root, report.dependent_names))
            op_id = deploy_ops.record_intent(
                conn, intent_row={
                    "op_kind": "disable_symlink", "name": name, "from_kind": "symlink",
                    "to_kind": "absent", "from_target": raw_target, "to_target": None,
                    "retired_path": None, "subject_content_hash": live.content_hash,
                    "subject_artifact_hash": live.artifact_hash, "target_content_hash": None,
                    "target_artifact_hash": None, "approval_id": approved["id"] if approved else None,
                    "force": force,
                }, revoke=deploy_ops.RevokeSpec(name, live.content_hash) if revoke else None,
                cascade_identities=identities, now=now, actor=actor)
        if not deploy_ops.live_matches_from(live_path, raw_target):
            deploy_store.set_phase(conn, op_id, "foreign", now=now)
            row = deploy_store.get_by_id(conn, op_id)
            deploy_ops.try_notify(conn, row, deploy_ops.activity_or_null(activity), now=now)
            return DisableOutcome("foreign", name, op_id=op_id)
        try:
            os.unlink(live_path)
        except OSError:
            deploy_store.set_phase(conn, op_id, "needs_retry", now=now, fail_reason="syscall_failed")
            row = deploy_store.get_open_by_name(conn, name)
            deploy_ops.try_notify(conn, row, deploy_ops.activity_or_null(activity), now=now)
            return DisableOutcome("needs_retry", name, op_id=op_id)
        deploy_store.set_phase(conn, op_id, "done", now=now)
        row = deploy_store.get_by_id(conn, op_id)
        deploy_ops.try_notify(conn, row, deploy_ops.activity_or_null(activity), now=now)
        return DisableOutcome("disabled", name, True, "effective_on_next_signal_insert",
                              tuple(sorted(report.dependent_names)), op_id=op_id)


def revoke_version(conn: sqlite3.Connection, plugins_root: Path, name: str, *, content_hash: str,
                   now: datetime, actor: str = "human_cli", activity=None,
                   deadline: float | None = None, stop_event=None,
                   dry_run: bool = False,
                   expect: deploy_ops.Expectation | None = None) -> RevokeOutcome:
    """Revoke a non-live version and link its pending/claimed signals atomically.

    ``expect`` is the state a preview showed; a real run refuses with
    ``StateChangedError`` when the latest decision of the version changed since.
    """
    if not loader.is_canonical_plugin_name(name):
        raise ValueError(f"invalid plugin name: {name!r}")
    from agentic_fx.plugin import switch
    with switch._plugin_locks(plugins_root, [name], deadline=deadline, stop_event=stop_event):
        open_row = deploy_ops.check_can_start_op(conn, name, dependents=set())
        if open_row is not None:
            raise deploy_ops.DeployOpBlockedError(f"plugin {name!r} has open deploy op")
        observed = revoke_expectation(conn, name, content_hash)
        if not dry_run:
            deploy_ops.require_expectation(expect, observed)
        live = deploy_store.live_identity(plugins_root, name)
        if live is not None and live.content_hash == content_hash:
            raise ValueError("cannot revoke the current live version; disable it first")
        if dry_run:
            decision = approvals_store.latest_decision_for_identity(conn, name, content_hash)
            if decision is None:
                raise ValueError(f"no approved plugin approval for {name!r} {content_hash!r}")
            if decision["status"] == "rejected":
                return RevokeOutcome("already_revoked", name, content_hash,
                                     expectation=observed)
            return RevokeOutcome("dry_run", name, content_hash, expectation=observed)
        found = _find_version_identity(plugins_root, name, content_hash)
        dir_artifact = found[1] if found is not None else None
        mismatch_artifact: str | None = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            revoke_row_id = approvals_store.append_revocation(
                conn, name, content_hash, now=now, actor=actor, commit=False)
            if revoke_row_id is None:
                conn.rollback()
                notified = deploy_ops.recover_notifications(
                    conn, name, _RevokeActivity(activity, content_hash), now=now,
                    include=lambda row: (row["op_kind"] == "revoke"
                                         and row["subject_content_hash"] == content_hash))
                return RevokeOutcome("already_revoked", name, content_hash,
                                     notified_op_ids=notified)
            payload = conn.execute("SELECT payload_json FROM approval_requests WHERE id=?", (revoke_row_id,)).fetchone()
            revoke_payload = json.loads(payload["payload_json"])
            approval_id = revoke_payload["revokes_approval_id"]
            artifact_hash = revoke_payload.get("artifact_hash")
            if not isinstance(artifact_hash, str) or not artifact_hash:
                if dir_artifact is None:
                    raise ValueError(
                        f"cannot determine the artifact hash of {name!r} {content_hash!r}: "
                        "the approval records none and no version directory matches")
                artifact_hash = dir_artifact
            elif dir_artifact is not None and dir_artifact != artifact_hash:
                mismatch_artifact = dir_artifact
            op_id = deploy_store.insert_done_revoke(
                conn, name=name, subject_content_hash=content_hash,
                subject_artifact_hash=artifact_hash, approval_id=approval_id,
                revoke_row_id=revoke_row_id, now=now, actor=actor)
            signals_store.abandon_pending_for_identities(
                conn, [(name, content_hash)], op_id=op_id, now=now, commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        row = deploy_store.get_by_id(conn, op_id)
        revoke_activity = _RevokeActivity(activity, content_hash)
        if mismatch_artifact is not None:
            try:
                revoke_activity.write(
                    Category.APPROVAL, "plugin_version_dir_mismatch",
                    f"name={name} dir_artifact={mismatch_artifact} "
                    f"approved_artifact={artifact_hash}")
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "version dir mismatch notice failed name=%s: %r", name, exc)
        deploy_ops.try_notify(conn, row, revoke_activity, now=now)
        return RevokeOutcome("revoked", name, content_hash, op_id)
