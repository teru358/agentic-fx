"""履歴守りの導入時の移行 manifest (承認 15 件の追跡表) の形と検証。

記録先は repository root 基準の `data/history-guard-migration/manifest-2026-11-11.json`。
書き手は運用担当者で、gate や worker は書かない。この module は形と受入条件の検証だけを
持ち、manifest を書かない。DB と照合する検証 (`verify_against_db`) は読取のみ。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from agentic_fx.backtest import history_evidence
from agentic_fx.backtest import history_health as hh

MANIFEST_RELPATH = Path("data") / "history-guard-migration" / "manifest-2026-11-11.json"
SCHEMA_VERSION = 1

# 09-04 以降の承認 15 件: (承認 id, 名前, artifact hash 先頭, 配備中か)。
EXPECTED_ENTRIES = (
    (32, "ichimoku_rsi_cloud_reentry", "3b35b61fa798", True),
    (92, "ichimoku_rsi_cloud_reentry", "3b35b61fa798", True),
    (65, "volume_close_pressure", "127825cc55df", True),
    (94, "volume_close_pressure", "127825cc55df", True),
    (73, "asym_shock_breakout", "0b5ec2c155db", True),
    (89, "asym_shock_breakout", "0b5ec2c155db", True),
    (74, "close_pressure_followthrough", "ee670b8630f6", True),
    (91, "close_pressure_followthrough", "ee670b8630f6", True),
    (21, "sma_cross_10_30", "395e2ce2d065", False),
    (23, "bb_ema_breakout", "eb0bd5257957", False),
    (24, "rsi_bb_reversion_200", "dc59a62892ca", False),
    (25, "rsi6_bb200_rr2", "8ad4942e2d29", False),
    (39, "asym_shock_breakout", "1483bc7e63ea", False),
    (71, "close_pressure_followthrough", "12d14addcf7c", False),
    (72, "close_pressure_followthrough", "a74732d7d316", False),
)

RESULT_STATUSES = ("pending", "evaluated", "unevaluable", "deferred")
HUMAN_DECISIONS = ("pending", "approved", "rejected", "not_applicable")
UNEVALUABLE_REASONS = frozenset({hh.HISTORY_STALE, hh.HISTORY_GAP, hh.HISTORY_UNAVAILABLE,
                                 "insufficient_trades", "reference_history_missing",
                                 "history_source_unconfigured", "changed_during_evaluation",
                                 "failed"})
_TOP_KEYS = frozenset({"schema_version", "base_commit", "backup_id", "created_at",
                       "updated_at", "recorded_by", "entries"})
_ENTRY_KEYS = frozenset({
    "old_approval_id", "name", "artifact_hash", "content_hash", "dependency_pins",
    "deployed_at_inventory", "phase", "result_status", "new_reevaluation_approval_id",
    "unevaluable_reason", "evaluated_at", "evidence_ref", "human_decision", "decided_at"})
_HEX64 = re.compile(r"[0-9a-f]{64}")


def manifest_path(root: Path) -> Path:
    return Path(root) / MANIFEST_RELPATH


def _aware(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _entry_problems(entry: object, expected: tuple) -> list[str]:
    approval_id, name, prefix, deployed = expected
    label = f"#{approval_id}"
    if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
        return [f"{label}: the entry must have exactly the specified fields"]
    out: list[str] = []
    if entry["old_approval_id"] != approval_id or isinstance(entry["old_approval_id"], bool):
        out.append(f"{label}: old_approval_id differs")
    if entry["name"] != name:
        out.append(f"{label}: name differs")
    for key in ("artifact_hash", "content_hash"):
        if not isinstance(entry[key], str) or not _HEX64.fullmatch(entry[key]):
            out.append(f"{label}: {key} must be the full 64-character hash")
    if (isinstance(entry["artifact_hash"], str)
            and not entry["artifact_hash"].startswith(prefix)):
        out.append(f"{label}: artifact_hash does not start with {prefix}")
    if not isinstance(entry["dependency_pins"], dict):
        out.append(f"{label}: dependency_pins must be an object")
    if entry["deployed_at_inventory"] is not deployed:
        out.append(f"{label}: deployed_at_inventory differs from the inventory")
    status = entry["result_status"]
    if status not in RESULT_STATUSES:
        out.append(f"{label}: result_status is unknown")
    if entry["human_decision"] not in HUMAN_DECISIONS:
        out.append(f"{label}: human_decision is unknown")
    if deployed:
        if entry["phase"] != "1a":
            out.append(f"{label}: a deployed version belongs to phase 1a")
    else:
        if entry["phase"] != "1b" or status != "deferred":
            out.append(f"{label}: a non-deployed version stays phase 1b / deferred")
        if entry["new_reevaluation_approval_id"] is not None:
            out.append(f"{label}: a non-deployed version must not point at a new approval")
    new_id, reason = entry["new_reevaluation_approval_id"], entry["unevaluable_reason"]
    if status == "evaluated":
        if type(new_id) is not int or reason is not None:
            out.append(f"{label}: evaluated needs a new approval id and no reason")
    else:
        if new_id is not None:
            out.append(f"{label}: only an evaluated entry has a new approval id")
        if status == "unevaluable" and reason not in UNEVALUABLE_REASONS:
            out.append(f"{label}: unevaluable needs a fixed reason")
        if status != "unevaluable" and reason is not None:
            out.append(f"{label}: only an unevaluable entry has a reason")
    if deployed and status in ("evaluated", "unevaluable"):
        if not _aware(entry["evaluated_at"]):
            out.append(f"{label}: evaluated_at must be an aware UTC time")
        if not isinstance(entry["evidence_ref"], str) or not entry["evidence_ref"].strip():
            out.append(f"{label}: evidence_ref is required")
    if entry["human_decision"] in ("approved", "rejected") and not _aware(entry["decided_at"]):
        out.append(f"{label}: a decided entry needs decided_at")
    return out


def validate_manifest(manifest: object) -> list[str]:
    """形と受入条件の問題の一覧 (空なら満たしている)。DB は見ない。

    旧承認 15 件が一致し、配備中 8 行すべてに新しい承認 id か固定の評価不能理由と評価
    時刻・根拠参照があり、同じ版の旧承認 2 行が同じ結果を参照し、非配備 7 行が
    未評価 (phase 1b / deferred) のまま残っていること。"""
    if not isinstance(manifest, dict) or set(manifest) != _TOP_KEYS:
        return ["the manifest must have exactly the specified top-level fields"]
    out: list[str] = []
    if manifest["schema_version"] != SCHEMA_VERSION:
        out.append("schema_version must be 1")
    for key in ("base_commit", "backup_id", "recorded_by"):
        if not isinstance(manifest[key], str) or not manifest[key].strip():
            out.append(f"{key} is required")
    for key in ("created_at", "updated_at"):
        if not _aware(manifest[key]):
            out.append(f"{key} must be an aware UTC time")
    entries = manifest["entries"]
    if not isinstance(entries, list) or len(entries) != len(EXPECTED_ENTRIES):
        return out + ["entries must hold exactly the 15 old approvals"]
    ids = [e.get("old_approval_id") if isinstance(e, dict) else None for e in entries]
    if sorted(map(str, ids)) != sorted(str(e[0]) for e in EXPECTED_ENTRIES) or len(set(ids)) != 15:
        out.append("entries must be exactly the 15 old approvals, with no duplicate or gap")
        return out
    by_id = {e["old_approval_id"]: e for e in entries}
    for expected in EXPECTED_ENTRIES:
        out += _entry_problems(by_id[expected[0]], expected)
    if out:
        return out
    by_version: dict[str, list[dict]] = {}
    for entry in entries:
        if entry["deployed_at_inventory"]:
            by_version.setdefault(entry["artifact_hash"], []).append(entry)
    for versions in by_version.values():
        shape = {(e["result_status"], e["new_reevaluation_approval_id"], e["unevaluable_reason"])
                 for e in versions}
        if len(versions) != 2 or len(shape) != 1:
            out.append(f"{versions[0]['name']}: the two old approvals of one deployed version "
                       "must record the same result")
    return out


def initial_manifest(*, base_commit: str, backup_id: str, recorded_by: str, now: datetime,
                     versions: dict) -> dict:
    """導入の開始時に作る 15 行。`versions` は旧承認 id -> {"artifact_hash", "content_hash",
    "dependency_pins"} (DB の旧承認 payload から運用担当者が写す。完全長)。配備中 8 行は
    1a / pending、非配備 7 行は 1b / deferred で、未評価であることを明示する。"""
    stamp = now.isoformat()
    entries = []
    for approval_id, name, _prefix, deployed in EXPECTED_ENTRIES:
        info = versions[approval_id]
        entries.append({
            "old_approval_id": approval_id, "name": name,
            "artifact_hash": info["artifact_hash"], "content_hash": info["content_hash"],
            "dependency_pins": info.get("dependency_pins", {}),
            "deployed_at_inventory": deployed, "phase": "1a" if deployed else "1b",
            "result_status": "pending" if deployed else "deferred",
            "new_reevaluation_approval_id": None, "unevaluable_reason": None,
            "evaluated_at": None, "evidence_ref": None,
            "human_decision": "pending" if deployed else "not_applicable",
            "decided_at": None})
    return {"schema_version": SCHEMA_VERSION, "base_commit": base_commit,
            "backup_id": backup_id, "created_at": stamp, "updated_at": stamp,
            "recorded_by": recorded_by, "entries": entries}


def load_manifest(root: Path) -> dict:
    return json.loads(manifest_path(root).read_text(encoding="utf-8"))


def verify_against_db(manifest: dict, conn: sqlite3.Connection, *, no_bars_path: Path) -> list[str]:
    """DB と照合する (読取のみ)。evaluated の新 id が、その版自身の ready 証跡つきの
    再評価の依頼であること、人間の決定が DB の状態と合っていることを確かめる。"""
    out = validate_manifest(manifest)
    if out:
        return out
    for entry in manifest["entries"]:
        label = f"#{entry['old_approval_id']}"
        old = conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                           (entry["old_approval_id"],)).fetchone()
        if old is None:
            out.append(f"{label}: the old approval does not exist")
            continue
        payload = json.loads(old[0])
        if (payload.get("name"), payload.get("artifact_hash"), payload.get("content_hash")) != (
                entry["name"], entry["artifact_hash"], entry["content_hash"]):
            out.append(f"{label}: name / hashes differ from the old approval")
        new_id = entry["new_reevaluation_approval_id"]
        if new_id is None:
            if entry["human_decision"] not in ("pending", "not_applicable"):
                out.append(f"{label}: a decision without a new approval")
            continue
        row = conn.execute("SELECT status, payload_json FROM approval_requests WHERE id=?",
                           (new_id,)).fetchone()
        if row is None:
            out.append(f"{label}: the new approval #{new_id} does not exist")
            continue
        new = json.loads(row[1])
        if (new.get("name"), new.get("artifact_hash"), new.get("content_hash"),
                new.get("candidate_origin")) != (entry["name"], entry["artifact_hash"],
                                                 entry["content_hash"], "reevaluation"):
            out.append(f"{label}: the new approval is not this version's re-evaluation")
        check = history_evidence.verify_payload(new, no_bars_path=no_bars_path)
        if not check.ok:
            out.append(f"{label}: the new approval has no valid history evidence")
        decided = {"approved": "approved", "rejected": "rejected"}.get(entry["human_decision"])
        if decided is not None and row[0] != decided:
            out.append(f"{label}: the recorded decision differs from the database")
        if entry["human_decision"] == "pending" and row[0] not in ("pending",):
            out.append(f"{label}: the database already decided this request")
    return out
