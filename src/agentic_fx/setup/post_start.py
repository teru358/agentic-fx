"""Post-placement setup actions that must happen while the service is stopped,
and the observation of the started service (journal cursor, admission line, readiness)."""
from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_fx.ops.client import ApiClient, ClientError
from agentic_fx.ops.contracts import Principal
from agentic_fx.ops_setup.interactive import EnableResult, interactive_enable
from agentic_fx.ops_setup.setup_lease import SetupLease, begin_handoff
from agentic_fx.ops_setup.stages import OpsSetup
from agentic_fx.ops_setup.units import OpsExecNotFound, UnitPaths, resolve_ops_exec
from agentic_fx.store.instance_lock import InstanceAlreadyRunning, acquire_instance_lock
from agentic_fx.store.setup_marker import format_setup_marker, remove_setup_marker


_ADMISSION = re.compile(
    r"^improve isolation: enabled; backend=(?P<backend>[a-z0-9_.-]+) "
    r"admitted=(?P<admitted>yes|no)(?: reason=(?P<reason>[a-z0-9_:.-]+))? "
    r"fingerprint=(?P<fingerprint>[^\s]+)\Z"
)
_HINT = "admission は起動後しばらくかかります。`journalctl --user -u afx | grep 'improve isolation'` で確認してください"


@dataclass(frozen=True)
class OpsPrecheck:
    available: bool
    reason_code: str | None = None
    guidance: str = ""


@dataclass(frozen=True)
class FeatureOutcome:
    feature: str
    result: str
    stage: str | None
    reason_code: str | None
    at: str


@dataclass(frozen=True)
class IsolationObservation:
    admitted: str
    reason_code: str
    method: str
    guidance: str = ""


@dataclass(frozen=True)
class StartOutcome:
    result: str
    reason_code: str | None
    at: str
    active: str = "unchecked"
    api_result: str = "unchecked"
    api_reason_code: str | None = None
    ops: dict[str, str] | None = None
    invocation_id: str | None = None
    journal_cursor: str | None = None


def precheck_ops(root: Path) -> OpsPrecheck:
    """Check the same executable rule used when ops units are installed."""
    del root  # resolution deliberately follows the process environment.
    try:
        resolve_ops_exec(UnitPaths(Path(".")))
    except OpsExecNotFound:
        return OpsPrecheck(False, "ops_exec_not_found",
                           "afx-ops が見つかりません。`afx-ops/` を PATH に置くか "
                           "`afx_ops` を import できる環境にしてください")
    return OpsPrecheck(True)


def handoff_and_enable(root: Path, lease: SetupLease, features: Iterable[str], *,
                       enable: Callable[..., EnableResult] = interactive_enable,
                       precheck: Callable[[Path], OpsPrecheck] = precheck_ops,
                       now: Callable[[], str], hook: Callable[[str], None] | None = None,
                       settings_sha256: str | None = None) -> list[FeatureOutcome]:
    """Hand off ``instance.lock`` once, then enable every requested feature.

    An exception deliberately leaves the handoff marker in place: a later init
    run can safely resume rather than allowing the daemon to start mid-handoff.
    """
    selected = tuple(features)
    if not selected:
        return []
    check = precheck(Path(root))
    if not check.available:
        return [FeatureOutcome(feature, "later", None, check.reason_code or "ops_exec_not_found", now())
                for feature in selected]
    begin_handoff(lease, format_setup_marker("handoff", now(), settings_sha256), hook=hook)
    outcomes: list[FeatureOutcome] = []
    for feature in selected:
        result = enable(Path(root), feature, lease=lease)
        outcomes.append(_feature_outcome(feature, result, now()))
    remove_setup_marker(Path(root), hook=hook)
    return outcomes


def _feature_outcome(feature: str, result: EnableResult, at: str) -> FeatureOutcome:
    if result.rc == 0:
        return FeatureOutcome(feature, "enabled", result.stage, result.reason_code, at)
    return FeatureOutcome(feature, "failed", result.stage, result.reason_code, at)


def enable_and_start(root: Path, *, run: Callable[..., Any] = subprocess.run,
                     confirm: Callable[[str], bool], now: Callable[[], str],
                     api_status: Callable[[], Any] | None = None,
                     api_enabled: bool = True,
                     features: Iterable[str] = (),
                     ops_ready: Callable[[str], bool] | None = None,
                     clock: Callable[[], float] = time.monotonic,
                     sleep: Callable[[float], None] = time.sleep) -> StartOutcome:
    """Enable and start the unit, then independently observe each readiness layer."""
    root = Path(root)
    at = now()
    if not confirm("enable"):
        return StartOutcome("skipped", "enable_declined", at)
    if not _ok(run, ["systemctl", "--user", "enable", "afx.service"]):
        return StartOutcome("pending", "unit_enable_failed", at)
    if not confirm("start"):
        return StartOutcome("skipped", "start_declined", at)
    try:
        lock = acquire_instance_lock(root / "data")
    except InstanceAlreadyRunning:
        return StartOutcome("pending", "instance_lock_busy", at)
    else:
        lock.close()
    previous_invocation = _invocation_id(run)
    cursor = _journal_cursor(run)
    if not _ok(run, ["systemctl", "--user", "start", "afx.service"]):
        return StartOutcome("pending", "unit_start_failed", at)
    invocation_id = _wait_for_new_invocation(run, previous_invocation, clock=clock, sleep=sleep)
    active = _wait_for_active(run, clock=clock, sleep=sleep)
    api_result, api_reason = _wait_for_api(root, api_status, enabled=api_enabled, clock=clock, sleep=sleep)
    ready = ops_ready or _default_ops_ready(root, run)
    ops = {feature: _wait_for_ready(ready, feature, clock=clock, sleep=sleep) for feature in features}
    if active != "ok":
        return StartOutcome("pending", "service_not_active", at, active=active, api_result=api_result,
                            api_reason_code=api_reason, ops=ops, invocation_id=invocation_id,
                            journal_cursor=cursor)
    return StartOutcome("started", None, at, active=active, api_result=api_result,
                        api_reason_code=api_reason, ops=ops, invocation_id=invocation_id,
                        journal_cursor=cursor)


def _ok(run: Callable[..., Any], argv: list[str]) -> bool:
    try:
        return getattr(run(argv, capture_output=True, text=True, timeout=30), "returncode", 1) == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _active_state(run: Callable[..., Any]) -> str:
    try:
        completed = run(["systemctl", "--user", "is-active", "afx.service"],
                        capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "failed"
    state = getattr(completed, "stdout", "").strip()
    if state == "active" and getattr(completed, "returncode", 1) == 0:
        return "ok"
    return "failed" if state == "failed" else "pending"


def _wait_for_active(run: Callable[..., Any], *, clock: Callable[[], float], sleep: Callable[[float], None]) -> str:
    deadline = clock() + 10
    while True:
        state = _active_state(run)
        if state != "pending" or clock() >= deadline:
            return "ok" if state == "ok" else "failed"
        sleep(min(0.5, max(0.0, deadline - clock())))


def _api_observation(root: Path, api_status: Callable[[], Any] | None, *, enabled: bool) -> tuple[str, str | None]:
    if not enabled:
        return "disabled", "disabled"
    if api_status is None:
        api_status = lambda: ApiClient(root, Principal.OPERATOR).request("GET", "/v1/status")
    try:
        api_status()
    except FileNotFoundError:
        return "failed", "socket_missing"
    except ClientError:
        return "failed", "not_ready"
    except (OSError, subprocess.SubprocessError):
        return "failed", "not_ready"
    return "ok", None


def _wait_for_api(root: Path, api_status: Callable[[], Any] | None, *, enabled: bool,
                  clock: Callable[[], float], sleep: Callable[[float], None]) -> tuple[str, str | None]:
    if not enabled:
        return "disabled", "disabled"
    deadline = clock() + 30
    while True:
        result, reason = _api_observation(root, api_status, enabled=True)
        if result == "ok" or clock() >= deadline:
            return result, reason
        sleep(min(2.0, max(0.0, deadline - clock())))


def _wait_for_ready(ready: Callable[[str], bool], feature: str, *, clock: Callable[[], float],
                    sleep: Callable[[float], None]) -> str:
    deadline = clock() + 30
    while True:
        if ready(feature):
            return "ok"
        if clock() >= deadline:
            return "failed"
        sleep(min(2.0, max(0.0, deadline - clock())))


def _invocation_id(run: Callable[..., Any]) -> str | None:
    try:
        completed = run(["systemctl", "--user", "show", "afx.service", "-p", "InvocationID", "--value"],
                        capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    value = getattr(completed, "stdout", "").strip()
    return value if getattr(completed, "returncode", 1) == 0 and value else None


def _wait_for_new_invocation(run: Callable[..., Any], previous: str | None, *,
                             clock: Callable[[], float], sleep: Callable[[float], None]) -> str | None:
    deadline = clock() + 10
    while True:
        current = _invocation_id(run)
        if current and current != previous:
            return current
        if clock() >= deadline:
            return None
        sleep(min(0.5, max(0.0, deadline - clock())))


def _journal_cursor(run: Callable[..., Any]) -> str | None:
    try:
        completed = run(["journalctl", "--user", "-n", "0", "--show-cursor"],
                        capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(completed, "returncode", 1) != 0:
        return None
    for line in reversed((getattr(completed, "stdout", "") or "").splitlines()):
        if line.startswith("-- cursor: "):
            return line.removeprefix("-- cursor: ").strip() or None
    return None


def _default_ops_ready(root: Path, run: Callable[..., Any]) -> Callable[[str], bool]:
    def ready(feature: str) -> bool:
        if not _ok(run, ["systemctl", "--user", "is-active", f"afx-ops@{feature}.service"]):
            return False
        try:
            return OpsSetup(root).status(feature).stage in {"enabled", "installed"}
        except (OSError, ValueError):
            return False
    return ready


_CLI_BACKENDS = frozenset({"claude", "codex", "opencode"})
_JOURNAL_WINDOW = "5000"
_ID_FIELDS = ("_SYSTEMD_INVOCATION_ID", "USER_INVOCATION_ID")


def read_admission_line(run: Callable[..., Any], invocation_id: str | None, *,
                        cursor_fallback: str | None, backend: str,
                        clock: Callable[[], float] = time.monotonic,
                        sleep: Callable[[float], None] = time.sleep,
                        timeout: float = 30) -> IsolationObservation | None:
    """Read only this start's admission line; ``None`` when the backend has no admission.

    The unit's journal is read without an invocation filter, from the pre-start cursor
    onward when one was obtained (the last 5000 entries only when it was not), so that
    the presence of an invocation field is judged over every entry in that window. The
    line is then chosen by invocation id; the cursor alone decides when no entry carries
    such a field or the invocation id could not be obtained.
    """
    if backend not in _CLI_BACKENDS:
        return None
    if invocation_id is None:
        return _read_from_cursor(run, cursor_fallback, backend)
    deadline = clock() + timeout
    while True:
        window = (["--after-cursor", cursor_fallback] if cursor_fallback
                  else ["-n", _JOURNAL_WINDOW])
        journal = _journal(run, ["journalctl", "--user", "-u", "afx.service", "-o", "json",
                                 "--no-pager", *window])
        if journal[0] == "malformed":
            return _unknown("journal_format_invalid", "invocation_id")
        field = next((name for name in _ID_FIELDS if _has_field(journal, name)), None)
        if field is None:
            return _read_from_cursor(run, cursor_fallback, backend)
        result, invalid = _admission(journal, invocation_id, backend, field)
        if result is not None:
            return result
        if invalid:
            return _unknown("journal_format_invalid", "invocation_id")
        if clock() >= deadline:
            return _unknown("admission_line_timeout", "invocation_id")
        sleep(min(2.0, max(0.0, deadline - clock())))


def _read_from_cursor(run: Callable[..., Any], cursor_fallback: str | None,
                      backend: str) -> IsolationObservation:
    if not cursor_fallback:
        return _unknown("journal_unavailable", "cursor")
    cursor = _journal(run, ["journalctl", "--user", "-u", "afx.service", "-o", "json", "--no-pager",
                            "--after-cursor", cursor_fallback])
    result, invalid = _admission(cursor, None, backend, None, method="cursor")
    if result is not None:
        return result
    if cursor[0] == "unavailable":
        return _unknown("journal_unavailable", "cursor")
    return _unknown("journal_format_invalid" if invalid or cursor[0] == "malformed" else "admission_line_timeout", "cursor")


def _journal(run: Callable[..., Any], argv: list[str]) -> tuple[str, list[dict[str, Any]]]:
    try:
        completed = run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unavailable", []
    if getattr(completed, "returncode", 1) != 0:
        return "unavailable", []
    rows: list[dict[str, Any]] = []
    for line in (getattr(completed, "stdout", "") or "").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return "malformed", []
        if not isinstance(row, dict):
            return "malformed", []
        rows.append(row)
    return "ok", rows


def _has_field(journal: tuple[str, list[dict[str, Any]]], field: str) -> bool:
    return journal[0] == "ok" and any(field in row for row in journal[1])


def _admission(journal: tuple[str, list[dict[str, Any]]], invocation_id: str | None,
               backend: str, field: str | None, *, method: str = "invocation_id") -> tuple[IsolationObservation | None, bool]:
    state, rows = journal
    if state != "ok":
        return None, False
    matched: IsolationObservation | None = None
    invalid = False
    for row in rows:
        if invocation_id is not None and field is not None:
            value = row.get(field)
            if value != invocation_id:
                continue
        message = row.get("MESSAGE")
        if not isinstance(message, str):
            continue
        prefix = f"improve isolation: enabled; backend={backend} "
        if not message.startswith(prefix):
            continue
        parsed = _ADMISSION.fullmatch(message)
        if parsed is None:
            invalid = True
            continue
        admitted = parsed.group("admitted")
        reason = parsed.group("reason") if admitted == "no" else method
        matched = IsolationObservation(admitted, reason or "unknown", method)
    return matched, invalid


def _unknown(reason_code: str, method: str) -> IsolationObservation:
    return IsolationObservation("unknown", reason_code, method, _HINT)


def ops_record_values(outcomes: Iterable[FeatureOutcome]) -> dict[str, dict[str, str]]:
    """Map enable results to the bounded setup-record vocabulary without writing it."""
    return {item.feature: {key: value for key, value in {
        "result": item.result, "stage": item.stage, "reason_code": item.reason_code,
        "at": item.at, "ready": "unchecked",
    }.items() if value is not None} for item in outcomes}


def start_record_values(outcome: StartOutcome, *, api_enabled: bool) -> dict[str, object]:
    """Map observations to record fields; callers retain ownership of the write."""
    start = {"result": outcome.result, "at": outcome.at}
    api = {"enabled": api_enabled, "result": outcome.api_result, "at": outcome.at}
    if outcome.reason_code is not None:
        start["reason_code"] = outcome.reason_code
    if outcome.api_reason_code is not None:
        api["reason_code"] = outcome.api_reason_code
    return {"start": start, "api": api}


def ops_ready_record_values(outcomes: Iterable[FeatureOutcome], outcome: StartOutcome) -> dict[str, dict[str, str]]:
    """Merge post-start ready observations into already bounded ops record values."""
    values = ops_record_values(outcomes)
    for feature, ready in (outcome.ops or {}).items():
        if feature in values:
            values[feature]["ready"] = ready
    return values


def isolation_record_values(observation: IsolationObservation | None, *, at: str) -> dict[str, str]:
    """Map the journal observation to the bounded isolation record value.

    ``None`` means the backend has no admission (not a CLI backend); the record then
    carries a fixed ``unknown`` entry with the reason ``not_cli_backend``.
    """
    if observation is None:
        return {"admitted": "unknown", "reason_code": "not_cli_backend", "at": at}
    return {"admitted": observation.admitted, "reason_code": observation.reason_code, "at": at}
