"""Shared, DB-first helpers for durable plugin deployment operations.

These helpers deliberately do not invoke ``approve_candidate``,
``bless_candidate``, or ``retire_plugin`` while a name lock is held:
``_plugin_locks`` is non-reentrant for the same path and doing so can
self-deadlock the process.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
from os import readlink as _readlink
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from agentic_fx.activity import Category
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store import plugin_switch_journal as journal_store
from agentic_fx.store import signals as signals_store


class DependentsChangedError(RuntimeError):
    """The dependency set changed on every bounded lock acquisition attempt."""


class DeployOpBlockedError(RuntimeError):
    """A switch journal or another blocking deploy operation prevents progress."""


class SignatureMismatchError(DeployOpBlockedError):
    """A retry cannot safely inherit the existing immutable operation intent."""


class StateChangedError(RuntimeError):
    """The state shown to the operator before confirmation no longer holds."""


@dataclass(frozen=True)
class Expectation:
    """What an operator was shown before confirming; re-checked under the lock.

    ``live_*`` / ``content_hash`` / ``artifact_hash`` describe the live entry,
    ``to_target`` the rollback destination, ``cascade`` the strategies that would
    be restricted, ``decision`` the latest approval decision of a revoke subject
    and ``open_op_id`` the open deploy op that would be inherited.
    """
    live_kind: str | None = None
    live_target: str | None = None
    content_hash: str | None = None
    artifact_hash: str | None = None
    to_target: str | None = None
    cascade: tuple[str, ...] = ()
    decision: tuple[int, str] | None = None
    open_op_id: int | None = None

    def same_live(self, other: "Expectation") -> bool:
        return (self.live_kind, self.live_target, self.content_hash, self.artifact_hash) == (
            other.live_kind, other.live_target, other.content_hash, other.artifact_hash)


_UNREAD = object()


def observe_live(plugins_root: Path, name: str, identity=_UNREAD) -> Expectation:
    """The live entry of ``name`` as an ``Expectation`` (live fields only).

    ``identity`` is the already-read ``live_identity`` result, when the caller has one.
    """
    live_path = Path(plugins_root) / name
    target = None
    if live_path.is_symlink():
        kind = "symlink"
        try:
            target = _readlink(live_path)
        except OSError:
            target = None
    elif live_path.is_dir():
        kind = "plain"
    elif os.path.lexists(live_path):
        kind = "other"
    else:
        kind = "absent"
    if identity is _UNREAD:
        identity = deploy_store.live_identity(plugins_root, name)
    return Expectation(
        live_kind=kind, live_target=target,
        content_hash=identity.content_hash if identity is not None else None,
        artifact_hash=identity.artifact_hash if identity is not None else None)


def observe(plugins_root: Path, name: str, *, open_row: Mapping[str, object] | None,
            cascade, to_target: str | None = None, identity=_UNREAD) -> Expectation:
    """``observe_live`` plus the plan-level facts an operation depends on."""
    return replace(
        observe_live(plugins_root, name, identity), to_target=to_target,
        cascade=tuple(sorted(cascade)),
        open_op_id=int(open_row["op_id"]) if open_row is not None else None)


def require_expectation(expect: Expectation | None, observed: Expectation) -> None:
    """Refuse, before any change, when the state differs from what was previewed."""
    if expect is not None and expect != observed:
        raise StateChangedError(
            f"state changed after the preview: expected {expect!r}, found {observed!r}")


def recover_noop_notifications(conn: sqlite3.Connection, plugins_root: Path, name: str,
                               activity, *, now: datetime, expect: Expectation | None,
                               deadline: float | None = None) -> tuple[int, ...] | None:
    """Attempt missed notifications for a no-op, only while the live entry is as previewed.

    Returns ``None`` (and does nothing) when the live entry changed since the preview.
    """
    from agentic_fx.plugin import switch

    with switch._plugin_locks(plugins_root, [name], deadline=deadline):
        if expect is not None and not observe_live(plugins_root, name).same_live(expect):
            return None
        return recover_notifications(conn, name, activity_or_null(activity), now=now)


@dataclass(frozen=True)
class Signature:
    op_kind: str
    name: str
    subject_content_hash: str | None
    subject_artifact_hash: str | None
    to_target: str | None
    revoke: bool
    force: bool


@dataclass(frozen=True)
class RevokeSpec:
    name: str
    content_hash: str


def row_signature(row: Mapping[str, object]) -> Signature:
    """Return the immutable fields of a deployment-op row."""
    return Signature(
        op_kind=str(row["op_kind"]), name=str(row["name"]),
        subject_content_hash=row["subject_content_hash"],
        subject_artifact_hash=row["subject_artifact_hash"],
        to_target=row["to_target"], revoke=bool(row["revoke"]),
        force=bool(row["force"]),
    )


def _signature_text(signature: Signature) -> str:
    return (
        f"op_kind={signature.op_kind!r}, name={signature.name!r}, "
        f"subject_content_hash={signature.subject_content_hash!r}, "
        f"subject_artifact_hash={signature.subject_artifact_hash!r}, "
        f"to_target={signature.to_target!r}, revoke={signature.revoke!r}, "
        f"force={signature.force!r}"
    )


def _from_identity(row: Mapping[str, object]) -> deploy_store.LiveIdentity | None:
    if row["from_kind"] == "absent":
        return None
    if row["from_kind"] in ("symlink", "plain"):
        return deploy_store.LiveIdentity(
            str(row["from_kind"]), row["subject_content_hash"],
            row["subject_artifact_hash"],
        )
    return None


def _to_identity(row: Mapping[str, object]) -> deploy_store.LiveIdentity | None:
    if row["to_kind"] in ("absent", "retired", "none"):
        return None
    if row["to_kind"] == "symlink":
        return deploy_store.LiveIdentity(
            "symlink", row["target_content_hash"], row["target_artifact_hash"],
        )
    return None


def signature_matches(open_row: Mapping[str, object], requested: Signature) -> bool:
    """Whether every immutable field of the open op equals the requested signature."""
    return row_signature(open_row) == requested


def match_or_reject(open_row: Mapping[str, object], requested: Signature,
                    live_identity: deploy_store.LiveIdentity | None, *,
                    conn: sqlite3.Connection | None = None,
                    now: datetime | None = None) -> int:
    """Validate retry inheritance and reopen a syscall failure when requested.

    The caller supplies an identity read while holding the operation's name lock.
    A retry is accepted only if every immutable field matches and live is still
    either the recorded pre-operation or post-operation identity.
    """
    existing = row_signature(open_row)
    if not signature_matches(open_row, requested):
        raise SignatureMismatchError(
            f"open deploy op op_id={open_row['op_id']} has signature "
            f"({_signature_text(existing)}); requested "
            f"({_signature_text(requested)}) — wait for reconcile or restart "
            "the daemon")
    if live_identity not in (_from_identity(open_row), _to_identity(open_row)):
        raise SignatureMismatchError(
            f"open deploy op op_id={open_row['op_id']} has signature "
            f"({_signature_text(existing)}), but live identity {live_identity!r} "
            "is neither its from nor to identity — wait for reconcile or restart "
            "the daemon")
    if open_row["phase"] == "needs_retry" and open_row["fail_reason"] == "syscall_failed":
        if conn is None or now is None:
            raise ValueError("conn and now are required to reopen syscall_failed")
        deploy_store.set_phase(conn, int(open_row["op_id"]), "intent", now=now)
    return int(open_row["op_id"])


@contextlib.contextmanager
def acquire_name_locks(plugins_root: Path, names, *,
                       deadline: float | None = None, stop_event=None):
    """Hold the plugin name locks for ``names`` (sorted, deduplicated).

    Raises ``switch.PluginBusyError`` when ``deadline`` passes first. Not
    reentrant for a name this process already holds.
    """
    from agentic_fx.plugin import switch

    kwargs = {"deadline": deadline}
    if stop_event is not None:
        kwargs["stop_event"] = stop_event
    with switch._plugin_locks(plugins_root, names, **kwargs):
        yield


@contextlib.contextmanager
def acquire_op_locks(plugins_root: Path, name: str, *,
                     enumerate_dependents: Callable[[], set[str]],
                     deadline: float | None = None, stop_event=None,
                     max_attempts: int = 3):
    """Acquire stable target/dependent locks, retrying if dependencies grow.

    ``_plugin_locks`` is intentionally used once per attempt.  Do not call a
    switch entry point from the yielded block: the underlying flock is not
    reentrant when the same path is opened twice by this process.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    # Imported lazily to keep this shared module independent of switch's import
    # graph while still using the single canonical flock implementation.
    from agentic_fx.plugin import switch

    for _attempt in range(max_attempts):
        provisional = {name, *enumerate_dependents()}
        with switch._plugin_locks(plugins_root, provisional, deadline=deadline,
                                  stop_event=stop_event):
            confirmed = {name, *enumerate_dependents()}
            if confirmed <= provisional:
                yield confirmed
                return
    raise DependentsChangedError(
        f"dependents for {name!r} changed while acquiring operation locks")


def check_can_start_op(conn: sqlite3.Connection, name: str, *,
                       dependents: set[str]) -> dict | None:
    """Reject conflicting switch journals and return the target's open op."""
    for checked_name in sorted({name, *dependents}):
        journal = journal_store.get_open_by_name(conn, checked_name)
        if journal is not None:
            raise DeployOpBlockedError(
                f"plugin {checked_name!r} has unresolved switch journal "
                f"op_id={journal['op_id']} blocking deploy operation for {name!r}")
    return deploy_store.get_open_by_name(conn, name)


def require_no_open_deploy_ops(conn: sqlite3.Connection, names: set[str], *,
                               operation: str) -> None:
    """Reject an approval/switch entry point blocked by any intersecting name."""
    for checked_name in sorted(names):
        row = deploy_store.get_open_by_name(conn, checked_name)
        if row is not None:
            raise DeployOpBlockedError(
                f"plugin {checked_name!r} has open deploy op op_id={row['op_id']} "
                f"blocking {operation}")


def record_intent(conn: sqlite3.Connection, *, intent_row: dict,
                  revoke: RevokeSpec | None,
                  cascade_identities: list[tuple[str, str]], now: datetime,
                  actor: str, after_insert: Callable[[int], None] | None = None) -> int:
    """Atomically persist a restrictive deploy intent and its signal effects."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        revoke_row_id = None
        approval_id = intent_row.get("approval_id")
        if revoke is not None:
            revoke_row_id = approvals_store.append_revocation(
                conn, revoke.name, revoke.content_hash, now=now, actor=actor,
                commit=False)
            if revoke_row_id is None:
                raise ValueError(
                    f"cannot revoke {revoke.name!r} {revoke.content_hash!r}: "
                    "latest approval is already rejected")
            revoke_payload = conn.execute(
                "SELECT payload_json FROM approval_requests WHERE id=?", (revoke_row_id,)
            ).fetchone()
            approval_id = json.loads(revoke_payload["payload_json"])["revokes_approval_id"]
        values = dict(intent_row)
        values.update(approval_id=approval_id, revoke=revoke is not None,
                      revoke_row_id=revoke_row_id, actor=actor, now=now,
                      commit=False)
        op_id = deploy_store.insert_intent(conn, **values)
        if after_insert is not None:
            after_insert(op_id)
        signals_store.abandon_pending_for_identities(
            conn, cascade_identities, op_id=op_id, now=now, commit=False)
        conn.commit()
        return op_id
    except BaseException:
        conn.rollback()
        raise


def activity_event(op_row: Mapping[str, object]) -> str | None:
    """Map a terminal deployment-op state to its single activity event name."""
    if op_row["phase"] == "done":
        if op_row["op_kind"] == "disable_symlink":
            return "plugin_disabled"
        if op_row["op_kind"] == "retire_plain":
            return "plugin_retired"
        return "plugin_deploy_op_done"
    if op_row["phase"] == "needs_retry":
        return "plugin_deploy_op_needs_retry"
    if op_row["phase"] == "foreign":
        return "plugin_deploy_op_foreign"
    return None


def try_notify(conn: sqlite3.Connection, op_row: Mapping[str, object], activity, *,
               now: datetime) -> bool:
    """Attempt a notification for a notification-window row.

    Delivery is not guaranteed exactly-once (spec section 7.0); only the fact
    that an activity write was attempted is recorded.
    """
    event = activity_event(op_row)
    if event is None or bool(op_row["notify_attempted"]):
        return False
    try:
        activity.write(
            Category.APPROVAL, event,
            f"op_id={op_row['op_id']} op_kind={op_row['op_kind']} "
            f"name={op_row['name']} phase={op_row['phase']}",
        )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "deploy op notify failed op_id=%s event=%s: %r",
            op_row["op_id"], event, exc)
    deploy_store.mark_notify_attempted(conn, int(op_row["op_id"]))
    return True


def settle_op(conn: sqlite3.Connection, op_row: Mapping[str, object], phase: str, activity, *,
              now: datetime, fail_reason: str | None = None) -> None:
    """Move an op to ``phase`` and attempt its notification.

    Leaving the ``needs_retry`` / ``syscall_failed`` state for any other state is a
    new verdict, so its notification is owed again.
    """
    leaves_syscall_failure = (
        op_row["phase"] == "needs_retry" and op_row["fail_reason"] == "syscall_failed"
        and not (phase == "needs_retry" and fail_reason == "syscall_failed"))
    op_id = int(op_row["op_id"])
    deploy_store.set_phase(conn, op_id, phase, now=now, fail_reason=fail_reason,
                           notify_attempted=False if leaves_syscall_failure else None)
    try_notify(conn, deploy_store.get_by_id(conn, op_id), activity_or_null(activity), now=now)


def recover_notifications(conn: sqlite3.Connection, name: str, activity, *, now: datetime,
                          include: Callable[[Mapping[str, object]], bool] | None = None
                          ) -> tuple[int, ...]:
    """Attempt missed notifications for completed operations on one plugin name."""
    notified: list[int] = []
    for row in reversed(deploy_store.list_by_name(conn, name)):
        if include is not None and not include(row):
            continue
        if row["notify_attempted"] or activity_event(row) is None:
            continue
        if try_notify(conn, row, activity, now=now):
            notified.append(int(row["op_id"]))
    return tuple(notified)


class NullActivity:
    def write(self, *args, **kwargs) -> None:
        return None


def activity_or_null(activity):
    return activity if activity is not None else NullActivity()


def approved_row(conn: sqlite3.Connection, name: str, content_hash: str) -> dict | None:
    """The latest decision of an identity when it is an approval, else None."""
    row = approvals_store.latest_decision_for_identity(conn, name, content_hash)
    return row if row is not None and row["status"] == "approved" else None


def cascade_identities(conn: sqlite3.Connection, plugins_root: Path,
                       names: set[str]) -> list[tuple[str, str]]:
    """Live, approved (name, content_hash) identities among ``names``."""
    identities: list[tuple[str, str]] = []
    for strategy in sorted(names):
        live = deploy_store.live_identity(plugins_root, strategy)
        if live is None or live.content_hash is None:
            continue
        decision = approvals_store.latest_decision_for_identity(
            conn, strategy, live.content_hash)
        if decision is None or decision["status"] != "approved":
            continue
        identities.append((strategy, live.content_hash))
    return identities


def live_matches_from(live_path: Path, from_target: str | None) -> bool:
    """Whether ``live_path`` is still exactly the state an op recorded as its source.

    ``from_target`` is the raw symlink target, or ``None`` when the source state
    was an absent live.
    """
    if from_target is None:
        return not os.path.lexists(live_path)
    if not live_path.is_symlink():
        return False
    try:
        return os.readlink(live_path) == from_target
    except OSError:
        return False
