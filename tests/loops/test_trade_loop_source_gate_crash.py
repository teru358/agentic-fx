"""Deploy 変更と専用 signal 採否の排他・回復。"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops import trade_loop
from agentic_fx.plugin import deploy_disable, deploy_retire, deploy_rollback, switch
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import missions, signals

from tests.loops.test_trade_loop import NOW, SETTINGS, _loop
from tests.loops.test_trade_loop_signal import _add_signal
from tests.plugin._conc_support import lock_is_free
from tests.plugin._crash import CrashPoint, crash_at
from tests.plugin.deploy_world import World
from agentic_fx.store.db import connect


ACCEPT = {"decision": "accept", "reject_code": None}


def _loop_on(world, tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult("completed", ACCEPT, [])],
                             conn=world.conn)
    loop._plugins_root = world.root
    loop.econ.window_view.return_value.in_window = False
    loop.executor.gather_open_snapshot = lambda *args, **kwargs: SimpleNamespace(
        captured_at=NOW)
    loop.executor.open_from_snapshot = lambda *args, **kwargs: {
        "result": "submitted", "order_id": None, "reasons": []}
    return conn, loop


def _generations(world):
    first = world.version("strat", "s1", kind="strategy")
    second = world.version("strat", "s2", kind="strategy")
    world.deploy("strat", first, None, 1)
    world.deploy("strat", second, first, 2)
    return first, second


def _run_disable(world):
    return deploy_disable.disable_plugin(
        world.conn, world.root, "strat", revoke=False, force=False,
        now=NOW, settings=SETTINGS)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.mark.parametrize("operation", ["disable", "rollback", "retire"])
def test_completed_deploy_change_is_seen_before_decision_commit(
        world, tmp_path, monkeypatch, operation):
    if operation == "retire":
        os.rename(world.version("strat", "plain", kind="strategy"),
                  world.root / "strat")
        content = world.content(world.root / "strat")
        apply_change = lambda: deploy_retire.retire_plugin(
            world.conn, world.root, "strat", force=False, now=NOW,
            settings=SETTINGS)
    else:
        first, second = _generations(world)
        content = world.content(second)
        apply_change = ((lambda: _run_disable(world)) if operation == "disable" else lambda:
                        deploy_rollback.rollback_plugin(
                            world.conn, world.root, "strat", to=first.name,
                            revoke=False, force=False, now=NOW, settings=SETTINGS))
    conn, loop = _loop_on(world, tmp_path)
    signal_id = signals.add(
        conn, plugin="strat", content_hash=content, pair="USDJPY", timeframe="1h",
        bar_ts=(NOW - timedelta(hours=1)).isoformat(),
        kind="strategy", payload={"action": "open", "pair": "USDJPY",
        "direction": "long", "entry_type": "market", "horizon": "day",
        "stop_loss": 147, "take_profit": 150}, now=NOW,
        contract_version="executable_v1")
    real_lock = trade_loop.acquire_name_locks

    @contextmanager
    def changed_before_lock(*args, **kwargs):
        apply_change()
        with real_lock(*args, **kwargs):
            yield

    monkeypatch.setattr(trade_loop, "acquire_name_locks", changed_before_lock)
    loop.run_once("signal")

    row = conn.execute(
        "SELECT status,abandon_reason,abandoned_at FROM signals WHERE id=?",
        (signal_id,)).fetchone()
    assert tuple(row) == ("abandoned", "source_inactive", NOW.isoformat())
    assert conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trade_intents").fetchone()[0] == 0


def test_deploy_change_waits_for_decision_commit_lock(world, tmp_path, monkeypatch):
    _first, second = _generations(world)
    conn, loop = _loop_on(world, tmp_path)
    signal_id = signals.add(
        conn, plugin="strat", content_hash=world.content(second), pair="USDJPY",
        timeframe="1h", bar_ts=(NOW - timedelta(hours=1)).isoformat(),
        kind="strategy", payload={"action": "open", "pair": "USDJPY",
        "direction": "long", "entry_type": "market", "horizon": "day",
        "stop_loss": 147, "take_profit": 150}, now=NOW,
        contract_version="executable_v1")
    started = threading.Event()
    finished = threading.Event()
    outcome = {}

    def disable_after_commit_lock():
        other = connect(world.db_path)
        try:
            started.set()
            outcome["result"] = deploy_disable.disable_plugin(
                other, world.root, "strat", revoke=False, force=False,
                now=NOW, settings=SETTINGS, deadline=None)
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc
        finally:
            other.close()
            finished.set()

    worker = threading.Thread(target=disable_after_commit_lock)
    real_commit = trade_loop.signal_decisions.commit_accept

    def commit(*args, **kwargs):
        worker.start()
        assert started.wait(5)
        assert not finished.wait(0.05)
        assert not lock_is_free(world.root, "strat")
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(trade_loop.signal_decisions, "commit_accept", commit)
    try:
        loop.run_once("signal")
    finally:
        worker.join(timeout=10)

    assert not worker.is_alive() and "error" not in outcome
    assert finished.is_set()
    assert conn.execute(
        "SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()[0] == "consumed"
    assert conn.execute("SELECT decision FROM signal_decisions WHERE signal_id=?",
                        (signal_id,)).fetchone()[0] == "accept"


def test_executor_crash_after_commit_keeps_final_decision_and_consumption(
        tmp_path, monkeypatch):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult("completed", ACCEPT, [])])
    signal_id = _add_signal(conn)
    loop.econ.window_view.return_value.in_window = False
    loop.executor.gather_open_snapshot = lambda *args, **kwargs: SimpleNamespace(
        captured_at=NOW)
    crash_at(monkeypatch, loop.executor, "open_from_snapshot")

    with pytest.raises(CrashPoint):
        loop.run_once("signal")

    mission_id = conn.execute("SELECT id FROM missions").fetchone()[0]
    conn.execute("UPDATE missions SET status='running', finished_at=NULL WHERE id=?",
                 (mission_id,))
    conn.commit()
    conn.close()
    from agentic_fx.store.db import connect
    conn = connect(tmp_path / "t.db")
    result = missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    assert result["missions_recovered"] == 1
    assert conn.execute("SELECT status FROM signals WHERE id=?",
                        (signal_id,)).fetchone()[0] == "consumed"
    assert conn.execute("SELECT decision FROM signal_decisions WHERE signal_id=?",
                        (signal_id,)).fetchone()[0] == "accept"
    assert conn.execute("SELECT COUNT(*) FROM signal_shadow WHERE signal_id=?",
                        (signal_id,)).fetchone()[0] == 1


def test_interrupted_claim_before_decision_returns_to_pending(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [])
    signal_id = _add_signal(conn)
    mission_id = missions.start(conn, "trade", "local", "m", NOW,
                                trigger="signal:sig1")
    claimed = signals.claim_oldest(
        conn, mission_id=mission_id, now=NOW,
        freshness_bars=SETTINGS.plugin.signal_freshness_bars)
    assert claimed is not None and claimed["id"] == signal_id

    recovered = missions.recover_interrupted(
        conn, now=NOW + timedelta(seconds=10), max_requeue=2)

    assert recovered["missions_recovered"] == 1
    assert conn.execute(
        "SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()[0] == "interrupted"
    assert tuple(conn.execute(
        "SELECT status,requeue_count,abandon_reason,abandoned_at FROM signals "
        "WHERE id=?", (signal_id,)).fetchone()) == ("pending", 1, None, None)
    assert conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM signal_shadow").fetchone()[0] == 0
