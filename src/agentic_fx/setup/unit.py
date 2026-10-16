"""afx.service (direct exec の user unit) の生成・設置と linger の確認つき有効化。

systemctl / loginctl / `afx check` の実行は呼び出し側から注入された `run` を通す。
秘密は unit に入れない。unit 名は `afx.service` 固定。
"""
from __future__ import annotations

import difflib
import json
import getpass
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from agentic_fx.ops_setup.units import _syntax_problem, atomic_write
from agentic_fx.setup.checks import cli_candidate
from agentic_fx.setup.preflight import CheckReport

UNIT_NAME = "afx.service"
_BASE_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
_LANG_RE = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
_JOURNAL_HINT = "journalctl --user -u afx.service -n 50 で理由を確認してください"
_PLACED_NOT_RELOADED_HINT = ("unit は置きましたが daemon-reload に失敗しました。"
                             "`systemctl --user daemon-reload` を実行してください。" + _JOURNAL_HINT)

_TEMPLATE = """[Unit]
Description=agentic-fx paper trader (daemon)
After=network-online.target{bridge_after}
Wants=network-online.target{bridge_wants}

[Service]
Type=exec
NotifyAccess=none
Delegate=pids memory
WorkingDirectory={repo_root}
ExecStart={python} main.py --daemon
SendSIGKILL=yes
FinalKillSignal=SIGKILL
TimeoutStopFailureMode=terminate
TimeoutStopSec=630
KillMode=control-group
Restart=on-failure
RestartSec=30
RestartPreventExitStatus=75 76
StandardOutput=journal
StandardError=journal
Environment=LANG={lang}
Environment=PYTHONUNBUFFERED=1
Environment=PATH={path}

[Install]
WantedBy=default.target
"""


@dataclass(frozen=True)
class UnitValues:
    root: Path
    python: Path
    path_dirs: tuple[str, ...]
    lang: str
    bridge_unit_present: bool
    attributes: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnitBlocked:
    reason_code: str
    message: str


def default_unit_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME")
    root = Path(base) if base and Path(base).is_absolute() else Path(env.get("HOME") or Path.home()) / ".config"
    return root / "systemd" / "user"


def render_unit(root: Path, python: Path, path_dirs: tuple[str, ...] | list[str], lang: str,
                bridge_unit_present: bool) -> str:
    return _TEMPLATE.format(
        bridge_after=" mt5-bridge.service" if bridge_unit_present else "",
        bridge_wants=" mt5-bridge.service" if bridge_unit_present else "",
        repo_root=str(root), python=str(python), lang=lang,
        path=":".join(path_dirs))


def _backend_bins(settings: Any) -> list[str]:
    bins: list[str] = []
    for which in ("trade", "improve"):
        backend = getattr(settings.runner, which).backend
        if backend in ("claude", "codex", "opencode"):
            bins.append(str(getattr(settings.runner, backend).bin))
    return bins


def detect_unit_values(root: Path, settings: Any, which: Callable[[str], str | None] = shutil.which,
                       env: Mapping[str, str] | None = None, *,
                       unit_dir: Path | None = None) -> UnitValues | UnitBlocked:
    env = os.environ if env is None else env
    root = Path(root)
    python = root / ".venv" / "bin" / "python"
    if not python.exists():
        return UnitBlocked("python_missing",
                           f"{python} がありません。先に `uv sync` を実行してください")
    dirs: list[str] = []
    nvm = False
    for bin_value in _backend_bins(settings):
        resolved = cli_candidate(bin_value, which=which)
        if resolved is None:
            continue
        real_parent = str(Path(resolved).resolve().parent)
        if "/.nvm/" in real_parent:
            nvm = True
        for parent in (real_parent, str(Path(resolved).parent)):
            if parent not in dirs:
                dirs.append(parent)
    for fixed in _BASE_PATH:
        if fixed not in dirs:
            dirs.append(fixed)
    for label, value, forbidden in (("repo root", str(root), ""), ("python", str(python), ""),
                                    *(("PATH", d, ":") for d in dirs)):
        problem = _syntax_problem(value, forbidden=forbidden)
        if problem:
            return UnitBlocked("unsafe_path_chars", f"{label} が systemd の unit に書けません ({problem})")
    attributes = ("nvm_path",) if nvm else ()
    lang = env.get("LANG") or "C.UTF-8"
    if not _LANG_RE.fullmatch(lang):
        lang = "C.UTF-8"
    directory = unit_dir if unit_dir is not None else default_unit_dir(env)
    bridge = (directory / "mt5-bridge.service").exists()
    return UnitValues(root, python, tuple(dirs), lang, bridge, attributes)


def systemctl_available(run: Callable[..., Any] = subprocess.run,
                        which: Callable[[str], str | None] = shutil.which) -> bool:
    if which("systemctl") is None:
        return False
    try:
        completed = run(["systemctl", "--user", "show-environment"],
                        capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(completed, "returncode", 1) == 0


@dataclass(frozen=True)
class InstallResult:
    state: str  # installed | unchanged | declined | placed_not_reloaded | failed
    path: Path
    backup: Path | None = None
    reason_code: str | None = None
    hint: str | None = None
    notes: tuple[str, ...] = ()


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _free_backup_name(target: Path, stamp: str) -> Path:
    """同じ名前の退避が既にあれば連番を足し、前の退避を上書きしない。"""
    candidate = target.with_name(f"{UNIT_NAME}.bak-{stamp}")
    number = 1
    while candidate.exists():
        number += 1
        candidate = target.with_name(f"{UNIT_NAME}.bak-{stamp}-{number}")
    return candidate


def install_unit(unit_dir: Path, content: str, *, confirm: Callable[[str], bool],
                 run: Callable[..., Any] = subprocess.run,
                 now: Callable[[], str] = _utc_stamp) -> InstallResult:
    target = Path(unit_dir) / UNIT_NAME
    backup: Path | None = None
    notes: tuple[str, ...] = ()
    try:
        existing = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = None
    except (OSError, UnicodeDecodeError):
        return InstallResult("failed", target, None, "read_failed", _JOURNAL_HINT, notes)
    if existing is not None:
        if existing == content:
            return _reload(target, None, notes, "unchanged", run)
        diff = "".join(difflib.unified_diff(
            existing.splitlines(keepends=True), content.splitlines(keepends=True),
            fromfile=f"{UNIT_NAME} (現在)", tofile=f"{UNIT_NAME} (新)"))
        if "afx-start" in existing:
            notes = ("対話シェル (screen -r afx) はなくなり、操作は afx ctl になります",)
        if not confirm(diff):
            return InstallResult("declined", target, notes=notes)
        backup = _free_backup_name(target, now())
        try:
            atomic_write(backup, existing.encode("utf-8"), 0o644)
        except OSError:
            return InstallResult("failed", target, None, "backup_failed", _JOURNAL_HINT, notes)
    try:
        atomic_write(target, content.encode("utf-8"), 0o644)
    except OSError:
        return InstallResult("failed", target, backup, "write_failed", _JOURNAL_HINT, notes)
    return _reload(target, backup, notes, "installed", run)


def _reload(target: Path, backup: Path | None, notes: tuple[str, ...], state: str,
            run: Callable[..., Any]) -> InstallResult:
    """daemon-reload を実行する (冪等)。失敗なら placed_not_reloaded。"""
    try:
        completed = run(["systemctl", "--user", "daemon-reload"],
                        capture_output=True, text=True, timeout=30)
        reloaded = getattr(completed, "returncode", 1) == 0
    except (OSError, subprocess.SubprocessError):
        reloaded = False
    if not reloaded:
        return InstallResult("placed_not_reloaded", target, backup, "daemon_reload_failed",
                             _PLACED_NOT_RELOADED_HINT, notes)
    return InstallResult(state, target, backup, notes=notes)


def unit_record_values(result: InstallResult | PlacementResult) -> dict[str, str | None]:
    """設置の結果を確認記録の `unit` 語彙 (installed | placed_not_reloaded | not_installed) に写す。

    記録を書くのは呼び出し側。`reason_code` は置けていない・有効化できていない理由で、
    問題が無いときは None。unit を置く前に止まった状態 (blocked / check_failed /
    no_systemd) は not_installed。"""
    if result.state in ("installed", "unchanged"):
        return {"unit": "installed", "reason_code": None}
    if result.state == "placed_not_reloaded":
        return {"unit": "placed_not_reloaded", "reason_code": result.reason_code}
    if result.state in ("declined", "failed"):
        install = result if isinstance(result, InstallResult) else result.install
        reason = "declined" if result.state == "declined" else result.reason_code
        if install is not None and install.path.is_file():
            return {"unit": "installed", "reason_code": reason}
        return {"unit": "not_installed", "reason_code": reason}
    return {"unit": "not_installed", "reason_code": result.reason_code}


def run_check_in_unit_env(values: UnitValues, run: Callable[..., Any] = subprocess.run, *,
                          env: Mapping[str, str] | None = None) -> CheckReport:
    """unit と同じ最小環境で `main.py check` を実行し、fatal / warning を読む。"""
    env = os.environ if env is None else env
    argv = ["env", "-i", f"HOME={env.get('HOME') or Path.home()}",
            f"PATH={':'.join(values.path_dirs)}", f"LANG={values.lang}",
            "PYTHONUNBUFFERED=1", str(values.python), "main.py", "check", "--json", "--unit-env"]
    try:
        completed = run(argv, cwd=str(values.root), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return CheckReport(["startup_check_failed"], [])
    report = _read_report(getattr(completed, "stdout", "") or "")
    if report is None:
        return CheckReport(["startup_check_failed"], [])
    if getattr(completed, "returncode", 1) != 0 and not report.fatal:
        return CheckReport(["startup_check_failed"], report.warning)
    return report


def _read_report(text: str) -> CheckReport | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    fatal, warning = data.get("fatal"), data.get("warning")
    if not (isinstance(fatal, list) and isinstance(warning, list)
            and all(isinstance(code, str) for code in (*fatal, *warning))):
        return None
    return CheckReport(list(fatal), list(warning))


@dataclass(frozen=True)
class PlacementResult:
    state: str  # installed | unchanged | declined | placed_not_reloaded | blocked | check_failed | no_systemd | failed
    reason_code: str | None = None
    message: str = ""
    report: CheckReport | None = None
    install: InstallResult | None = None
    notes: tuple[str, ...] = ()


MANUAL_START_GUIDE = "systemd のユーザーサービスが使えません。手で起動するには `uv run afx` を実行してください"
_MISSING_USER_SESSION_GUIDE = (
    "systemd のユーザーセッションが見えません (XDG_RUNTIME_DIR が無い)。"
    "`su` ではなく {user} で直接ログインして `afx init` を再実行してください")


def _login_name(env: Mapping[str, str]) -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return env.get("USER") or "このユーザー"


def place_unit(root: Path, settings: Any, *, unit_dir: Path, confirm: Callable[[str], bool],
               run: Callable[..., Any] = subprocess.run,
               check_run: Callable[..., Any] = subprocess.run,
               which: Callable[[str], str | None] = shutil.which,
               env: Mapping[str, str] | None = None) -> PlacementResult:
    """値の検出 → unit と同じ環境での afx check → systemctl の可否 → 設置。"""
    values = detect_unit_values(root, settings, which, env, unit_dir=unit_dir)
    if isinstance(values, UnitBlocked):
        return PlacementResult("blocked", values.reason_code, values.message)
    report = run_check_in_unit_env(values, check_run, env=env)
    if report.fatal:
        return PlacementResult("check_failed", report.fatal[0],
                               "fatal: " + ", ".join(report.fatal), report)
    if not systemctl_available(run, which):
        active_env = os.environ if env is None else env
        if which("systemctl") is not None and not active_env.get("XDG_RUNTIME_DIR"):
            return PlacementResult("no_systemd", "user_session_missing",
                                   _MISSING_USER_SESSION_GUIDE.format(user=_login_name(active_env)), report)
        return PlacementResult("no_systemd", "systemd_unavailable", MANUAL_START_GUIDE, report)
    content = render_unit(values.root, values.python, values.path_dirs, values.lang,
                          values.bridge_unit_present)
    result = install_unit(unit_dir, content, confirm=confirm, run=run)
    notes = result.notes
    if "nvm_path" in values.attributes:
        notes += ("node を更新したら afx init を再実行して unit を作り直してください",)
    return PlacementResult(result.state, result.reason_code, result.hint or "", report, result, notes)


@dataclass(frozen=True)
class LingerResult:
    state: str  # owned_by_wizard | already_enabled | declined | failed
    reason_code: str | None = None


def linger_state(run: Callable[..., Any] = subprocess.run, *, user: str | None = None) -> str:
    """`yes` / `no` / `unknown`。"""
    name = user or getpass.getuser()
    try:
        completed = run(["loginctl", "show-user", name, "-p", "Linger"],
                        capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if getattr(completed, "returncode", 1) != 0:
        return "unknown"
    for line in (getattr(completed, "stdout", "") or "").splitlines():
        if line.strip() == "Linger=yes":
            return "yes"
        if line.strip() == "Linger=no":
            return "no"
    return "unknown"


def enable_linger(run: Callable[..., Any] = subprocess.run, *, confirm: Callable[[], bool],
                  user: str | None = None) -> LingerResult:
    name = user or getpass.getuser()
    state = linger_state(run, user=name)
    if state == "yes":
        return LingerResult("already_enabled")
    if state == "unknown":
        return LingerResult("failed", "unknown")
    if not confirm():
        return LingerResult("declined")
    try:
        completed = run(["loginctl", "enable-linger", name],
                        capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return LingerResult("failed", "timeout")
    except (OSError, subprocess.SubprocessError):
        return LingerResult("failed", "unknown")
    if getattr(completed, "returncode", 1) != 0:
        return LingerResult("failed", "unknown")
    if linger_state(run, user=name) != "yes":
        return LingerResult("failed", "unknown")
    return LingerResult("owned_by_wizard")
