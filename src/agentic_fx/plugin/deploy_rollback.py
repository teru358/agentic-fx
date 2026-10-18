"""Rollback deployment operation; intentionally not a tools API.

The design calls for reusing switch / loader internals (the atomic symlink swap,
version directory hash check, plugin discovery and approved-hash lookup) so that
rollback verifies exactly what a normal switch would.
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agentic_fx.plugin import deploy_ops
from agentic_fx.plugin import loader
from agentic_fx.plugin.deploy_deps import DependentsReport, enumerate_dependents
from agentic_fx.plugin.resolve import IndicatorResolutionError, resolve_indicator_deps
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store import signals as signals_store
from agentic_fx.tools import plugin_loader as tools_plugin_loader

_HASH_PREFIX_RE = re.compile(r"^[0-9a-f]{1,64}$")
_DIR_NAME_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RollbackTarget:
    name: str
    to_target: str
    artifact_hash: str


@dataclass(frozen=True)
class RollbackRefusal:
    code: str
    reason: str


@dataclass(frozen=True)
class VerifiedTarget:
    to_target: str
    content_hash: str
    artifact_hash: str
    dependents: DependentsReport


@dataclass(frozen=True)
class VerificationFailure:
    reasons: tuple[str, ...]
    dependents: DependentsReport | None = None


@dataclass(frozen=True)
class RollbackOutcome:
    outcome: str
    name: str
    filesystem_switched: bool = False
    runtime_activation: str | None = None
    cascade_strategies: tuple[str, ...] = ()
    dependents: tuple[tuple[str, str, str], ...] = ()
    reasons: tuple[str, ...] = ()
    refusal_code: str | None = None
    to_target: str | None = None
    op_id: int | None = None
    notified_op_ids: tuple[int, ...] = ()


def _version_target(name: str, artifact_hash: str) -> str:
    return f".versions/{name}/{artifact_hash}"


def discover_version_dir(version_dir: Path, name: str) -> loader.PluginMeta | None:
    try:
        return loader._discover_one(version_dir, name)
    except (OSError, ValueError):
        return None


def _live_raw_target(plugins_root: Path, name: str) -> str | None:
    live = plugins_root / name
    if not live.is_symlink():
        return None
    try:
        return os.readlink(live)
    except OSError:
        return None


def _resolve_prefix(plugins_root: Path, name: str, prefix: str) -> RollbackTarget | RollbackRefusal:
    if not _HASH_PREFIX_RE.fullmatch(prefix):
        return RollbackRefusal("bad_target", f"--to {prefix!r} is not a lowercase hex prefix")
    base = plugins_root / ".versions" / name
    matches: list[str] = []
    if base.is_dir():
        matches = sorted(entry.name for entry in base.iterdir()
                         if _DIR_NAME_RE.fullmatch(entry.name) and entry.name.startswith(prefix))
    if not matches:
        return RollbackRefusal("target_not_found", f"no version of {name!r} matches {prefix!r}")
    if len(matches) > 1:
        return RollbackRefusal(
            "target_ambiguous",
            f"--to {prefix!r} matches several versions of {name!r}: " + ", ".join(matches))
    return RollbackTarget(name, _version_target(name, matches[0]), matches[0])


def resolve_rollback_target(conn: sqlite3.Connection, plugins_root: Path, name: str, *,
                            to: str | None) -> RollbackTarget | RollbackRefusal:
    """Decide where a rollback goes (spec table of default targets)."""
    live = deploy_store.live_identity(plugins_root, name)
    if live is not None and live.kind == "plain":
        return RollbackRefusal(
            "live_is_plain", f"plugins/{name} is a plain directory; use afx plugin retire")
    live_target = _live_raw_target(plugins_root, name)
    if to is not None:
        resolved = _resolve_prefix(plugins_root, name, to)
        if isinstance(resolved, RollbackRefusal):
            return resolved
        if live_target == resolved.to_target:
            return RollbackRefusal("already_live", f"{resolved.to_target} is already the live version")
        return resolved
    if live is None:
        return RollbackRefusal("target_required", "no live version; --to is required")
    history = deploy_store.plugin_live_history(conn, name)
    if not history:
        return RollbackRefusal("target_required", "no deployment history; --to is required")
    latest = history[-1]
    if latest["kind"] in ("approve", "bless") and latest["to"] == live_target:
        previous = latest["from"]
        if previous == "absent":
            return RollbackRefusal(
                "no_rollback_target",
                "there is no earlier version to return to; use afx plugin disable")
        if not isinstance(previous, str) or not loader.is_canonical_symlink_target(name, previous):
            return RollbackRefusal("target_required", "previous version is unknown; --to is required")
        return RollbackTarget(name, previous, previous.rsplit("/", 1)[-1])
    return RollbackRefusal(
        "target_required",
        "the latest live change is not an approval of the live version; --to is required")


def current_deployed_kind(conn: sqlite3.Connection, plugins_root: Path, name: str) -> str | None:
    """Kind of the deployed version; for an absent live, of the last version taken out."""
    raw = _live_raw_target(plugins_root, name)
    if raw is not None and loader.is_canonical_symlink_target(name, raw):
        meta = discover_version_dir(plugins_root / raw, name)
        return meta.kind if meta is not None else None
    if os.path.lexists(plugins_root / name):
        return None
    for row in reversed(deploy_store.list_by_name(conn, name)):
        if row["op_kind"] == "revoke" or not deploy_store.is_closed(row):
            continue
        closed_out = (row["phase"] == "done" and row["op_kind"] in ("disable_symlink", "rollback_symlink")) or (
            row["phase"] == "needs_retry" and row["fail_reason"] == "verify_failed")
        if not closed_out:
            return None
        if row["from_kind"] == "symlink":
            artifact = row["subject_artifact_hash"]
        elif row["op_kind"] == "rollback_symlink":
            artifact = row["target_artifact_hash"]
        else:
            return None
        if not artifact:
            return None
        meta = discover_version_dir(plugins_root / ".versions" / name / artifact, name)
        return meta.kind if meta is not None else None
    return None


def _approved_content_hash(conn: sqlite3.Connection, name: str, artifact_hash: str) -> str | None:
    approved = tools_plugin_loader._approved_hashes_by_name(conn).get(name, set())
    recorded = {
        row[0] for row in conn.execute(
            "SELECT CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.content_hash') END "
            "FROM approval_requests WHERE kind='plugin' AND status IN ('approved','rejected') "
            "AND CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.name') END = ? "
            "AND CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.artifact_hash') END = ?",
            (name, artifact_hash))
        if isinstance(row[0], str)}
    admitted = sorted(recorded & set(approved))
    return admitted[0] if admitted else None


def verify_rollback_target(conn: sqlite3.Connection, plugins_root: Path, name: str,
                           target_hash: str, *, settings, force: bool,
                           current_kind: str | None,
                           report: DependentsReport | None = None
                           ) -> VerifiedTarget | VerificationFailure:
    """Validate a rollback target; every check must pass, with the lock held.

    ``report`` is the dependents report already computed for this target under the
    lock; when omitted it is computed here.
    """
    from agentic_fx.plugin import switch

    version_dir = plugins_root / ".versions" / name / target_hash
    if not _DIR_NAME_RE.fullmatch(target_hash) or version_dir.is_symlink() or not version_dir.is_dir():
        return VerificationFailure(
            (f"target version directory .versions/{name}/{target_hash} does not exist",))
    content = _approved_content_hash(conn, name, target_hash)
    if content is None:
        return VerificationFailure(
            (f"version {target_hash} has no approved decision (never approved or revoked)",))
    if not switch._version_dir_hashes_ok(
            version_dir, content_hash=content, artifact_hash=target_hash):
        return VerificationFailure(
            (f"version {target_hash} directory does not match its approved hashes",))
    meta = discover_version_dir(version_dir, name)
    if meta is None:
        return VerificationFailure((f"version {target_hash} is rejected by the plugin loader",))
    if current_kind is None:
        return VerificationFailure(
            ("the kind of the currently deployed version could not be determined",))
    if meta.kind != current_kind:
        return VerificationFailure(
            (f"target kind {meta.kind!r} differs from deployed kind {current_kind!r}",))
    if meta.kind == "strategy":
        result = tools_plugin_loader.approved_plugins(conn, plugins_root, settings=settings)
        try:
            resolve_indicator_deps(meta, result.inventory, settings=settings, pin_mode="require")
        except IndicatorResolutionError as exc:
            return VerificationFailure(
                (f"target strategy dependency {exc.alias or '-'}: {exc.reason}",))
    if report is None:
        report = enumerate_dependents(conn, plugins_root, name, settings=settings,
                                      replacement_meta=meta)
    if report.unresolvable and not force:
        listing = ", ".join(f"{s}:{a}:{r}" for s, a, r in report.unresolvable)
        return VerificationFailure((f"dependents_unresolvable: {listing}",), report)
    return VerifiedTarget(_version_target(name, target_hash), content, target_hash, report)


def dependents_with_target(conn: sqlite3.Connection, plugins_root: Path, name: str,
                           target_dir: Path | None, *, settings) -> DependentsReport:
    meta = discover_version_dir(target_dir, name) if target_dir is not None else None
    return enumerate_dependents(conn, plugins_root, name, settings=settings,
                                replacement_meta=meta)


def supplement_cascade(conn: sqlite3.Connection, plugins_root: Path, op_id: int,
                       dependents: set[str], *, now: datetime) -> None:
    """Idempotently restrict dependents that became unresolvable after the intent."""
    identities = deploy_ops.cascade_identities(conn, plugins_root, dependents)
    if not identities:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        signals_store.abandon_pending_for_identities(
            conn, identities, op_id=op_id, now=now, commit=False)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def swap_to_target(plugins_root: Path, name: str, op_id: int, to_target: str) -> None:
    from agentic_fx.plugin import switch

    switch._atomic_symlink_swap(plugins_root, plugins_root / name, target=to_target,
                                temp_name=f".{name}.link-d{op_id}")


def _finish(conn: sqlite3.Connection, op_id: int, phase: str, activity, *, now: datetime,
            fail_reason: str | None = None) -> None:
    deploy_store.set_phase(conn, op_id, phase, now=now, fail_reason=fail_reason)
    deploy_ops.try_notify(conn, deploy_store.get_by_id(conn, op_id), deploy_ops.activity_or_null(activity), now=now)


def _success(name: str, op_id: int, to_target: str, cascade: set[str]) -> RollbackOutcome:
    return RollbackOutcome("rolled_back", name, True, "pending_restart",
                           tuple(sorted(cascade)), to_target=to_target, op_id=op_id)


def rollback_plugin(conn: sqlite3.Connection, plugins_root: Path, name: str, *, to: str | None,
                    revoke: bool, force: bool, now: datetime, actor: str = "human_cli",
                    settings, activity=None, deadline: float | None = None, stop_event=None,
                    dry_run: bool = False) -> RollbackOutcome:
    """Journal-first replacement of a canonical live symlink by an earlier version."""
    if not loader.is_canonical_plugin_name(name):
        raise ValueError(f"invalid plugin name: {name!r}")

    box: list[tuple[RollbackTarget | RollbackRefusal, DependentsReport]] = []

    def dependents() -> set[str]:
        target = resolve_rollback_target(conn, plugins_root, name, to=to)
        target_dir = (plugins_root / ".versions" / name / target.artifact_hash
                      if isinstance(target, RollbackTarget) else None)
        report = dependents_with_target(conn, plugins_root, name, target_dir, settings=settings)
        box[:] = [(target, report)]
        return report.dependent_names

    with deploy_ops.acquire_op_locks(plugins_root, name, enumerate_dependents=dependents,
                                     deadline=deadline, stop_event=stop_event):
        target, report = box[-1]
        open_row = deploy_ops.check_can_start_op(conn, name, dependents=report.dependent_names)
        if open_row is not None and open_row["op_kind"] != "rollback_symlink":
            raise deploy_ops.DeployOpBlockedError(
                f"plugin {name!r} has open deploy op op_id={open_row['op_id']} "
                f"op_kind={open_row['op_kind']}")
        live = deploy_store.live_identity(plugins_root, name)
        live_path = plugins_root / name
        if live is not None and live.kind == "plain":
            raise ValueError(f"plugins/{name} is a plain directory; use afx plugin retire")
        raw_target = None
        if live is not None:
            raw_target = os.readlink(live_path)
            if (not loader.is_canonical_symlink_target(name, raw_target)
                    or not live.content_hash or not live.artifact_hash):
                raise ValueError(f"plugins/{name} is not a canonical verified symlink")

        if open_row is not None and raw_target is not None and raw_target == open_row["to_target"]:
            to_target = open_row["to_target"]
            if to is not None:
                resolved = _resolve_prefix(plugins_root, name, to)
                if isinstance(resolved, RollbackRefusal):
                    return RollbackOutcome("refused", name, reasons=(resolved.reason,),
                                           refusal_code=resolved.code)
                to_target = resolved.to_target
            requested = deploy_ops.Signature(
                "rollback_symlink", name, open_row["subject_content_hash"],
                open_row["subject_artifact_hash"], to_target, revoke, force)
            if dry_run:
                return RollbackOutcome("dry_run", name, to_target=to_target,
                                       op_id=int(open_row["op_id"]))
            op_id = deploy_ops.match_or_reject(open_row, requested, live, conn=conn, now=now)
            _finish(conn, op_id, "done", activity, now=now)
            return _success(name, op_id, to_target, report.dependent_names)

        if isinstance(target, RollbackRefusal):
            notified = ()
            if (not dry_run and raw_target is not None
                    and target.code in {"already_live", "target_required"}):
                notified = deploy_ops.recover_notifications(
                    conn, name, deploy_ops.activity_or_null(activity), now=now,
                    include=lambda row: (
                        row["op_kind"] == "rollback_symlink"
                        and row["to_target"] == raw_target),
                )
            return RollbackOutcome("refused", name, reasons=(target.reason,),
                                   refusal_code=target.code, notified_op_ids=notified)
        if revoke and live is None:
            raise ValueError(
                "cannot revoke with no live version; use afx plugin revoke --version")
        approved = deploy_ops.approved_row(conn, name, live.content_hash) if live is not None else None
        revoke_already_recorded = open_row is not None and bool(open_row["revoke"])
        if revoke and approved is None and not revoke_already_recorded:
            raise ValueError(f"no approved version for {name!r} {live.content_hash!r}")
        verified = verify_rollback_target(
            conn, plugins_root, name, target.artifact_hash, settings=settings, force=force,
            current_kind=current_deployed_kind(conn, plugins_root, name), report=report)
        if isinstance(verified, VerificationFailure):
            if verified.dependents is not None and verified.dependents.unresolvable:
                return RollbackOutcome("dependents_blocked", name,
                                       dependents=verified.dependents.unresolvable,
                                       reasons=verified.reasons,
                                       refusal_code="dependents_unresolvable",
                                       to_target=target.to_target)
            return RollbackOutcome("refused", name, reasons=verified.reasons,
                                   refusal_code="target_verification_failed",
                                   to_target=target.to_target)
        cascade = verified.dependents.dependent_names
        if dry_run:
            return RollbackOutcome("dry_run", name, cascade_strategies=tuple(sorted(cascade)),
                                   dependents=verified.dependents.unresolvable,
                                   to_target=verified.to_target)
        requested = deploy_ops.Signature(
            "rollback_symlink", name, live.content_hash if live else None,
            live.artifact_hash if live else None, verified.to_target, revoke, force)
        if open_row is not None:
            op_id = deploy_ops.match_or_reject(open_row, requested, live, conn=conn, now=now)
            if force:
                supplement_cascade(conn, plugins_root, op_id, cascade, now=now)
        else:
            identities = [(name, live.content_hash)] if live is not None else []
            identities.extend(deploy_ops.cascade_identities(conn, plugins_root, cascade))
            op_id = deploy_ops.record_intent(
                conn, intent_row={
                    "op_kind": "rollback_symlink", "name": name,
                    "from_kind": "symlink" if live is not None else "absent",
                    "to_kind": "symlink", "from_target": raw_target,
                    "to_target": verified.to_target, "retired_path": None,
                    "subject_content_hash": live.content_hash if live else None,
                    "subject_artifact_hash": live.artifact_hash if live else None,
                    "target_content_hash": verified.content_hash,
                    "target_artifact_hash": verified.artifact_hash,
                    "approval_id": approved["id"] if approved else None, "force": force,
                }, revoke=deploy_ops.RevokeSpec(name, live.content_hash) if revoke else None,
                cascade_identities=identities, now=now, actor=actor)
        if not deploy_ops.live_matches_from(live_path, raw_target):
            _finish(conn, op_id, "foreign", activity, now=now)
            return RollbackOutcome("foreign", name, op_id=op_id)
        try:
            swap_to_target(plugins_root, name, op_id, verified.to_target)
        except OSError:
            _finish(conn, op_id, "needs_retry", activity, now=now, fail_reason="syscall_failed")
            return RollbackOutcome("needs_retry", name, op_id=op_id, to_target=verified.to_target)
        _finish(conn, op_id, "done", activity, now=now)
        return _success(name, op_id, verified.to_target, cascade)
