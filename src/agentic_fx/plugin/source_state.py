"""Live/approval state of a signal's source plugin, shared by producer and OPEN gate."""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from agentic_fx.plugin.deploy_ops import require_no_open_deploy_ops
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import plugin_deploy_ops


def live_mismatch(plugins_root: Path, *, name: str, content_hash: str,
                  artifact_hash: str | None) -> str | None:
    """Disk-only check of the source plugin: ``strategy_not_live`` or None.

    Needs no database and no lock, so callers may use it as a cheap pre-check;
    the authoritative answer is ``source_state`` evaluated under the name locks.
    """
    identity = plugin_deploy_ops.live_identity(plugins_root, name)
    if identity is None or identity.content_hash != content_hash:
        return "strategy_not_live"
    if (identity.kind == "symlink" and artifact_hash is not None
            and identity.artifact_hash != artifact_hash):
        return "strategy_not_live"
    return None


def source_state(conn, plugins_root: Path, *, name: str, content_hash: str,
                 artifact_hash: str | None,
                 indicators: Iterable | None) -> str | None:
    """Why the source is unusable, or None when it is live and approved.

    ``indicators`` are the resolved indicator items (``plugin_name`` and
    ``content_hash``). Call with the plugin name locks of the source and its
    indicators held.
    """
    items = list(indicators or ())
    reason = live_mismatch(plugins_root, name=name, content_hash=content_hash,
                           artifact_hash=artifact_hash)
    if reason is not None:
        return reason
    if approvals_store.latest_decision_status(
            conn, name, content_hash) != "approved":
        return "strategy_not_approved"
    for item in items:
        identity = plugin_deploy_ops.live_identity(plugins_root, item.plugin_name)
        if identity is None:
            return "indicator_not_live"
        if identity.kind == "plain":
            continue
        if identity.content_hash != item.content_hash:
            return "indicator_not_live"
        if approvals_store.latest_decision_status(
                conn, item.plugin_name, item.content_hash) != "approved":
            return "indicator_not_approved"
    try:
        require_no_open_deploy_ops(
            conn, {name, *(item.plugin_name for item in items)},
            operation="signal production")
    except Exception:  # noqa: BLE001 — fail closed
        return "open_deploy_op"
    return None
