"""Read-only, offline instance status for the local CLI."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agentic_fx.config import load_settings_text
from agentic_fx.core.contracts import SystemClock
from agentic_fx.ops.contracts import Principal
from agentic_fx.ops.keys import key_dir, key_file_name
from agentic_fx.ops.redactor import redact
from agentic_fx.ops.tail import tail_lines
from agentic_fx.ops_setup.stages import read_marker, read_stage
from agentic_fx.setup.pending_facts import collect_pending_facts
from agentic_fx.setup.pending_steps import pending_steps
from agentic_fx.setup.pending_text import render_pending_step
from agentic_fx.setup.unit import UNIT_NAME, default_unit_dir, linger_state
from agentic_fx.store import approvals, missions
from agentic_fx.store.db import connect_observer_readonly
from agentic_fx.store.plugin_deploy_ops import count_open_readonly
from agentic_fx.store.setup_marker import setup_marker_present
from agentic_fx.store.setup_record import read_setup_record

_NOTE = "note: この表示は観測時点の情報です。複数データ源の原子的 snapshot ではありません。"


def probe_instance_lock(root: Path) -> str:
    """Return the one-shot shared-lock observation without creating a file."""
    try:
        fd = os.open(Path(root) / "data" / "instance.lock", os.O_RDONLY)
    except OSError:
        return "unknown"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "locked"
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return "locked"
            return "unknown"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "free"
    finally:
        os.close(fd)


def _state_path(root: Path) -> Path:
    return Path(root) / "data" / "state" / "app_state.json"


def read_state(root: Path, *, sleep: Callable[[float], None] = time.sleep,
               monotonic: Callable[[], float] = time.monotonic) -> tuple[str, str]:
    """Read state under its shared lock without ever creating the lock file."""
    path = _state_path(root)
    if not path.parent.exists():
        return "learning", "clear"
    fd: int | None = None
    try:
        try:
            fd = os.open(str(path) + ".lock", os.O_RDONLY)
        except FileNotFoundError:
            fd = None
        except OSError:
            return "unavailable", "unavailable"
        if fd is not None:
            deadline = monotonic() + 1.0
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if monotonic() >= deadline:
                        return "unavailable", "unavailable"
                    sleep(0.05)
                except OSError:
                    return "unavailable", "unavailable"
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "learning", "clear"
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        mode = data.get("mode")
        if mode not in {"learning", "paper", "live"}:
            raise ValueError("invalid mode")
        latched = data.get("kill_switch_latched")
        if type(latched) is not bool:
            raise ValueError("invalid kill switch")
        marker = Path(str(path) + ".reset-in-progress")
        try:
            os.lstat(marker)
        except FileNotFoundError:
            marker_present = False
        except OSError:
            marker_present = True
        else:
            marker_present = True
        return mode, "latched" if latched or marker_present else "clear"
    except (OSError, ValueError, json.JSONDecodeError):
        return "unavailable", "unavailable"
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _locked_by(root: Path) -> str:
    setup = setup_marker_present(root)
    try:
        marker = read_marker(root)
    except Exception:  # noqa: BLE001 - diagnostic marker reader
        marker = {}
    if marker is None:
        return "setup" if setup else "unknown (daemon 稼働中とみられる)"
    feature = marker.get("feature") if isinstance(marker, dict) else None
    enroll = f"enroll:{feature}" if isinstance(feature, str) and feature else "enroll:unknown"
    return enroll + "+setup" if setup else enroll


def _has_operator_key(root: Path) -> bool:
    directory = key_dir(root)
    for principal in (Principal.OPERATOR, Principal.APPROVER):
        try:
            os.lstat(directory / key_file_name(principal))
        except OSError:
            continue
        return True
    return False


def _locked_next(root: Path, locked_by: str) -> str:
    if locked_by == "setup":
        return "afx init の完了を待ってから afx status"
    if locked_by.startswith("enroll:unknown"):
        return "afx ops の有効化の完了を待ってから afx status"
    if locked_by.startswith("enroll:"):
        return f"afx ops {locked_by.split(':', 1)[1].split('+', 1)[0]} status"
    if _has_operator_key(root):
        return "afx ctl status"
    return "journalctl --user -u afx.service (operator の鍵が無いため afx ctl status は使えません)"


def render_locked(locked_by: str, *, root: Path | None = None) -> str:
    if root is None:
        next_step = "afx init の完了を待ってから afx status" if locked_by == "setup" else "afx ctl status"
    else:
        next_step = _locked_next(root, locked_by)
    return "\n".join(("agentic-fx offline status", "instance: locked", f"locked-by: {locked_by}",
                      _NOTE, f"next: {next_step}")) + "\n"


@dataclass(frozen=True)
class StatusData:
    instance: str
    setup: str
    settings: str
    unit: tuple[str, str, str]
    ops: tuple[str, str, str, str]
    state: tuple[str, str]
    database: str
    mission: str
    approvals: str
    deploy_ops: str
    activity: str
    steps: tuple[str, ...]
    unavailable: bool


def _settings(root: Path) -> tuple[str, Any | None, bool]:
    try:
        raw = (Path(root) / "config" / "settings.yaml").read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", None, False
    except OSError:
        return "invalid", None, True
    try:
        return "ok", load_settings_text(raw), False
    except Exception:  # noqa: BLE001 - validation is display-only
        return "invalid", None, False


def _unit(run: Callable[..., Any], unit_path: Path) -> tuple[tuple[str, str, str], bool]:
    installed = "installed" if unit_path.is_file() else "missing"
    unavailable = False
    try:
        active_result = run(["systemctl", "--user", "is-active", UNIT_NAME], capture_output=True,
                            text=True, timeout=10)
        state = str(getattr(active_result, "stdout", "")).strip()
        active = "active" if state == "active" and getattr(active_result, "returncode", 1) == 0 else (
            "failed" if state == "failed" else "inactive")
    except (OSError, subprocess.SubprocessError):
        active, unavailable = "unknown", True
    try:
        linger = {"yes": "enabled", "no": "disabled"}.get(linger_state(run), "unknown")
    except Exception:  # noqa: BLE001 - the injected runner is an observation source
        linger, unavailable = "unknown", True
    return (installed, active, linger), unavailable


def _ops(root: Path) -> tuple[tuple[str, str, str, str], bool]:
    values: list[str] = []
    bad = False
    for feature in ("bot", "web", "notify"):
        try:
            stage = read_stage(root, feature, missing_ok=True)
            values.append("missing" if stage is None else str(stage.get("stage", "invalid")))
        except Exception:  # noqa: BLE001 - each stage is fail-soft
            values.append("invalid")
    try:
        marker = read_marker(root)
        marker_value = "none" if marker is None else (str(marker.get("feature")) if marker else "invalid")
    except Exception:  # noqa: BLE001
        marker_value, bad = "invalid", True
    return (values[0], values[1], values[2], marker_value), bad


def _database(root: Path) -> tuple[tuple[str, str, str, str], sqlite3.Connection | None, bool]:
    db_path = Path(root) / "data" / "agentic.db"
    if not db_path.is_file():
        return ("missing", "none", "0", "none"), None, False
    try:
        conn = connect_observer_readonly(db_path)
        rows = missions.recent(conn, 1)
        mission = (f"#{rows[0]['id']} {rows[0].get('state', rows[0].get('status', 'unknown'))} at "
                   f"{rows[0].get('started_at', rows[0].get('created_at', 'unknown'))}"
                   if rows else "none")
        approval_count = str(len(approvals.pending(conn)))
        deploy = count_open_readonly(db_path, opener=connect_observer_readonly)
        if deploy is None:
            return ("ok", mission, approval_count, "unavailable"), conn, True
        return ("ok", mission, approval_count, "none" if deploy == 0 else str(deploy)), conn, False
    except Exception:  # noqa: BLE001 - readonly DB can be corrupt or schema-incomplete
        return ("unavailable", "unavailable", "unavailable", "unavailable"), None, True


def _activity(root: Path) -> tuple[str, bool]:
    try:
        result = tail_lines(Path(root) / "logs" / "activity.log", 1)
        if not result.lines:
            return "none", False
        fields = redact(result.lines[-1]).split("\t")
        if len(fields) < 3:
            return "unavailable", True
        return " ".join(fields[:3])[:200], False
    except Exception:  # noqa: BLE001 - log is a separate source
        return "unavailable", True


def _pending(root: Path, instance: str) -> tuple[tuple[str, ...], bool]:
    conn: sqlite3.Connection | None = None
    try:
        _settings_state, settings, _settings_bad = _settings(root)
        db_path = Path(root) / "data" / "agentic.db"
        if db_path.is_file():
            conn = connect_observer_readonly(db_path)
        facts = collect_pending_facts(root, settings, conn, deployed_plugins=(), outage=None,
                                      clock=SystemClock(), service_active=False if instance == "free" else None)
        return tuple(render_pending_step(step) for step in pending_steps(facts, for_status=True)), False
    except Exception:  # noqa: BLE001 - optional facts must not suppress status
        return (), True
    finally:
        if conn is not None:
            conn.close()


def collect_status(root: Path, *, run: Callable[..., Any] = subprocess.run,
                   unit_path: Path | None = None, instance: str | None = None) -> StatusData:
    """Collect independent local observations for a non-locked instance."""
    instance = probe_instance_lock(root) if instance is None else instance
    setup_read = read_setup_record(root)
    settings_state, _settings_value, settings_bad = _settings(root)
    unit, unit_bad = _unit(run, unit_path or default_unit_dir() / UNIT_NAME)
    ops, ops_bad = _ops(root)
    state = ("unavailable", "unavailable") if instance == "unknown" else read_state(root)
    db, _conn, db_bad = _database(root)
    if _conn is not None:
        _conn.close()
    activity, activity_bad = _activity(root)
    steps, pending_bad = _pending(root, instance)
    state_bad = state[0] == "unavailable" and instance != "unknown"
    return StatusData(instance, setup_read.state, settings_state, unit, ops, state, *db,
                      activity, steps, any((settings_bad, unit_bad, ops_bad, state_bad, db_bad,
                                            activity_bad, pending_bad)))


def render_status(data: StatusData) -> str:
    lines = ["agentic-fx offline status", f"instance: {data.instance}", f"setup: {data.setup}",
             f"settings: {data.settings}",
             "unit: afx.service=",]
    lines[-1] += f"{data.unit[0]} active={data.unit[1]} linger={data.unit[2]}"
    lines.append("ops: bot=%s web=%s notify=%s marker=%s" % data.ops)
    lines.append("state: mode=%s kill_switch=%s" % data.state)
    lines.extend((f"database: {data.database}", f"recent mission: {data.mission}",
                  f"pending approvals: {data.approvals}", f"plugin deploy ops: {data.deploy_ops}",
                  f"recent activity: {data.activity}", f"pending steps: {len(data.steps)}"))
    lines.extend(f"  - {step}" for step in data.steps)
    lines.append(_NOTE)
    if data.setup in {"missing", "corrupt"}:
        next_step = "afx init"
    elif data.settings in {"missing", "invalid"}:
        next_step = "afx check"
    elif data.instance == "free" and data.unit[0] == "installed":
        next_step = "systemctl --user start afx.service"
    else:
        next_step = "afx ctl status"
    lines.append(f"next: {next_step}")
    return "\n".join(lines) + "\n"


def main(root: Path, *, run: Callable[..., Any] = subprocess.run) -> int:
    """Print offline state and return a diagnostic exit status."""
    instance = probe_instance_lock(root)
    if instance == "locked":
        print(render_locked(_locked_by(root), root=root), end="")
        return 0
    data = collect_status(root, run=run, instance=instance)
    print(render_status(data), end="")
    return 1 if data.unavailable else 0
