"""Initial setup confirmation record, stored independently from runtime state."""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from agentic_fx.setup.checks import CheckReason
from agentic_fx.ops_setup.interactive import REASON_CODES as OPS_ENABLE_REASON_CODES

_IDENT = re.compile(r"^[a-z0-9_.-]{1,64}$")
_TOP_LEVEL = frozenset({"version", "completed_at", "mode", "settings_schema_version", "price", "llm_trade", "llm_improve", "landlock", "bridge_plan", "history", "indicators", "api", "ops", "isolation", "linger", "start", "unit"})
_REQUIRED_TOP_LEVEL = _TOP_LEVEL - {"landlock"}
# 検査の語彙 (CheckReason) に、記録だけが持つ語を足したもの。
_RECORD_ONLY_REASONS = frozenset({
    "ok", "unknown", "auth_failed", "socket_missing", "timeout", "not_ready",
    "journal_unavailable", "journal_format_invalid", "admission_line_timeout",
    "landlock_abi_too_old", "sandbox_unavailable", "landlock_create_failed",
    "landlock_restrict_failed", "setup_lease_invalid", "disabled", "invocation_id",
    "cursor", "unit_enable_failed", "unit_start_failed", "service_not_active",
    "instance_lock_busy", "enable_declined", "start_declined", "not_cli_backend", "declined"})
REASON_CODES = frozenset(reason.value for reason in CheckReason) | _RECORD_ONLY_REASONS | OPS_ENABLE_REASON_CODES
_COMPOSITE_REASON = re.compile(r"(?:feature_config_unsafe|feature_probe_failed):[a-z_]{1,32}\Z")

def _reason(value: object) -> bool:
    return isinstance(value, str) and (value in REASON_CODES or _COMPOSITE_REASON.fullmatch(value) is not None)

@dataclass(frozen=True, slots=True)
class SetupRecord:
    data: dict[str, Any]

@dataclass(frozen=True, slots=True)
class SetupRecordRead:
    state: str
    record: SetupRecord | None = None

def setup_record_path(root: Path) -> Path:
    return Path(root) / "data" / "state" / "setup.json"

def _in(value: object, allowed: "set[str] | frozenset[str]") -> bool:
    return isinstance(value, str) and value in allowed

def _timestamp(value: object) -> bool:
    if not isinstance(value, str): return False
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError: return False
    return parsed.tzinfo is not None

def _keys(value: object, allowed: set[str], required: set[str]) -> bool:
    return isinstance(value, Mapping) and required <= value.keys() and set(value) <= allowed

def _result(value: object, results: set[str]) -> bool:
    return (_keys(value, {"result", "at", "reason_code"}, {"result"}) and _in(value["result"], results)
            and ("at" not in value or _timestamp(value["at"]))
            and ("reason_code" not in value or _reason(value["reason_code"])))

def _safe_value(value: Any) -> bool:
    """Conservative primitive guard retained for callers validating record values."""
    if value is None or isinstance(value, (bool, int)): return True
    if not isinstance(value, str) or "/" in value or "\\" in value: return False
    return _timestamp(value) or _reason(value) or bool(_IDENT.fullmatch(value))

def _valid(data: object) -> bool:
    if not isinstance(data, dict) or not (_REQUIRED_TOP_LEVEL <= set(data) <= _TOP_LEVEL): return False
    if (not isinstance(data["version"], int) or isinstance(data["version"], bool) or data["version"] < 1 or not _timestamp(data["completed_at"]) or not _in(data["mode"], {"interactive", "non-interactive"}) or not isinstance(data["settings_schema_version"], int) or isinstance(data["settings_schema_version"], bool)): return False
    if not all(_result(data[name], {"ok", "failed", "unverified", "planned"}) for name in ("price", "llm_trade", "llm_improve")): return False
    if "landlock" in data and not _result(data["landlock"], {"ok", "failed"}): return False
    if not _in(data["bridge_plan"], {"connected", "planned", "unused"}): return False
    if not _keys(data["history"], {"source"}, {"source"}) or not isinstance(data["history"]["source"], str) or not _IDENT.fullmatch(data["history"]["source"]): return False
    if not _keys(data["indicators"], {"installed"}, {"installed"}) or not isinstance(data["indicators"]["installed"], int) or isinstance(data["indicators"]["installed"], bool) or data["indicators"]["installed"] < 0: return False
    unit = data["unit"]
    if not (_in(unit, {"installed", "placed_not_reloaded", "not_installed"}) or (_keys(unit, {"installed", "launch"}, {"installed"}) and isinstance(unit["installed"], bool) and ("launch" not in unit or unit["launch"] == "direct"))): return False
    api = data["api"]
    if not _keys(api, {"enabled", "result", "at", "reason_code"}, {"enabled", "result"}) or not isinstance(api["enabled"], bool) or not _result({key: api[key] for key in api if key != "enabled"}, {"ok", "failed", "disabled", "unchecked"}): return False
    if not _result(data["start"], {"started", "pending", "skipped"}): return False
    linger = data["linger"]
    if not _keys(linger, {"state", "at", "reason_code"}, {"state"}) or not _in(linger["state"], {"owned_by_wizard", "already_enabled", "declined", "failed", "not_checked"}) or ("at" in linger and not _timestamp(linger["at"])) or ("reason_code" in linger and not _reason(linger["reason_code"])): return False
    isolation = data["isolation"]
    if not _keys(isolation, {"admitted", "at", "reason_code"}, {"admitted"}) or not _in(isolation["admitted"], {"yes", "no", "unknown"}) or ("at" in isolation and not _timestamp(isolation["at"])) or ("reason_code" in isolation and not _reason(isolation["reason_code"])): return False
    if not isinstance(data["ops"], Mapping): return False
    for feature, item in data["ops"].items():
        if not isinstance(feature, str) or not _IDENT.fullmatch(feature) or not _keys(item, {"result", "stage", "at", "reason_code", "ready"}, {"result"}) or not _in(item["result"], {"enabled", "failed", "later"}) or ("stage" in item and (not isinstance(item["stage"], str) or not _IDENT.fullmatch(item["stage"]))) or ("at" in item and not _timestamp(item["at"])) or ("reason_code" in item and not _reason(item["reason_code"])) or ("ready" in item and not _in(item["ready"], {"ok", "failed", "unchecked"})): return False
    return True

def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0: raise OSError("short write to setup record")
        offset += written

def write_setup_record(root: Path, record: SetupRecord) -> None:
    """Durably replace the record without exposing a partial JSON document."""
    if not _valid(record.data): raise ValueError("setup record contains an unsupported value")
    path = setup_record_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record.data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".setup.json.")
    try:
        _write_all(fd, payload); os.fsync(fd); os.close(fd); fd = -1
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(parent_fd)
        finally: os.close(parent_fd)
    except BaseException:
        if fd >= 0: os.close(fd)
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise

def read_setup_record(root: Path) -> SetupRecordRead:
    """Read a non-critical record; any malformed or inaccessible data is fail-soft."""
    try: raw = setup_record_path(root).read_text(encoding="utf-8")
    except FileNotFoundError: return SetupRecordRead("missing")
    except Exception: return SetupRecordRead("corrupt")
    try:
        data = json.loads(raw)
        valid = _valid(data)
    except Exception: return SetupRecordRead("corrupt")
    return SetupRecordRead("ok", SetupRecord(data)) if valid else SetupRecordRead("corrupt")
