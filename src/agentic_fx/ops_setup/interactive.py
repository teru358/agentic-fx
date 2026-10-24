"""Terminal-facing preflight for ``afx ops <feature> enable``."""
from __future__ import annotations

import fcntl
import getpass
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from agentic_fx.ops.keys import KeySetError, run_keys_command
from agentic_fx.ops_setup.feature_config import InputError, collect, unsafe_reason, write_inputs
from agentic_fx.ops_setup.probe import run_probe
from agentic_fx.ops_setup.setup_lease import (SetupLease, SetupLeaseBusy, SetupLeaseUnsafe,
                                              acquire_setup_lock)
from agentic_fx.ops_setup.stages import InvalidState, OpsSetup
from agentic_fx.ops_setup.systemd import SystemdError
from agentic_fx.ops_setup.units import resolve_ops_exec


class IO(Protocol):
    def isatty(self) -> bool: ...
    def ask(self, label: str, default: str | None = None) -> str | None: ...
    def ask_secret(self, label: str) -> str: ...
    def stdout(self, text: str) -> None: ...
    def stderr(self, text: str) -> None: ...


class ConsoleIO:
    def isatty(self) -> bool:
        return sys.stdin.isatty() and sys.stdout.isatty()

    def ask(self, label: str, default: str | None = None) -> str | None:
        suffix = f" [{default}]" if default is not None else ""
        answer = input(f"{label}{suffix}: ").strip()
        return answer or default

    def ask_secret(self, label: str) -> str:
        return getpass.getpass(f"{label}: ")

    def stdout(self, text: str) -> None:
        print(text)

    def stderr(self, text: str) -> None:
        print(text, file=sys.stderr)


@dataclass(frozen=True)
class EnableResult:
    rc: int
    stage: str
    reason_code: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_code", _normalize_reason_code(self.reason_code))


REASON_CODES = frozenset({
    "ok", "unknown", "setup_aborted", "setup_busy", "setup_input_invalid", "setup_input_required",
    "setup_internal_error", "setup_lease_invalid", "setup_lock_unsafe", "keys_init_failed",
    "afx_running_keys_not_initialized", "feature_probe_timeout", "feature_probe_unreadable",
    "config_dir_missing", "config_dir_invalid", "config_invalid", "secrets_failed", "url_invalid",
    "token_invalid", "startup_internal_error", "probe_not_implemented", "runtime_unavailable",
    "probe_bind_failed", "invalid_stage", "invalid_state", "material_write_failed",
    "ops_exec_not_found", "feature_config_invalid", "principal_key_mismatch", "afx_still_running",
    "marker_corrupt", "marker_foreign", "marker_write_failed", "marker_remove_failed", "ops_not_ready",
    "ops_still_active", "uninstall_incomplete", "unavailable", "stop", "start", "daemon-reload",
    "show", "not_initialized", "key_dir_invalid", "token_dir_in_sandbox", "ready_invalid",
    "ready_not_committed", "key_missing", "digest_mismatch", "key_file_invalid", "not_enrolled",
})
_COMPONENT_REASON = re.compile(r"feature_config_unsafe:[a-z_]{1,32}\Z")
_PROBE_FAILURE_REASON = re.compile(r"feature_probe_failed:[a-z_]{1,32}\Z")


def _normalize_reason_code(value: object) -> str:
    if isinstance(value, str) and (value in REASON_CODES or _COMPONENT_REASON.fullmatch(value)
                                   or _PROBE_FAILURE_REASON.fullmatch(value)):
        return value
    return "unknown"


def _default_probe(feature: str) -> tuple[int, str]:
    paths = resolve_ops_exec(OpsSetup(Path.cwd()).paths)
    return run_probe(str(paths.ops_exec), feature)


def _ask_values(io: IO, feature: str, missing: tuple[str, ...]) -> dict[str, object]:
    if feature == "web":
        return {"port": int(io.ask("port", "8788") or "")} if "port" in missing else {}
    if feature == "notify":
        return {"webhook_url": io.ask_secret("webhook URL")} if "webhook_url" in missing else {}
    values: dict[str, object] = {}
    if "token" in missing:
        values["token"] = io.ask_secret("Discord token")
    if "guild_id" in missing:
        values["guild_id"] = int(io.ask("guild ID") or "")
    if "channel_id" in missing:
        values["channel_id"] = int(io.ask("channel ID") or "")
    if "allowlist_user_ids" in missing:
        values["allowlist_user_ids"] = [int(item.strip()) for item in (io.ask("allowlist user IDs (comma separated)") or "").split(",") if item.strip()]
    if "mention_user_ids" in missing:
        values["mention_user_ids"] = values.get("allowlist_user_ids", [])
    return values


def _active(setup: OpsSetup) -> bool:
    return setup.systemd.show(setup.paths.afx_unit, "ActiveState")["ActiveState"] in {"active", "activating", "reloading"}


_LOCK_UNSAFE = "setup_lock_unsafe"
_LOCK_BUSY = "setup_busy"


def _setup_lock(root: Path):
    """`data/ops/setup.lock` の非 blocking flock。lock の file object か、固定 reason (str) を返す。
    dir は自 uid の所有で other 書込み bit が無いこと (umask 002 の 0775 は許す)。"""
    try:
        return acquire_setup_lock(root)
    except SetupLeaseBusy:
        return _LOCK_BUSY
    except SetupLeaseUnsafe:
        return _LOCK_UNSAFE


def interactive_enable(root: Path, feature: str, *, lease: SetupLease | None = None,
                       io: IO | None = None, probe_runner: Callable[[str], tuple[int, str]] = _default_probe,
                       setup_factory: Callable[[Path], OpsSetup] = OpsSetup,
                       keys_init: Callable[[], int] | None = None) -> EnableResult:
    """Collect only absent inputs, probe them, then delegate to the durable operation."""
    root = Path(root).resolve()
    if io is None:
        io = ConsoleIO()
    if lease is not None:
        if lease.root != root or not lease.active:
            return EnableResult(1, "precheck", "setup_lease_invalid")
        lock = None
    else:
        lock = _setup_lock(root)
        if isinstance(lock, str):
            result = EnableResult(1, "precheck", lock)
            io.stderr(f"afx ops: {result.reason_code}")
            return result
    try:
        existing = collect(root, feature)
        if reason := unsafe_reason(root, feature):
            result = EnableResult(1, "config", f"feature_config_unsafe:{reason}")
            io.stderr(f"afx ops: {result.reason_code}")
            return result
        if not existing.complete and not io.isatty():
            result = EnableResult(2, "config", "setup_input_required")
            io.stderr(f"afx ops: {result.reason_code}")
            return result
        attempts = 0
        while True:
            if not existing.complete or attempts:
                try:
                    entered = _ask_values(io, feature, tuple(_fields_for_prompt(feature, existing, attempts)))
                    values = existing.values | entered
                    if feature == "bot" and ("mention_user_ids" not in values or "allowlist_user_ids" in entered):
                        values["mention_user_ids"] = values.get("allowlist_user_ids", [])
                    write_inputs(root, feature, values)
                except (ValueError, InputError):
                    io.stderr("afx ops: setup_input_invalid")
                    continue
            rc, reason = probe_runner(feature)
            if rc == 0:
                break
            reason_code = _probe_reason_code(reason)
            io.stderr(f"afx-ops: {reason_code}")
            if rc not in {71, 73}:
                return EnableResult(1, "probe", reason_code)
            attempts += 1
            if attempts >= 3 or not io.isatty():
                return EnableResult(1, "probe", reason_code)
            existing = collect(root, feature)
        setup = setup_factory(root)
        try:
            result = setup.enable(feature)
        except KeySetError as error:
            if error.code != "not_initialized":
                raise
            if _active(setup):
                io.stderr("afx ops: afx_running_keys_not_initialized (afx を止めてから再実行してください)")
                return EnableResult(1, "enable", "afx_running_keys_not_initialized")
            init = run_keys_command(root, "init") if keys_init is None else keys_init()
            if init != 0:
                return EnableResult(1, "enable", "keys_init_failed")
            result = setup.enable(feature)
        io.stdout(f"{feature}: {result.stage}")
        return EnableResult(0, result.stage, "ok")
    except (EOFError, KeyboardInterrupt):
        result = EnableResult(1, "config", "setup_aborted")
        io.stderr(f"afx ops: {result.reason_code}")
        return result
    except KeySetError as error:
        result = EnableResult(1, "enable", error.code)
        io.stderr(f"afx ops: {result.reason_code}")
        return result
    except (SystemdError, InvalidState) as error:
        result = EnableResult(1, "enable", error.reason)
        io.stderr(f"afx ops: {result.reason_code}")
        return result
    except Exception:
        result = EnableResult(1, "enable", "unknown")
        io.stderr(f"afx ops: {result.reason_code}")
        return result
    finally:
        if lock is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


def _fields_for_prompt(feature: str, existing, attempts: int) -> tuple[str, ...]:
    if attempts:
        # A failed probe may be caused by any value; ask the feature's complete input set.
        return {"bot": ("token", "guild_id", "channel_id", "allowlist_user_ids"),
                "web": ("port",), "notify": ("webhook_url",)}[feature]
    return existing.missing


def _safe_reason(value: object) -> bool:
    # `run_probe` normalizes the subprocess boundary. Keep injected test/caller values equally safe.
    from agentic_fx.ops_setup.probe import _FIXED_REASONS, _PROBE_REASON
    return isinstance(value, str) and (value in _FIXED_REASONS or bool(_PROBE_REASON.fullmatch(value))
                                       or value == "feature_probe_timeout")


def _probe_reason_code(value: object) -> str:
    if not _safe_reason(value):
        return "feature_probe_unreadable"
    if isinstance(value, str) and value not in REASON_CODES and value.startswith("probe_"):
        word = value.removeprefix("probe_")
        if re.fullmatch(r"[a-z_]{1,32}", word):
            return f"feature_probe_failed:{word}"
    return _normalize_reason_code(value)
