"""Read-only, offline instance status for the local CLI."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import sqlite3
import stat
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
from agentic_fx.ops_setup.stages import InvalidState, read_marker, read_stage
from agentic_fx.setup.pending_facts import collect_pending_facts
from agentic_fx.setup.pending_steps import pending_steps
from agentic_fx.setup.pending_text import render_pending_step
from agentic_fx.setup.unit import UNIT_NAME, default_unit_dir, linger_state
from agentic_fx.store import approvals, missions
from agentic_fx.store.db import connect_observer_readonly
from agentic_fx.store.plugin_deploy_ops import count_open_readonly
from agentic_fx.store.setup_marker import setup_marker_present
from agentic_fx.store.setup_record import read_setup_record
from agentic_fx.store.state import parse_state
from agentic_fx.tools import plugin_loader

_UNIT_WORDS = frozenset(("active", "activating", "deactivating", "inactive", "failed"))
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
        marker = Path(str(path) + ".reset-in-progress")
        try:
            os.lstat(marker)
        except FileNotFoundError:
            marker_present = False
        except OSError:
            marker_present = True
        else:
            marker_present = True
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "learning", "latched" if marker_present else "clear"
        state = parse_state(json.loads(raw))
        return state.mode.value, "latched" if state.kill_switch_latched or marker_present else "clear"
    except Exception:  # noqa: BLE001 - any unreadable state is one unavailable observation
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


def _locked_next(root: Path | None, locked_by: str) -> str:
    if locked_by == "setup":
        return "afx init の完了を待ってから afx status"
    if locked_by.startswith("enroll:unknown"):
        return "afx ops の有効化の完了を待ってから afx status"
    if locked_by.startswith("enroll:"):
        return f"afx ops {locked_by.split(':', 1)[1].split('+', 1)[0]} status"
    if root is None or _has_operator_key(root):
        return "afx ctl status"
    return "journalctl --user -u afx.service (operator の鍵が無いため afx ctl status は使えません)"


def _root_line(root: Path | None) -> tuple[str, ...]:
    return () if root is None else (f"root: {os.path.abspath(root)}",)


def render_locked(locked_by: str, *, root: Path | None = None) -> str:
    return "\n".join(("agentic-fx offline status", *_root_line(root), "instance: locked",
                      f"locked-by: {locked_by}", _NOTE,
                      f"next: {_locked_next(root, locked_by)}")) + "\n"


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
    steps: tuple[str, ...] | None
    unavailable: bool


def _settings(root: Path) -> tuple[str, Any | None]:
    try:
        raw = (Path(root) / "config" / "settings.yaml").read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", None
    except (OSError, UnicodeDecodeError):
        return "invalid", None
    try:
        return "ok", load_settings_text(raw)
    except Exception:  # noqa: BLE001 - validation is display-only
        return "invalid", None


def _unit(run: Callable[..., Any], unit_path: Path) -> tuple[tuple[str, str, str], bool]:
    try:
        installed = "installed" if unit_path.is_file() else "missing"
    except OSError:
        installed = "missing"
    unavailable = False
    try:
        active_result = run(["systemctl", "--user", "is-active", UNIT_NAME], capture_output=True,
                            text=True, timeout=10)
        state = str(getattr(active_result, "stdout", "")).strip()
        active = state if state in _UNIT_WORDS else "unknown"
    except FileNotFoundError:
        active = "unknown"
    except Exception:  # noqa: BLE001 - the injected runner is an observation source
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
        except InvalidState:
            values.append("invalid")
        except Exception:  # noqa: BLE001 - each stage is fail-soft
            values.append("unavailable")
            bad = True
    try:
        marker = read_marker(root)
        marker_value = "none" if marker is None else (str(marker.get("feature")) if marker else "invalid")
    except Exception:  # noqa: BLE001
        marker_value, bad = "unavailable", True
    return (values[0], values[1], values[2], marker_value), bad


def _observer_db_path(root: Path) -> tuple[Path, str]:
    """Return the database path and `missing`, `file` or `other`, following symlinks."""
    db_path = Path(root) / "data" / "agentic.db"
    try:
        mode = os.stat(db_path).st_mode
    except FileNotFoundError:
        try:
            os.lstat(db_path)
        except FileNotFoundError:
            return db_path, "missing"
        except OSError:
            return db_path, "other"
        return db_path, "other"
    except OSError:
        return db_path, "other"
    return db_path, "file" if stat.S_ISREG(mode) else "other"


def _database(db_path: Path, kind: str, conn: sqlite3.Connection | None) -> tuple[tuple[str, str, str, str], bool]:
    if kind == "missing":
        return ("missing", "none", "0", "none"), False
    unavailable = ("unavailable", "unavailable", "unavailable", "unavailable")
    if conn is None:
        return unavailable, True
    try:
        rows = missions.recent(conn, 1)
        mission = (f"#{rows[0]['id']} {rows[0].get('state', rows[0].get('status', 'unknown'))} at "
                   f"{rows[0].get('started_at', rows[0].get('created_at', 'unknown'))}"
                   if rows else "none")
    except Exception:  # noqa: BLE001
        mission = "unavailable"
    try:
        approval_count = str(len(approvals.pending(conn)))
    except Exception:  # noqa: BLE001
        approval_count = "unavailable"
    try:
        deploy_count = count_open_readonly(db_path, opener=connect_observer_readonly)
    except Exception:  # noqa: BLE001
        deploy_count = None
    deploy = "unavailable" if deploy_count is None else ("none" if deploy_count == 0 else str(deploy_count))
    lines = (mission, approval_count, deploy)
    if all(line == "unavailable" for line in lines):
        return unavailable, True
    return ("ok", *lines), "unavailable" in lines


def _activity(root: Path) -> tuple[str, bool]:
    try:
        result = tail_lines(Path(root) / "logs" / "activity.log", 1)
        if not result.lines:
            return "none", False
        fields = result.lines[-1].split("\t")
        if len(fields) < 3:
            return "unavailable", True
        return " ".join(redact(field) for field in fields[:3])[:200], False
    except Exception:  # noqa: BLE001 - log is a separate source
        return "unavailable", True


def _pending(root: Path, instance: str, settings: Any | None,
             conn: sqlite3.Connection | None) -> tuple[tuple[str, ...] | None, bool]:
    try:
        deployed: tuple[Any, ...] = ()
        plugins_dir = Path(root) / "plugins"
        if settings is not None and conn is not None and plugins_dir.is_dir():
            deployed = tuple(plugin_loader.approved_plugins(
                conn, plugins_dir, settings=settings).inventory.metas)
        facts = collect_pending_facts(root, settings, conn, deployed_plugins=deployed, outage=None,
                                      clock=SystemClock(), service_active=False if instance == "free" else None)
        return tuple(render_pending_step(step) for step in pending_steps(facts, for_status=True)), False
    except Exception:  # noqa: BLE001 - optional facts must not suppress status
        return None, True


def collect_status(root: Path, *, run: Callable[..., Any] = subprocess.run,
                   unit_path: Path | None = None, instance: str | None = None) -> StatusData:
    """Collect independent local observations for a non-locked instance."""
    instance = probe_instance_lock(root) if instance is None else instance
    try:
        setup_state, setup_bad = read_setup_record(root).state, False
    except Exception:  # noqa: BLE001 - each source is independent
        setup_state, setup_bad = "unavailable", True
    settings_state, settings_value = _settings(root)
    try:
        unit, unit_bad = _unit(run, unit_path or default_unit_dir() / UNIT_NAME)
    except Exception:  # noqa: BLE001
        unit, unit_bad = ("missing", "unknown", "unknown"), True
    try:
        ops, ops_bad = _ops(root)
    except Exception:  # noqa: BLE001
        ops, ops_bad = ("unavailable",) * 4, True
    if instance == "unknown":
        state = ("unavailable", "unavailable")
    else:
        try:
            state = read_state(root)
        except Exception:  # noqa: BLE001
            state = ("unavailable", "unavailable")
    db_path, db_kind = _observer_db_path(root)
    conn: sqlite3.Connection | None = None
    if db_kind == "file":
        try:
            conn = connect_observer_readonly(db_path)
        except Exception:  # noqa: BLE001 - readonly DB can be corrupt or unreadable
            conn = None
    try:
        try:
            db, db_bad = _database(db_path, db_kind, conn)
        except Exception:  # noqa: BLE001
            db, db_bad = ("unavailable",) * 4, True
        steps, pending_bad = _pending(root, instance, settings_value, conn)
    finally:
        if conn is not None:
            conn.close()
    activity, activity_bad = _activity(root)
    state_bad = state[0] == "unavailable" and instance != "unknown"
    return StatusData(instance, setup_state, settings_state, unit, ops, state, *db,
                      activity, steps, any((setup_bad, unit_bad, ops_bad, state_bad, db_bad,
                                            activity_bad, pending_bad)))


def render_status(data: StatusData, *, root: Path | None = None) -> str:
    lines = ["agentic-fx offline status", *_root_line(root), f"instance: {data.instance}",
             f"setup: {data.setup}",
             f"settings: {data.settings}",
             f"unit: afx.service={data.unit[0]} active={data.unit[1]} linger={data.unit[2]}"]
    lines.append("ops: bot=%s web=%s notify=%s marker=%s" % data.ops)
    lines.append("state: mode=%s kill_switch=%s" % data.state)
    lines.extend((f"database: {data.database}", f"recent mission: {data.mission}",
                  f"pending approvals: {data.approvals}", f"plugin deploy ops: {data.deploy_ops}",
                  f"recent activity: {data.activity}",
                  f"pending steps: {'unavailable' if data.steps is None else len(data.steps)}"))
    lines.extend(f"  - {step}" for step in data.steps or ())
    lines.append(_NOTE)
    if data.setup in {"missing", "corrupt"}:
        next_step = "afx init"
    elif data.settings in {"missing", "invalid"}:
        next_step = "afx check"
    elif (data.instance == "free" and data.unit[0] == "installed"
          and data.unit[1] in {"inactive", "failed", "unknown"}):
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
    print(render_status(data, root=root), end="")
    return 1 if data.unavailable else 0
