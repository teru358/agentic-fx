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
