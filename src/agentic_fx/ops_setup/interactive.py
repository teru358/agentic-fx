"""Terminal-facing preflight for ``afx ops enable``."""
from __future__ import annotations

import fcntl
import getpass
import os
import stat
import sys
from pathlib import Path
from typing import Callable, Protocol

from agentic_fx.ops.keys import KeySetError, run_keys_command
from agentic_fx.ops_setup.feature_config import InputError, collect, unsafe_reason, write_inputs
from agentic_fx.ops_setup.probe import run_probe
from agentic_fx.ops_setup.stages import OpsSetup
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
    root_fd = os.open(Path(root), os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.getuid():
            return _LOCK_UNSAFE
        parent_fd = root_fd
        for name in ("data", "ops"):
            created = False
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                pass
            directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
            directory_stat = os.fstat(directory_fd)
            if (not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != os.getuid()
                    or stat.S_IMODE(directory_stat.st_mode) & 0o002):
                os.close(directory_fd)
                return _LOCK_UNSAFE
            if created:
                os.chmod(name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = directory_fd
    finally:
        os.close(root_fd)
    try:
        fd = os.open("setup.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o002:
        os.close(fd)
        return _LOCK_UNSAFE
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return _LOCK_BUSY
    return os.fdopen(fd, "a+b")


def interactive_enable(root: Path, feature: str, *, io: IO, probe_runner: Callable[[str], tuple[int, str]] = _default_probe,
                       setup_factory: Callable[[Path], OpsSetup] = OpsSetup,
                       keys_init: Callable[[], int] | None = None) -> int:
    """Collect only absent inputs, probe them, then delegate to the durable operation."""
    root = Path(root)
    lock = _setup_lock(root)
    if isinstance(lock, str):
        io.stderr(f"afx ops: {lock}")
        return 1
    try:
        existing = collect(root, feature)
        if reason := unsafe_reason(root, feature):
            io.stderr(f"afx ops: feature_config_unsafe:{reason}")
            return 1
        if not existing.complete and not io.isatty():
            io.stderr("afx ops: setup_input_required")
            return 2
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
            io.stderr(f"afx-ops: {reason if _safe_reason(reason) else 'feature_probe_unreadable'}")
            if rc not in {71, 73}:
                return 1
            attempts += 1
            if attempts >= 3 or not io.isatty():
                return 1
            existing = collect(root, feature)
        setup = setup_factory(root)
        try:
            result = setup.enable(feature)
        except KeySetError as error:
            if error.code != "not_initialized":
                raise
            if _active(setup):
                io.stderr("afx ops: afx_running_keys_not_initialized (afx を止めてから再実行してください)")
                return 1
            init = run_keys_command(root, "init") if keys_init is None else keys_init()
            if init != 0:
                return 1
            result = setup.enable(feature)
        io.stdout(f"{feature}: {result.stage}")
        return 0
    except (EOFError, KeyboardInterrupt):
        io.stderr("afx ops: setup_aborted")
        return 1
    except KeySetError as error:
        io.stderr(f"afx ops: {error.code}")
        return 1
    except Exception:
        io.stderr("afx ops: setup_internal_error")
        return 1
    finally:
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
