from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest

from agentic_fx.plugin import deploy_disable, deploy_reconcile
from agentic_fx.plugin import signal_producer as sp
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.resolve import ResolvedIndicatorSet
from agentic_fx.plugin.signal_producer import SignalProducer
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store import signals as signals_store
from agentic_fx.store.db import connect
from tests.core.test_signal_maintenance_concurrency import (
    TICK, _app, _count, _ProducerWithSandbox)
from tests.plugin._conc_support import (
    FIRST_TICK, Activity, ConcWorld, Sandbox, lock_is_free)
from tests.plugin._crash import CrashPoint, crash_at, crashed
from tests.plugin.conftest import SETTINGS
from tests.plugin.deploy_world import at
from tests.plugin.test_signal_producer import H, _open_result, _seed_flat

NOW = at(200)


def _cursor(producer, world):
    meta = world.meta()
    return producer._cursor.get((meta.name, meta.content_hash, "USDJPY"))


def _evaluate(world, producer=None, sandbox=None, activity=None):
    return world.evaluate(producer or SignalProducer(), FIRST_TICK,
                          sandbox or Sandbox(_open_result()), activity or Activity())


def test_a_crash_after_evaluation_and_before_the_lock_inserts_nothing_and_the_next_producer_reevaluates(
        tmp_path, monkeypatch):
    world = ConcWorld(tmp_path)
    crashed_producer = SignalProducer()
    crash_at(monkeypatch, sp, "acquire_name_locks")

    crashed(world, _evaluate, world, crashed_producer)

    assert world.total() == 0
    cursor = _cursor(crashed_producer, world)
    assert cursor is None or cursor < H
    sandbox = Sandbox(_open_result())
    assert _evaluate(world, SignalProducer(), sandbox) == 1
    assert len(sandbox.calls) == 1 and world.pending() == 1


def test_a_crash_inside_the_lock_before_the_insert_releases_the_lock_and_inserts_nothing(
        tmp_path, monkeypatch):
    world = ConcWorld(tmp_path)
    producer = SignalProducer()
    crash_at(monkeypatch, sp, "source_state", when="after")

    crashed(world, _evaluate, world, producer)

    assert world.total() == 0
    assert lock_is_free(world.root, "s")
    cursor = _cursor(producer, world)
    assert cursor is None or cursor < H
    assert _evaluate(world) == 1 and world.pending() == 1


def test_a_signal_committed_before_a_crash_is_not_duplicated_by_the_next_producer(
        tmp_path, monkeypatch):
    world = ConcWorld(tmp_path)
    crash_at(monkeypatch, signals_store, "add", when="after")

    crashed(world, _evaluate, world)

    assert world.pending() == 1
    assert _evaluate(world) == 0
    assert world.total() == 1


def test_a_signal_committed_before_a_crash_is_abandoned_by_a_following_disable(
        tmp_path, monkeypatch):
    world = ConcWorld(tmp_path)
    crash_at(monkeypatch, signals_store, "add", when="after")
    crashed(world, _evaluate, world)
    assert world.pending() == 1

    outcome = deploy_disable.disable_plugin(
        world.conn, world.root, "s", revoke=False, force=False, now=NOW, settings=SETTINGS)

    assert outcome.outcome == "disabled"
    assert world.signal_rows() == [("s", "abandoned")]
    op = deploy_store.list_by_name(world.conn, "s")[0]
    linked = world.conn.execute(
        "SELECT status_at_link FROM plugin_deploy_op_signals WHERE op_id=?",
        (op["op_id"],)).fetchall()
    assert [r["status_at_link"] for r in linked] == ["pending"]
    assert not (world.root / "s").exists()
    assert _evaluate(world) == 0 and world.total() == 1


def test_a_crash_between_the_intent_commit_and_the_unlink_keeps_a_producer_from_inserting(
        tmp_path, monkeypatch):
    from tests.plugin._crash import crash_after_intent_commit

    world = ConcWorld(tmp_path)
    crash_after_intent_commit(monkeypatch)
    crashed(world, deploy_disable.disable_plugin, world.conn, world.root, "s",
            revoke=False, force=False, now=NOW, settings=SETTINGS)
    activity = Activity()
    assert deploy_store.get_open_by_name(world.conn, "s") is not None

    assert _evaluate(world, activity=activity) == 0

    assert world.total() == 0
    assert activity.names() == ["plugin_skipped_not_live"]
    deploy_reconcile.reconcile_deploy_ops(
        world.conn, world.root, now=NOW, settings=SETTINGS)
    assert _evaluate(world) == 0 and world.total() == 0


# ---- a crash while the evaluation callable runs ----

def _signal_app(tmp_path):
    app = _app(tmp_path)
    source = ("mt5-live" if app.settings.datafeed.primary == "mt5"
              else app.settings.datafeed.primary)
    seed = connect(tmp_path / "data" / "agentic.db")
    _seed_flat(seed, H - timedelta(hours=3), 6 * 60 + 1, source=source)
    seed.close()
    meta = PluginMeta(
        name="sig", kind="signal", path=Path("/not-used"), params={},
        timeframe="1h", pairs=("USDJPY",), max_bars=2, content_hash="a" * 64)
    return app, meta


def _wire_maintenance(monkeypatch, tmp_path, meta, producer, sandbox, activity):
    from agentic_fx import service

    real = service._run_signal_maintenance

    def maintenance(**kwargs):
        kwargs.update(
            signal_producer=_ProducerWithSandbox(producer, sandbox),
            approved=[meta], plugins_root=None, activity=activity,
            resolved_by_identity={(meta.name, meta.content_hash):
                                  ResolvedIndicatorSet.empty(tmp_path)})
        return real(**kwargs)

    monkeypatch.setattr(service, "_run_signal_maintenance", maintenance)


def test_a_crash_during_the_evaluation_callable_leaves_the_core_lock_free_and_the_next_tick_inserts(
        tmp_path, monkeypatch):
    app, meta = _signal_app(tmp_path)
    taken = []
    activity = Activity()

    def dying_sandbox(_meta, _payload, *, settings):
        # an uncommitted write on the maintenance connection dies with the process
        app.conn_signal.execute("BEGIN IMMEDIATE")
        app.conn_signal.execute(
            "INSERT INTO signals (plugin, content_hash, pair, timeframe, bar_ts, kind, "
            "payload_json, created_at) VALUES ('ghost', 'c', 'USDJPY', '1h', ?, 'signal', "
            "'{}', ?)", (H.isoformat(), H.isoformat()))
        raise CrashPoint("evaluation")

    first = SignalProducer()
    _wire_maintenance(monkeypatch, tmp_path, meta, first, dying_sandbox, activity)
    try:
        with pytest.raises(CrashPoint):
            app.scheduler.run_signal_maintenance(TICK)

        def other_thread():
            with app.core_lock:
                taken.append(True)

        contender = threading.Thread(target=other_thread)
        contender.start()
        contender.join(timeout=10)
        assert not contender.is_alive()
        assert taken == [True]
        assert not app.core_lock.owned_by_current_thread()
        app.conn_signal.close()
        observer = connect(tmp_path / "data" / "agentic.db")
        try:
            assert observer.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
        finally:
            observer.close()
        cursor = first._cursor.get((meta.name, meta.content_hash, "USDJPY"))
        assert cursor is None or cursor < H

        monkeypatch.undo()
        app.conn_signal = connect(tmp_path / "data" / "agentic.db")
        survivor = SignalProducer()
        _wire_maintenance(monkeypatch, tmp_path, meta, survivor, lambda *_a, **_k: {
            "signals": [{"direction": "long", "strength": 0.8, "rationale": "go"}]}, activity)
        app.scheduler.run_signal_maintenance(TICK)

        assert _count(app.conn_core) == 1
        assert survivor._cursor[(meta.name, meta.content_hash, "USDJPY")] == H
    finally:
        app.close()
