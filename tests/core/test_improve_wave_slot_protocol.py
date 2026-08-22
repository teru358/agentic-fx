"""ImproveSupervisor の wave/slot 状態機械 (設計書 §3.1、プラン §8.1-17〜19/26)。"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from agentic_fx.core.improve_supervisor import ImproveSupervisor
from agentic_fx.store import db as db_mod
from agentic_fx.store import improve_waves


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "agentic.db"
    c = db_mod.connect(path)
    db_mod.init_db(c)
    return c


# Test helpers for 9.2 (and later sections)
class _FixedClock:
    """Fixed clock for testing."""
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _fake_settings(parallel: int) -> Any:
    """Minimal fake Settings for 9.2 wave creation testing.
    Real Settings consumer path (config.py ScheduleSettings) not yet in scope.
    """
    class _Cadence:
        improve = "weekly"
        improve_at = "Sat 03:00"

    class _Improve:
        def __init__(self, p: int) -> None:
            self.parallel = p

    class _FakeSettings:
        def __init__(self) -> None:
            self.schedule = _Cadence()
            self.improve = _Improve(parallel)
            self.display_timezone = "UTC"

    return _FakeSettings()


# Tests for 9.2: wave + slot Tx-0 creation + M=0

def test_wave_creation_writes_wave_and_m_slots_in_one_tx(conn):
    """M = min(parallel, 空き) 個の reserved slot が wave 行と**同じ commit**
    で現れる (part-way な状態が外部から観測できない — 単一 tx pin)。"""
    now = datetime(2026, 8, 22, 3, 0)
    ok = improve_waves.create_wave_and_slots(conn, period_key="2026-W34", now=now, expected=2, commit=True)
    assert ok is True
    wave = conn.execute(
        "SELECT * FROM improve_waves WHERE period_key='2026-W34'").fetchone()
    assert wave["expected"] == 2
    slots = conn.execute(
        "SELECT k, status, mission_id FROM improve_wave_slots "
        "WHERE wave_period_key='2026-W34' ORDER BY k").fetchall()
    assert [dict(s) for s in slots] == [
        {"k": 0, "status": "reserved", "mission_id": None},
        {"k": 1, "status": "reserved", "mission_id": None},
    ]


def test_wave_creation_is_idempotent_second_call_no_op(conn):
    """`INSERT OR IGNORE` — 同じ period_key への 2 回目の呼び出しは
    rowcount=0 (起動権を得られない) で slot も増えない。ImproveSupervisor.tick
    が 2 回呼ばれても、再試行は発生しない (period は消費済み、queue は1回分のみ)。"""
    now = datetime(2026, 8, 22, 3, 0)
    sup = ImproveSupervisor(capacity=10, root=Path("/nonexistent"),
                             settings=_fake_settings(parallel=2),
                             clock=_FixedClock(now),
                             db_path=Path(conn.execute("PRAGMA database_list").fetchone()[2]),
                             stop_event=threading.Event())
    sup._conn_for_test = conn
    # First tick で wave + 2 slots を作成
    sup.tick(now)
    # queue に 2 個の (period_key, k, -1) が入っているはず
    queued_1st = []
    while not sup._slot_queue.empty():
        queued_1st.append(sup._slot_queue.get_nowait())
    assert len(queued_1st) == 2
    # Second tick で同じ period で再試行
    sup.tick(now)
    # queue には何も追加されない (period は既に消費済み)
    queued_2nd = []
    while not sup._slot_queue.empty():
        queued_2nd.append(sup._slot_queue.get_nowait())
    assert len(queued_2nd) == 0


def test_m_zero_creates_no_wave_row(conn, monkeypatch):
    """M=0 (空きスロット無し) は何も書かない — 次 tick で再試行できる
    (period 非消費)。ImproveSupervisor.tick が M を計算して 0 なら
    何もしないこと — create_wave_and_slots を呼ばず、queue も populate しない。"""
    call_count = []
    original_create = improve_waves.create_wave_and_slots
    def spy_create(*args, **kwargs):
        call_count.append(1)
        return original_create(*args, **kwargs)
    monkeypatch.setattr(improve_waves, "create_wave_and_slots", spy_create)

    sup = ImproveSupervisor(capacity=0, root=Path("/nonexistent"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(datetime(2026, 8, 22, 3, 0)),
                             db_path=Path(conn.execute("PRAGMA database_list").fetchone()[2]),
                             stop_event=threading.Event())
    sup._conn_for_test = conn  # テストシーム
    sup.tick(datetime(2026, 8, 22, 3, 0))
    # M3 mutation check: create_wave_and_slots should not be called when m=0
    assert len(call_count) == 0, f"create_wave_and_slots should not be called when m=0, but was called {len(call_count)} times"
    n = conn.execute("SELECT count(*) c FROM improve_waves").fetchone()["c"]
    assert n == 0


# Tests for 9.3: 3-way 起動プロトコル

class _RecordingFakeWorkerRunner:
    """WorkerRunner の代役。呼び出し順序だけを記録する (Task 1/4/5 の
    実プロトコルはここでは検証しない — 骨格 Interfaces 節の
    `WorkerRunner(..., worker_profile="improve", run_context=ctx)` の
    構築タイミングのみを外形的に確認する)。"""

    def __init__(self, events: list):
        self._events = events

    def spawn(self):
        self._events.append("spawn")
        return "ready"  # spawn 直後に ready を返す fake

    def send_go(self):
        self._events.append("go")

    def run_to_completion(self):
        self._events.append("run")
        return {"status": "completed", "output": {}}


class _FakeImproveLoop:
    """ImproveLoop (Task 10) の代役。Tx-0 の slot claim は `prepare()` の
    責務として fake でも忠実に再現する — `claim_slot` を実際に呼び
    `reserved→claimed` を tx で確定させる (fake が本物の `improve_waves`
    を呼ぶことで、Task 9 側は「claim は prepare の内側で起きる」という
    契約だけを検証すればよい)。"""

    def __init__(self, conn, worker_runner, *, mission_id=999):
        self._conn = conn
        self._worker_runner = worker_runner
        self._mission_id = mission_id
        self.committed: list[tuple] = []

    def prepare(self, *, slot_key, now):
        period_key, k = slot_key
        claimed = improve_waves.claim_slot(
            self._conn, period_key=period_key, k=k, mission_id=self._mission_id,
            now=now, commit=True)
        if not claimed:
            raise RuntimeError(
                f"slot claim failed for {slot_key!r} — "
                "already claimed by a concurrent process")
        return f"mission-{self._mission_id}", f"ctx-{self._mission_id}", \
            self._worker_runner

    def commit(self, *, mission, ctx, result, now):
        self.committed.append((mission, ctx, result))


def test_three_way_launch_order_prepare_then_spawn_then_ready_then_running_commit_then_go(
        conn, monkeypatch):
    """prepare(Tx-0, claim含む) → spawn → ready 受信 → running commit
    (この時点でまだ go を送らない) → go → run → commit()、の順序を固定する。"""
    events: list[str] = []
    now = datetime(2026, 8, 22, 3, 0)
    improve_waves.create_wave_and_slots(conn, period_key="2026-W34", now=now, expected=1, commit=True)

    sup = ImproveSupervisor(capacity=1, root=Path("/tmp"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(now), db_path=Path("x"),
                             stop_event=threading.Event())
    sup._conn_for_test = conn
    fake_loop = _FakeImproveLoop(
        conn, _RecordingFakeWorkerRunner(events))
    sup._improve_loop = fake_loop
    sup._launch_slot("2026-W34", 0)

    row = conn.execute(
        "SELECT status, mission_id FROM improve_wave_slots "
        "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
    assert row["status"] == "running"
    assert row["mission_id"] == 999
    assert events == ["spawn", "go", "run"]
    assert len(fake_loop.committed) == 1


def test_go_is_not_sent_before_running_commit(conn):
    """running への commit が完了する前に go を送らないことを、send_go の
    内部で slot 状態を読み返して確認する fake WorkerRunner 経由で確認する。
    commit 前に go 呼び出しが記録されたら fail。"""
    events: list[str] = []
    now = datetime(2026, 8, 22, 3, 0)
    improve_waves.create_wave_and_slots(conn, period_key="2026-W34", now=now, expected=1, commit=True)

    class _OrderCheckingRunner(_RecordingFakeWorkerRunner):
        def send_go(self):
            row = conn.execute(
                "SELECT status FROM improve_wave_slots "
                "WHERE wave_period_key='2026-W34' AND k=0").fetchone()
            assert row["status"] == "running", (
                "go was sent before the running-commit was visible")
            super().send_go()

    sup = ImproveSupervisor(capacity=1, root=Path("/tmp"),
                             settings=_fake_settings(parallel=1),
                             clock=_FixedClock(now), db_path=Path("x"),
                             stop_event=threading.Event())
    sup._conn_for_test = conn
    sup._improve_loop = _FakeImproveLoop(conn, _OrderCheckingRunner(events))
    sup._launch_slot("2026-W34", 0)
