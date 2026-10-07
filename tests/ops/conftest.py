"""ops 層テストの共通部品と、実資源に触れないためのガード。

- 実 home (鍵 dir `~/.config/agentic-fx/api`)、実 `data/` (socket・DB)、`.env` を
  テストの前後で比べ、作成・変更があれば session の終わりに落とす。
- 各テストの HOME / XDG_CONFIG_HOME は tmp_path に向ける。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.ops.contracts import Principal
from agentic_fx.ops.service import OpsLimits, OpsService
from agentic_fx.store import db
from agentic_fx.store.state import StateStore

REPO = Path(__file__).resolve().parents[2]
WALL_START = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)


def _signature(path: Path):
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_dir():
        entries = []
        for child in sorted(path.rglob("*")):
            try:
                cst = child.lstat()
            except FileNotFoundError:
                continue
            entries.append((str(child), cst.st_mtime_ns, cst.st_size))
        return ("dir", st.st_mtime_ns, tuple(entries))
    return ("file", st.st_mtime_ns, st.st_size)


_GUARDED = (
    REPO / ".env",
    REPO / "data",
    Path.home() / ".config" / "agentic-fx",
)


@pytest.fixture(autouse=True, scope="session")
def _guard_real_keys_data_and_env_are_untouched():
    before = {path: _signature(path) for path in _GUARDED}
    yield
    after = {path: _signature(path) for path in _GUARDED}
    changed = [str(path) for path in _GUARDED if before[path] != after[path]]
    assert not changed, f"ops tests touched real resources: {changed}"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))


class WallClock:
    """監査時刻・event 時刻・冪等 24 時間に使う wall clock (手で進める)。"""

    def __init__(self, start: datetime = WALL_START) -> None:
        self.now = start
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self.now

    def advance(self, **delta) -> None:
        with self._lock:
            self.now = self.now + timedelta(**delta)


class MonoClock:
    """要求・lock・job の期限に使う monotonic。実時間で進み、手でも進められる。"""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


@dataclass
class OpsEnv:
    root: Path
    db_path: Path
    conn: object
    plugins_root: Path
    state: StateStore
    wall: WallClock
    mono: MonoClock
    policy_path: Path
    activity_path: Path
    settings: object
    services: list = field(default_factory=list)

    def service(self, **overrides) -> OpsService:
        from agentic_fx.activity import ActivityLog
        kwargs = dict(wall_clock=self.wall, monotonic=self.mono,
                      policy_path=self.policy_path, state_store=self.state,
                      plugins_root=self.plugins_root, settings=self.settings,
                      activity_log=ActivityLog(self.activity_path),
                      activity_path=self.activity_path)
        conn = overrides.pop("conn", self.conn)
        kwargs.update(overrides)
        service = OpsService(conn, **kwargs)
        self.services.append(service)
        return service

    def connect(self):
        return db.connect(self.db_path)

    def make_plugin_approval(self, name: str = "sma", value: float = 1.0,
                             mission_id: int = 1) -> int:
        """fake gate で実 submit_candidate を通した plugin 承認依頼を作る。"""
        from agentic_fx.plugin import switch
        from agentic_fx.plugin.gate_pytest import GateResult
        staging = self.plugins_root / "_staging" / str(mission_id)
        candidate = staging / name
        candidate.mkdir(parents=True, exist_ok=True)
        (candidate / "plugin.py").write_text(
            f"def compute(df, params):\n    return {{'v': {value}}}\n")
        (candidate / "config.yaml").write_text("kind: indicator\noutputs: [v]\n")
        (candidate / "test_plugin.py").write_text("def test_x():\n    pass\n")
        original = switch.run_gate_pytest
        switch.run_gate_pytest = lambda plugin_dir, *, settings: GateResult(
            passed=True, returncode=0, stdout_tail="ok", duration_sec=0.1)
        try:
            return switch.submit_candidate(
                self.conn, name=name, staging_dir=staging, candidate_origin="staging",
                mission_id=mission_id, backlog_id=None, settings=self.settings,
                now=self.wall())
        finally:
            switch.run_gate_pytest = original

    def raw_approval(self, kind: str = "plugin", payload: dict | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO approval_requests(kind,payload_json,created_at) VALUES (?,?,?)",
            (kind, json.dumps(payload or {"name": "x"}), self.wall().isoformat()))
        self.conn.commit()
        return int(cur.lastrowid)

    def approval(self, approval_id: int):
        return self.conn.execute("SELECT * FROM approval_requests WHERE id=?",
                                 (approval_id,)).fetchone()

    def ops_rows(self):
        return self.conn.execute("SELECT * FROM ops_requests ORDER BY id").fetchall()


@pytest.fixture
def ops_env(tmp_path):
    from agentic_fx.config import load_settings
    root = tmp_path / "root"
    plugins_root = root / "plugins"
    (plugins_root / ".locks").mkdir(parents=True)
    db_path = root / "data" / "agentic.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    env = OpsEnv(root=root, db_path=db_path, conn=conn, plugins_root=plugins_root,
                 state=StateStore(root / "data" / "app_state.json"),
                 wall=WallClock(), mono=MonoClock(),
                 policy_path=root / "policy" / "directives.md",
                 activity_path=root / "logs" / "activity.log",
                 settings=load_settings(REPO / "config" / "settings.yaml.example"))
    yield env
    for service in env.services:
        service.shutdown(join_timeout=2.0)
    conn.close()


def digest_of(env: OpsEnv, approval_id: int) -> str:
    from agentic_fx.plugin.switch import payload_sha256
    return payload_sha256(env.approval(approval_id)["payload_json"])


def wait_job(service: OpsService, job_id: str, timeout: float = 10.0) -> dict:
    return service.wait_for_job(job_id, timeout=timeout)


def hold_plugin_lock(plugins_root: Path, name: str):
    """別 thread で plugin flock を保持する。返り値の release() で解放。"""
    from agentic_fx.plugin.switch import _plugin_lock
    entered, release = threading.Event(), threading.Event()

    def holder():
        with _plugin_lock(plugins_root, name):
            entered.set()
            release.wait(30)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert entered.wait(5)

    def done():
        release.set()
        thread.join(5)
    return done


APPROVER = Principal.APPROVER
OPERATOR = Principal.OPERATOR
