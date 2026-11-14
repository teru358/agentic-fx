"""履歴守りの導入時の移行 manifest (承認 15 件の追跡表) の形と検証。

記録先は repository root 基準の `data/history-guard-migration/manifest-2026-11-11.json`。
書き手は運用担当者で、gate や worker は書かない。この module は形と受入条件の検証だけを
持ち、manifest を書かない。DB と照合する検証 (`verify_against_db`) は読取のみ。

版は `schema_version` で分かれる。1 は従来どおりの判定を保ち (従来受理していたものは全て
受理する)、厳格化は 2 だけに課す。2 は各 entry に `audit_ref` (非配備版の評価 report への
相対 path または null) を足した形で、report の内容とも照合する。
"""
from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import stat
from datetime import datetime, timedelta
from pathlib import Path

from agentic_fx.backtest import history_evidence
from agentic_fx.backtest import history_health as hh

MANIFEST_RELPATH = Path("data") / "history-guard-migration" / "manifest-2026-11-11.json"
SCHEMA_VERSION = 1
SCHEMA_VERSION_V2 = 2

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
# v2 で受理する評価不能の固定理由。v1 の集合 (`UNEVALUABLE_REASONS`) は広げない。
UNEVALUABLE_REASONS_V2 = UNEVALUABLE_REASONS | frozenset({
    "artifact_missing", "hash_mismatch", "invalid_snapshot", "dependency_changed",
    "unsupported_version", "sandbox_unavailable", "timeout", "worker_failed",
    "resource_unavailable", "db_busy", "interrupted"})
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


def _validate_v1(manifest: object) -> list[str]:
    """版 1 の形と受入条件の問題の一覧 (空なら満たしている)。DB は見ない。

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


def _verify_entry_v1(entry: dict, conn: sqlite3.Connection, no_bars_path: Path) -> list[str]:
    """1 行ぶんの DB 照合 (版 1 と、版 2 の配備中の行で共通)。"""
    out: list[str] = []
    label = f"#{entry['old_approval_id']}"
    old = conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                       (entry["old_approval_id"],)).fetchone()
    if old is None:
        out.append(f"{label}: the old approval does not exist")
        return out
    payload = json.loads(old[0])
    if (payload.get("name"), payload.get("artifact_hash"), payload.get("content_hash")) != (
            entry["name"], entry["artifact_hash"], entry["content_hash"]):
        out.append(f"{label}: name / hashes differ from the old approval")
    new_id = entry["new_reevaluation_approval_id"]
    if new_id is None:
        if entry["human_decision"] not in ("pending", "not_applicable"):
            out.append(f"{label}: a decision without a new approval")
        return out
    row = conn.execute("SELECT status, payload_json FROM approval_requests WHERE id=?",
                       (new_id,)).fetchone()
    if row is None:
        out.append(f"{label}: the new approval #{new_id} does not exist")
        return out
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


def _verify_v1(manifest: dict, conn: sqlite3.Connection, *, no_bars_path: Path) -> list[str]:
    """DB と照合する (読取のみ)。evaluated の新 id が、その版自身の ready 証跡つきの
    再評価の依頼であること、人間の決定が DB の状態と合っていることを確かめる。"""
    out = _validate_v1(manifest)
    if out:
        return out
    for entry in manifest["entries"]:
        out += _verify_entry_v1(entry, conn, no_bars_path)
    return out


# ---- 版 2 --------------------------------------------------------------------------

_V2_ENTRY_KEYS = _ENTRY_KEYS | {"audit_ref"}
REPORT_FORMAT = 1
_MAX_REPORT_BYTES = 4 * 1024 * 1024
_REPORT_SCOPES = ("in_sample", "holdout")
_REPORT_PASS, _REPORT_FAIL, _REPORT_UNEVALUABLE = "pass", "fail", "unevaluable"
_RESULT_STATUS_OF = {_REPORT_PASS: "evaluated", _REPORT_FAIL: "evaluated",
                     _REPORT_UNEVALUABLE: "unevaluable"}


def _aware_utc(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _audit_ref_problem(value: object) -> str | None:
    """`audit_ref` は null か、`.json` で終わる相対 path (絶対・`..`・空の成分を拒否)。"""
    if value is None:
        return None
    if not isinstance(value, str) or not value or not value.endswith(".json"):
        return "audit_ref must be null or a relative .json path"
    if value.startswith("/") or "\\" in value or "\x00" in value:
        return "audit_ref must be a relative path inside the report root"
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "audit_ref must be a relative path inside the report root"
    return None


def _entry_problems_v2(entry: object, expected: tuple) -> list[str]:
    approval_id, name, prefix, deployed = expected
    label = f"#{approval_id}"
    if not isinstance(entry, dict) or set(entry) != _V2_ENTRY_KEYS:
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
    problem = _audit_ref_problem(entry["audit_ref"])
    if problem is not None:
        out.append(f"{label}: {problem}")
    new_id, reason = entry["new_reevaluation_approval_id"], entry["unevaluable_reason"]
    if deployed:
        if entry["phase"] != "1a":
            out.append(f"{label}: a deployed version belongs to phase 1a")
        if entry["audit_ref"] is not None:
            out.append(f"{label}: a phase 1a row has no audit_ref")
        if status == "evaluated":
            if type(new_id) is not int or reason is not None:
                out.append(f"{label}: evaluated needs a new approval id and no reason")
        else:
            if new_id is not None:
                out.append(f"{label}: only an evaluated entry has a new approval id")
    else:
        if entry["phase"] != "1b":
            out.append(f"{label}: a non-deployed version belongs to phase 1b")
        if new_id is not None:
            out.append(f"{label}: a non-deployed version must not point at a new approval")
        if entry["human_decision"] != "not_applicable" or entry["decided_at"] is not None:
            out.append(f"{label}: a non-deployed version has no human decision "
                       "(not_applicable, no decided_at)")
        if status in ("evaluated", "unevaluable") and not _nonempty_str(entry["audit_ref"]):
            out.append(f"{label}: audit_ref is required")
        if status == "evaluated" and reason is not None:
            out.append(f"{label}: evaluated has no reason")
    if status == "unevaluable":
        if reason not in UNEVALUABLE_REASONS_V2:
            out.append(f"{label}: unevaluable needs a fixed reason")
    elif reason is not None:
        out.append(f"{label}: only an unevaluable entry has a reason")
    if status in ("evaluated", "unevaluable"):
        if not _aware_utc(entry["evaluated_at"]):
            out.append(f"{label}: evaluated_at must be an aware UTC time")
        if not _nonempty_str(entry["evidence_ref"]):
            out.append(f"{label}: evidence_ref is required")
    else:
        for key in ("evaluated_at", "evidence_ref", "audit_ref", "unevaluable_reason"):
            if entry[key] is not None:
                out.append(f"{label}: {key} must be null until the entry is evaluated")
    if entry["decided_at"] is not None and not _aware_utc(entry["decided_at"]):
        out.append(f"{label}: decided_at must be an aware UTC time")
    if entry["human_decision"] in ("approved", "rejected") and entry["decided_at"] is None:
        out.append(f"{label}: a decided entry needs decided_at")
    return out


def _validate_v2(manifest: object) -> list[str]:
    if not isinstance(manifest, dict) or set(manifest) != _TOP_KEYS:
        return ["the manifest must have exactly the specified top-level fields"]
    out: list[str] = []
    if manifest["schema_version"] != SCHEMA_VERSION_V2 or isinstance(
            manifest["schema_version"], bool):
        out.append("schema_version must be 2")
    for key in ("base_commit", "backup_id", "recorded_by"):
        if not _nonempty_str(manifest[key]):
            out.append(f"{key} is required")
    for key in ("created_at", "updated_at"):
        if not _aware_utc(manifest[key]):
            out.append(f"{key} must be an aware UTC time")
    entries = manifest["entries"]
    if not isinstance(entries, list) or len(entries) != len(EXPECTED_ENTRIES):
        return out + ["entries must hold exactly the 15 old approvals"]
    ids = [e.get("old_approval_id") if isinstance(e, dict) else None for e in entries]
    if sorted(map(str, ids)) != sorted(str(e[0]) for e in EXPECTED_ENTRIES) or len(set(ids)) != 15:
        return out + ["entries must be exactly the 15 old approvals, with no duplicate or gap"]
    by_id = {e["old_approval_id"]: e for e in entries}
    for expected in EXPECTED_ENTRIES:
        out += _entry_problems_v2(by_id[expected[0]], expected)
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
    refs = [e["audit_ref"] for e in entries if e["audit_ref"] is not None]
    if len(refs) != len(set(refs)):
        out.append("two entries point at the same audit report")
    return out


def validate_manifest(manifest: object) -> list[str]:
    """形と受入条件の問題の一覧 (空なら満たしている)。DB は見ない。

    `schema_version` で判定を分ける。整数の 2 だけが `audit_ref` を持つ厳格な形
    (`_validate_v2`)。それ以外は従来の判定 (`_validate_v1`) のまま (版が 1 でなければ従来どおり
    不合格)。"""
    version = manifest.get("schema_version") if isinstance(manifest, dict) else None
    if type(version) is int and version == SCHEMA_VERSION_V2:
        return _validate_v2(manifest)
    return _validate_v1(manifest)


def convert_to_v2(manifest: dict) -> tuple[dict | None, list[str]]:
    """版 1 の manifest から版 2 を作る (新しい dict を返し、元は変えない・書かない)。

    各 entry に `audit_ref: null` を足すだけで、時刻・根拠・決定は補作しない。版 2 の制約を
    満たさない行があれば変換を止め、不足を報告する (None と問題の一覧)。"""
    problems = _validate_v1(manifest)
    if problems:
        return None, problems
    converted = copy.deepcopy(manifest)
    converted["schema_version"] = SCHEMA_VERSION_V2
    for entry in converted["entries"]:
        entry["audit_ref"] = None
    problems = _validate_v2(converted)
    return (None, problems) if problems else (converted, [])


def _read_report(report_root: Path, audit_ref: str) -> tuple[dict | None, str | None]:
    """root の中の通常ファイルだけを、symlink を追わずに上限つきで読む。"""
    parts = audit_ref.split("/")
    try:
        fd = os.open(str(report_root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None, "the report root is not a readable directory"
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError:
                return None, "the report path leaves the report root or does not exist"
            os.close(fd)
            fd = nxt
        try:
            st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None, "the report is missing"
        except OSError:
            return None, "the report cannot be inspected"
        if not stat.S_ISREG(st.st_mode):
            return None, "the report is not a regular file (a symlink is refused)"
        if st.st_size > _MAX_REPORT_BYTES:
            return None, "the report is too large"
        try:
            rfd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except OSError:
            return None, "the report cannot be opened"
        try:
            data = os.read(rfd, _MAX_REPORT_BYTES + 1)
        finally:
            os.close(rfd)
    finally:
        os.close(fd)
    try:
        report = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "the report is not valid JSON"
    if not isinstance(report, dict):
        return None, "the report is not a JSON object"
    return report, None


def _pins_view(pins: dict) -> list:
    return [{"alias": a, "plugin": r.get("plugin"), "content_hash": r.get("content_hash")}
            for a, r in sorted(pins.items()) if isinstance(r, dict)]


def _report_problems(entry: dict, report: dict, ref: str, no_bars_path: Path) -> list[str]:
    """report が、その行の版・時刻・結果・理由と一致するか (実行時の記録整合)。"""
    out: list[str] = []
    label = f"#{entry['old_approval_id']}"

    def bad(text: str) -> None:
        out.append(f"{label}: {text}")

    if report.get("report_format") != REPORT_FORMAT:
        bad("the report format is not supported")
    if (report.get("old_approval_id"), report.get("name"), report.get("artifact_hash"),
            report.get("content_hash")) != (entry["old_approval_id"], entry["name"],
                                            entry["artifact_hash"], entry["content_hash"]):
        bad("the report is for another version")
    if report.get("dependencies") != _pins_view(entry["dependency_pins"]):
        bad("the report's dependencies differ from the recorded pins")
    if Path(ref).stem != report.get("trial_id"):
        bad("the report's trial id differs from its file name")
    result = report.get("result") if isinstance(report.get("result"), dict) else {}
    overall, reason = result.get("overall"), result.get("reason")
    if overall not in _RESULT_STATUS_OF:
        bad("the report has no valid overall result")
        return out
    if result.get("result_status") != _RESULT_STATUS_OF[overall]:
        bad("the report's result_status contradicts its overall result")
    if _RESULT_STATUS_OF[overall] != entry["result_status"]:
        bad("the recorded result_status differs from the report")
    if overall == _REPORT_UNEVALUABLE:
        if reason != entry["unevaluable_reason"] or reason not in UNEVALUABLE_REASONS_V2:
            bad("the recorded reason differs from the report")
    elif entry["unevaluable_reason"] is not None:
        bad("an evaluated entry has a reason")
    evaluation = report.get("evaluation") if isinstance(report.get("evaluation"), dict) else {}
    started = evaluation.get("started_at")
    if not (_aware_utc(started) and _aware_utc(entry["evaluated_at"])
            and datetime.fromisoformat(started) == datetime.fromisoformat(entry["evaluated_at"])):
        bad("the recorded evaluation time differs from the report")
    evidence_ref = entry["evidence_ref"]
    if not (isinstance(evidence_ref, str) and evidence_ref.startswith("#")
            and evidence_ref[1:] in report):
        bad("evidence_ref must point at a section of the report")
    rows = report.get("rows") if isinstance(report.get("rows"), list) else []
    if any(not isinstance(r, dict) or r.get("db_run_id") is not None for r in rows):
        bad("a report row claims a database run")
    if overall != _REPORT_PASS:
        return out
    axes = report.get("axes") if isinstance(report.get("axes"), dict) else {}
    pairs = evaluation.get("pairs")
    if not (isinstance(pairs, list) and pairs and all(isinstance(p, str) for p in pairs)):
        bad("a passing report needs its evaluated pairs")
        return out
    if (axes.get("execution") or {}).get("status") != "completed":
        bad("a passing report must record a completed run")
    floor = axes.get("floor") if isinstance(axes.get("floor"), dict) else {}
    for scope in _REPORT_SCOPES:
        scope_view = floor.get(scope) if isinstance(floor.get(scope), dict) else {}
        per_pair = scope_view.get("pairs") if isinstance(scope_view.get("pairs"), dict) else {}
        if scope_view.get("result") != "pass" or set(per_pair) != set(pairs) or any(
                (v or {}).get("result") != "pass" for v in per_pair.values()):
            bad(f"a passing report needs every pair of the {scope} scope to pass")
    if {(r.get("scope"), r.get("pair")) for r in rows if isinstance(r, dict)} != {
            (s, p) for s in _REPORT_SCOPES for p in pairs}:
        bad("a passing report needs a row for every pair and scope")
    if axes.get("unexecuted") != []:
        bad("a passing report has unexecuted scopes")
    if report.get("changed_during_evaluation") != []:
        bad("a passing report records a change during the evaluation")
    history = axes.get("history") if isinstance(axes.get("history"), dict) else {}
    dataset = evaluation.get("dataset") if isinstance(evaluation.get("dataset"), dict) else {}
    if history.get("result") != "ready_verified" or history.get("evidence_check") != "ok":
        bad("a passing report needs a verified ready evidence")
    if not _nonempty_str(history.get("epoch")) or history.get("epoch") != evaluation.get(
            "history_epoch"):
        bad("a passing report needs the history epoch it ran under")
    evidence = history.get("evidence")
    if not isinstance(evidence, dict) or evidence.get("dataset") != {
            "source": dataset.get("source"), "base_interval": dataset.get("base_interval")} \
            or evidence.get("pairs") != pairs or evidence.get("verdict") != "ready":
        bad("the ready evidence does not belong to this evaluation")
    return out


def evidence_currently_valid(report: dict, *, no_bars_path: Path):
    """report の ready 証跡が、現在の方針に今も合うか (記録の整合とは別の問い)。

    後日の方針変更で落ちても歴史 report は無効にならない。再評価済みの事実や rollback の
    許可と混同しないこと。"""
    history = (report.get("axes") or {}).get("history") or {}
    evaluation = report.get("evaluation") or {}
    dataset = evaluation.get("dataset") or {}
    return history_evidence.verify_evidence(
        history.get("evidence"), eval_source=dataset.get("source"),
        base_interval=dataset.get("base_interval"), pairs=evaluation.get("pairs"),
        no_bars_path=no_bars_path)


def _verify_v2(manifest: dict, conn: sqlite3.Connection, *, no_bars_path: Path,
               report_root: Path | None) -> list[str]:
    out = _validate_v2(manifest)
    if out:
        return out
    for entry in manifest["entries"]:
        label = f"#{entry['old_approval_id']}"
        if entry["deployed_at_inventory"]:
            out += _verify_entry_v1(entry, conn, no_bars_path)
            continue
        old = conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                           (entry["old_approval_id"],)).fetchone()
        if old is None:
            out.append(f"{label}: the old approval does not exist")
            continue
        try:
            payload = json.loads(old[0])
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            out.append(f"{label}: the old approval payload is unreadable")
            continue
        if (payload.get("name"), payload.get("artifact_hash"), payload.get("content_hash")) != (
                entry["name"], entry["artifact_hash"], entry["content_hash"]):
            out.append(f"{label}: name / hashes differ from the old approval")
        pins = payload.get("indicator_deps")
        if (pins if pins is not None else {}) != entry["dependency_pins"]:
            out.append(f"{label}: dependency pins differ from the old approval")
        ref = entry["audit_ref"]
        if ref is None:
            continue
        if report_root is None:
            out.append(f"{label}: report_root is required to check audit_ref")
            continue
        report, problem = _read_report(Path(report_root), ref)
        if problem is not None:
            out.append(f"{label}: {problem}")
            continue
        out += _report_problems(entry, report, ref, no_bars_path)
    return out


def verify_against_db(manifest: dict, conn: sqlite3.Connection, *, no_bars_path: Path,
                      report_root: Path | None = None) -> list[str]:
    """DB と照合する (読取のみ)。版 1 は従来の照合のまま (`report_root` は見ない)。

    版 2 は、配備中の行を従来と同じに照合し、非配備の行は旧承認 payload・pin・`audit_ref` が
    指す report (`report_root` の中の通常ファイルだけ) と照合する。"""
    version = manifest.get("schema_version") if isinstance(manifest, dict) else None
    if type(version) is int and version == SCHEMA_VERSION_V2:
        return _verify_v2(manifest, conn, no_bars_path=no_bars_path, report_root=report_root)
    return _verify_v1(manifest, conn, no_bars_path=no_bars_path)
