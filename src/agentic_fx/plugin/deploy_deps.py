"""Read-only dependency impact calculation for deployment operations."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from agentic_fx.plugin.resolve import (
    ApprovedInventory, IndicatorResolutionError, resolve_indicator_deps,
)
from agentic_fx.tools import plugin_loader as tools_plugin_loader


@dataclass(frozen=True)
class DependentsReport:
    """Strategies affected by replacing or removing an approved plugin."""

    unresolvable: tuple[tuple[str, str, str], ...] = ()
    resolvable: tuple[tuple[str, str], ...] = ()
    pending_approvals: tuple[tuple[str, str], ...] = ()

    @property
    def dependent_names(self) -> set[str]:
        return {name for name, _alias, _reason in self.unresolvable}


def _pending_pins(conn: sqlite3.Connection, name: str) -> tuple[tuple[str, str], ...]:
    pinned: list[tuple[str, str]] = []
    for row in conn.execute(
            "SELECT id,payload_json FROM approval_requests "
            "WHERE kind='plugin' AND status='pending'"):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        refs = payload.get("indicator_deps", {})
        if isinstance(refs, dict):
            for alias, ref in refs.items():
                if isinstance(alias, str) and isinstance(ref, dict) and ref.get("plugin") == name:
                    pinned.append((str(row["id"]), alias))
    return tuple(sorted(pinned))


def enumerate_dependents(conn: sqlite3.Connection, plugins_root: Path, name: str, *,
                         settings, replacement_meta=None) -> DependentsReport:
    """Calculate which admitted strategies need ``name`` without mutating state."""
    result = tools_plugin_loader.approved_plugins(conn, plugins_root, settings=settings)
    candidates = [meta for meta in result.phase1_metas if meta.kind == "strategy"
                  and any(ref.plugin == name for ref in meta.indicators)]
    metas = [meta for meta in result.inventory.metas if meta.name != name]
    if replacement_meta is not None:
        metas = [meta for meta in metas if meta.name != replacement_meta.name]
        metas.append(replacement_meta)
    inventory = ApprovedInventory(root=result.inventory.root, metas=tuple(metas))
    broken: list[tuple[str, str, str]] = []
    working: list[tuple[str, str]] = []
    for meta in candidates:
        try:
            resolve_indicator_deps(meta, inventory, settings=settings, pin_mode="require")
        except IndicatorResolutionError as exc:
            broken.append((meta.name, exc.alias or "-", exc.reason))
        else:
            for ref in meta.indicators:
                if ref.plugin == name:
                    working.append((meta.name, ref.alias))
    return DependentsReport(tuple(sorted(broken)), tuple(sorted(working)),
                            _pending_pins(conn, name))
