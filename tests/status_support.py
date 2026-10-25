"""Fixtures shared by the offline status tests."""
from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agentic_fx.ops.contracts import Principal
from agentic_fx.ops.keys import key_dir, key_file_name
from agentic_fx.store import approvals, missions
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.setup_record import SetupRecord, write_setup_record

NOW = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)
NOTE = "note: この表示は観測時点の情報です。複数データ源の原子的 snapshot ではありません。"
REPO = Path(__file__).resolve().parents[1]
UNKNOWN_HOLDER = "unknown (daemon 稼働中とみられる)"


def setup_data():
    return {
        "version": 1, "completed_at": "2026-10-12T00:00:00Z", "mode": "interactive",
        "settings_schema_version": 1,
        "price": {"result": "ok", "at": "2026-10-12T00:00:00Z", "reason_code": "ok"},
        "llm_trade": {"result": "ok", "at": "2026-10-12T00:00:00Z", "reason_code": "ok"},
        "llm_improve": {"result": "ok", "at": "2026-10-12T00:00:00Z", "reason_code": "ok"},
        "bridge_plan": "connected", "history": {"source": "mt5"},
        "indicators": {"installed": 9}, "unit": "installed",
        "api": {"enabled": True, "result": "ok"}, "ops": {},
        "isolation": {"admitted": "yes"}, "linger": {"state": "already_enabled"},
        "start": {"result": "started"},
    }


def make_run(*, active="inactive", active_rc=3, linger="Linger=yes\n", fail=None):
    """A systemctl / loginctl stand-in; ``fail`` makes every call raise OSError."""
    calls = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        if fail is not None:
            raise fail
        if argv[0] == "loginctl":
            return SimpleNamespace(returncode=0, stdout=linger)
        return SimpleNamespace(returncode=active_rc, stdout=active + "\n")

    run.calls = calls
    return run


def write_settings(root: Path, kind: str) -> None:
    path = root / "config" / "settings.yaml"
    if kind == "missing":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "ok":
        path.write_text((REPO / "config" / "settings.yaml.example").read_text(encoding="utf-8"),
                        encoding="utf-8")
    else:
        path.write_text("pairs: [[[not valid\n", encoding="utf-8")


def write_setup(root: Path, kind: str) -> None:
    if kind == "missing":
        return
    if kind == "ok":
        write_setup_record(root, SetupRecord(setup_data()))
        return
    path = root / "data" / "state" / "setup.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")


def make_db(root: Path, *, mission: bool = True, approval: int = 1, drop: tuple[str, ...] = ()) -> Path:
    path = root / "data" / "agentic.db"
    conn = connect(path)
    try:
        init_db(conn)
        if mission:
            missions.start(conn, "trade", "local", "m", NOW)
            conn.commit()
        for number in range(approval):
            approvals.create(conn, "plugin", {"n": number}, NOW)
        for table in drop:
            conn.execute(f"DROP TABLE {table}")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(f"{path}{suffix}")
        except FileNotFoundError:
            pass
    return path


def add_sidecars(db: Path) -> None:
    """Leave a committed, un-checkpointed WAL next to the database, as a stopped daemon would."""
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("INSERT INTO approval_requests (kind, payload_json, created_at) VALUES ('plugin','{}',?)",
                     (NOW.isoformat(),))
        conn.commit()
        stage = db.with_name(db.name + ".stage")
        stage.mkdir()
        shutil.copy(f"{db}-wal", stage / "wal")
        shutil.copy(f"{db}-shm", stage / "shm")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(f"{db}{suffix}")
        except FileNotFoundError:
            pass
    shutil.copy(stage / "wal", f"{db}-wal")
    shutil.copy(stage / "shm", f"{db}-shm")
    shutil.rmtree(stage)


def write_activity(root: Path, *lines: str) -> None:
    path = root / "logs" / "activity.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


VALID_STAGE = '{"operation":"enable","stage":"enabled","feature":"%s"}'


def write_stage(root: Path, feature: str, text: str) -> None:
    path = root / "data" / "ops" / f"{feature}.stage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_enroll_marker(root: Path, text: str) -> None:
    path = root / "data" / "ops" / "enroll_in_progress"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_setup_marker(root: Path) -> None:
    path = root / "data" / "state" / "setup_in_progress"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


def write_state(root: Path, text: str, *, lock: bool = False, reset_marker: bool = False) -> Path:
    path = root / "data" / "state" / "app_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if lock:
        path.with_name("app_state.json.lock").touch()
    if reset_marker:
        path.with_name("app_state.json.reset-in-progress").write_text("{}", encoding="utf-8")
    return path


def state_json(mode="learning", latched=False) -> str:
    flag = "true" if latched else "false"
    return '{"initialized":true,"mode":"%s","autopilot":false,"kill_switch_latched":%s}' % (mode, flag)


def write_key(root: Path, principal: Principal, *, mode: int = 0o600) -> Path:
    directory = key_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / key_file_name(principal)
    path.write_text("secret-token-value", encoding="utf-8")
    os.chmod(path, mode)
    return path


def touch_lock(root: Path) -> Path:
    path = root / "data" / "instance.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


_HOLDER = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR)\n"
    "fcntl.flock(fd, int(sys.argv[2]))\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)


@contextmanager
def held_by_other_process(path: Path, mode: int = fcntl.LOCK_EX):
    """Hold a flock from a separate process for the duration of the block."""
    process = subprocess.Popen([sys.executable, "-I", "-c", _HOLDER, str(path), str(mode)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready", "holder process did not take the lock"
        yield process
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


def manifest(root: Path) -> dict[str, tuple]:
    """Every path under ``root`` with its type, byte content and symlink target."""
    result: dict[str, tuple] = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            full = Path(current) / name
            rel = str(full.relative_to(root))
            info = os.lstat(full)
            if stat.S_ISLNK(info.st_mode):
                result[rel] = ("link", os.readlink(full))
            elif stat.S_ISDIR(info.st_mode):
                result[rel] = ("dir",)
            elif stat.S_ISREG(info.st_mode):
                result[rel] = ("file", hashlib.sha256(full.read_bytes()).hexdigest())
            else:
                result[rel] = ("other",)
    return result


def wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()
