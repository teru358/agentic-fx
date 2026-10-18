"""Read-only view of one plugin's deployed versions and deploy operations."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from agentic_fx.plugin import loader
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import plugin_deploy_ops as deploy_store

_DIR_NAME_RE = re.compile(r"^[0-9a-f]{64}$")
_MIN_PREFIX = 12


def _live_section(plugins_root: Path, name: str) -> dict:
    live_path = plugins_root / name
    if live_path.is_symlink():
        try:
            target = os.readlink(live_path)
        except OSError:
            target = None
        identity = deploy_store.live_identity(plugins_root, name)
        return {"kind": "symlink", "target": target,
                "content_hash": identity.content_hash if identity else None,
                "artifact_hash": identity.artifact_hash if identity else None}
    if live_path.is_dir():
        identity = deploy_store.live_identity(plugins_root, name)
        return {"kind": "plain", "target": None,
                "content_hash": identity.content_hash if identity else None,
                "artifact_hash": None}
    return {"kind": "absent", "target": None, "content_hash": None, "artifact_hash": None}


def _approved_artifact(approved: dict | None) -> str | None:
    if approved is None:
        return None
    try:
        payload = json.loads(approved["payload_json"])
    except (TypeError, ValueError):
        return None
    artifact = payload.get("artifact_hash") if isinstance(payload, dict) else None
    return artifact if isinstance(artifact, str) else None


def _version_row(conn: sqlite3.Connection, name: str, directory: Path,
                 live_artifact: str | None) -> dict:
    from agentic_fx.plugin import switch

    try:
        content = loader.content_hash(directory)
    except OSError:
        content = None
    latest = approved = None
    if content is not None:
        latest = approvals_store.latest_decision_for_identity(conn, name, content)
        approved = approvals_store.latest_approved_for_identity(conn, name, content)
    expected_artifact = _approved_artifact(approved) or directory.name
    return {
        "artifact_hash": directory.name, "content_hash": content,
        "decision": latest["status"] if latest else None,
        "decided_at": latest["decided_at"] if latest else None,
        "decided_by": latest["decided_by"] if latest else None,
        "approved_at": approved["decided_at"] if approved else None,
        "approved_by": approved["decided_by"] if approved else None,
        "dir_ok": switch._version_dir_hashes_ok(
            directory, content_hash=content, artifact_hash=expected_artifact),
        "is_live": live_artifact == directory.name,
    }


def _op_row(row: dict) -> dict:
    return {key: row[key] for key in (
        "op_id", "op_kind", "phase", "fail_reason", "updated_at", "notify_attempted",
        "from_kind", "from_target", "to_kind", "to_target", "retired_path",
        "subject_content_hash", "target_content_hash", "revoke", "force")}


def collect_versions(conn: sqlite3.Connection, plugins_root: Path, name: str, *,
                     settings=None) -> dict:
    """Gather what ``afx plugin versions`` shows; takes no lock and writes nothing."""
    if not loader.is_canonical_plugin_name(name):
        raise ValueError(f"invalid plugin name: {name!r}")
    plugins_root = Path(plugins_root)
    live = _live_section(plugins_root, name)
    base = plugins_root / ".versions" / name
    versions = []
    if base.is_dir():
        for entry in sorted(base.iterdir()):
            if entry.is_dir() and _DIR_NAME_RE.fullmatch(entry.name):
                versions.append(_version_row(conn, name, entry, live["artifact_hash"]))
    versions.sort(key=lambda v: (not v["is_live"], v["decided_at"] is None,
                                 _negate(v["decided_at"]), v["artifact_hash"]))
    rows = deploy_store.list_by_name(conn, name)
    ops = {
        "open": [_op_row(r) for r in rows if deploy_store.is_open(r)],
        "attention": [_op_row(r) for r in rows if (
            r["phase"] == "foreign"
            or (r["phase"] == "needs_retry" and r["fail_reason"] == "verify_failed"))],
    }
    dependents = None
    if settings is not None:
        try:
            from agentic_fx.plugin.deploy_deps import enumerate_dependents
            report = enumerate_dependents(conn, plugins_root, name, settings=settings)
            dependents = {
                "unresolvable": [list(item) for item in report.unresolvable],
                "resolvable": [list(item) for item in report.resolvable],
            }
        except Exception:  # noqa: BLE001 — a read-only view must not fail on an unreadable inventory
            dependents = None
    return {"name": name, "live": live, "versions": versions,
            "history": deploy_store.plugin_live_history(conn, name),
            "ops": ops, "dependents": dependents}


def _negate(text: str | None) -> tuple:
    """Sort key putting later ISO timestamps first."""
    if text is None:
        return ()
    return tuple(-ord(ch) for ch in text)


def _short(value: str | None, others: list[str]) -> str:
    if not value:
        return "-"
    for width in range(_MIN_PREFIX, len(value) + 1):
        prefix = value[:width]
        if sum(1 for other in others if other.startswith(prefix)) <= 1:
            return prefix
    return value


def render_versions(data: dict) -> list[str]:
    """Text lines for ``collect_versions`` output."""
    name = data["name"]
    live = data["live"]
    all_hashes = [v["artifact_hash"] for v in data["versions"]]
    all_contents = [v["content_hash"] for v in data["versions"] if v["content_hash"]]
    lines = [f"plugin {name}"]
    lines.append("-- live --")
    if live["kind"] == "symlink":
        shown = _short(live["artifact_hash"], all_hashes) if live["artifact_hash"] else "?"
        lines.append(f"symlink -> {live['target']} (version {shown})")
    elif live["kind"] == "plain":
        lines.append("plain directory (legacy; `afx plugin retire` で退避できます)")
    else:
        lines.append("不在")
    lines.append("-- 版 (先頭 = `rollback --to` に使う版、content = `revoke --version` に使う識別) --")
    if not data["versions"]:
        lines.append("(なし)")
    for version in data["versions"]:
        marker = "*" if version["is_live"] else " "
        decision = version["decision"] or "未決定"
        parts = [f"{marker} {_short(version['artifact_hash'], all_hashes)}",
                 f"content={_short(version['content_hash'], all_contents)}",
                 f"決定={decision}"]
        if version["approved_at"]:
            parts.append(f"承認={version['approved_at']} by {version['approved_by']}")
        if version["decision"] == "rejected":
            parts.append(f"却下={version['decided_at']} by {version['decided_by']}")
        parts.append("版dir=" + ("整合" if version["dir_ok"] else "不整合"))
        lines.append("  ".join(parts))
    lines.append("-- 経歴 (live の変遷) --")
    if not data["history"]:
        lines.append("(なし)")
    for event in data["history"]:
        lines.append(f"{event['effective_at']}  {event['kind']}  "
                     f"{_target_text(event['from'])} -> {_target_text(event['to'])}")
    lines.append("-- deploy op --")
    ops = data["ops"]
    if not ops["open"] and not ops["attention"]:
        lines.append("(未完・要注意の op はありません)")
    for row in ops["open"]:
        lines.append("未完 " + _op_text(row))
        lines.append("  同じコマンドの再実行で続きから完遂します。進まなければ daemon を再起動"
                     " (起動時の reconcile) するか reconcile を待ってください")
    for row in ops["attention"]:
        lines.append("要注意 " + _op_text(row))
    lines.append("-- 依存 strategy --")
    deps = data["dependents"]
    if deps is None:
        lines.append("(設定を読めないため列挙していません)")
    elif not deps["unresolvable"] and not deps["resolvable"]:
        lines.append("(なし)")
    else:
        for strategy, alias in deps["resolvable"]:
            lines.append(f"{strategy} ({alias}) 置換後も解決できる")
        for strategy, alias, reason in deps["unresolvable"]:
            lines.append(f"{strategy} ({alias}) 外すと解決できない: {reason}")
    return lines


def _target_text(value: str | None) -> str:
    if value is None:
        return "-"
    return value.rsplit("/", 1)[-1][:12] if "/" in value and ".versions" in value else value


def _op_text(row: dict) -> str:
    detail = row["phase"] + (f"/{row['fail_reason']}" if row["fail_reason"] else "")
    flags = "".join((" --revoke" if row["revoke"] else "", " --force" if row["force"] else ""))
    subject = row["subject_content_hash"]
    return (f"op_id={row['op_id']} {row['op_kind']}{flags} {detail} "
            f"updated_at={row['updated_at']} "
            f"subject={subject[:12] if subject else '-'} "
            f"to={_target_text(row['to_target'] or row['retired_path'])} "
            f"通知試行済み={'はい' if row['notify_attempted'] else 'いいえ'}")
