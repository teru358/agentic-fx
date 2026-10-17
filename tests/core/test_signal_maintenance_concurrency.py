"""Signal maintenance next to the trading core: lock scope and SQLite busy policy."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx import service
from agentic_fx.core.contracts import FixedClock
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.resolve import ResolvedIndicatorSet
from agentic_fx.plugin.signal_producer import SignalProducer
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import _scheduler_tick_once, build_app
from agentic_fx.store import signals
from agentic_fx.store.db import connect
from tests.plugin._conc_support import Activity
from tests.plugin.test_signal_producer import H, _seed_flat
from tests.store.test_rag import FakeEmbedding
from tests.test_service_app import _init

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
TICK = H + timedelta(hours=1, seconds=31)


def _app(tmp_path):
    _init(tmp_path)
    return build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())


def _add_signal(conn, plugin: str) -> int | None:
    return signals.add(
        conn, plugin=plugin, content_hash="c" * 64, pair="USDJPY",
        timeframe="1h", bar_ts=H.isoformat(), kind="signal",
        payload={"direction": "long"}, now=NOW)


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]


# --- maintenance runs outside core_lock --------------------------------------

def test_another_thread_can_take_core_lock_while_maintenance_runs(
        tmp_path, monkeypatch):
    app = _app(tmp_path)
    app.ingest = None  # keep the outage state ready; no price fetch is needed
    inside = threading.Event()
    proceed = threading.Event()
    seen = {}

    def blocked_maintenance(**kwargs):
        seen["owns_core_lock"] = app.core_lock._is_owned()
        seen["conn"] = kwargs["conn"]
        inside.set()
        assert proceed.wait(30)

    monkeypatch.setattr(service, "_run_signal_maintenance", blocked_maintenance)
    ticker = threading.Thread(target=_scheduler_tick_once, args=(app,))
    ticker.start()
    try:
        assert inside.wait(30), "maintenance never started"
        taken = []

        def other_thread():
            got = app.core_lock.acquire(timeout=1)
            taken.append(got)
            if got:
                app.core_lock.release()

        contender = threading.Thread(target=other_thread)
        contender.start()
        contender.join(timeout=10)
        assert taken == [True]
    finally:
        proceed.set()
        ticker.join(timeout=30)
        app.close()
    assert not ticker.is_alive()
    assert seen["owns_core_lock"] is False
    assert seen["conn"] is not app.conn_core


# --- busy policy -------------------------------------------------------------

def test_core_and_signal_connections_both_commit_when_contention_ends_within_busy_timeout(
        tmp_path):
    app = _app(tmp_path)
    outcome = {}
    started = threading.Event()
    done = threading.Event()

    def signal_side():
        started.set()
        try:
            outcome["row_id"] = _add_signal(app.conn_signal, "from_signal")
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc
        done.set()

    writer = threading.Thread(target=signal_side)
    try:
        app.conn_core.execute("BEGIN IMMEDIATE")
        writer.start()
        assert started.wait(30)
        deadline = time.monotonic() + 30
        while not app.conn_signal.in_transaction:
            assert time.monotonic() < deadline, "signal side never began its write"
            time.sleep(0.005)
        assert not done.wait(0.3), "the signal write must wait for the core writer"
        assert _add_signal(app.conn_core, "from_core") is not None
        assert done.wait(30)
        writer.join(timeout=30)
        assert "error" not in outcome and outcome["row_id"] is not None
        assert not app.conn_core.in_transaction
        assert not app.conn_signal.in_transaction
        observer = connect(tmp_path / "data" / "agentic.db")
        try:
            assert sorted(r["plugin"] for r in observer.execute(
                "SELECT plugin FROM signals")) == ["from_core", "from_signal"]
        finally:
            observer.close()
    finally:
        app.close()


class _ProducerWithSandbox:
    def __init__(self, producer, sandbox):
        self.producer, self.sandbox = producer, sandbox

    def evaluate_due_plugins(self, **kwargs):
        return self.producer.evaluate_due_plugins(
            sandbox_run=self.sandbox, **kwargs)


def test_core_writer_holding_past_busy_timeout_fails_maintenance_cleanly_and_next_tick_recovers(
        tmp_path, monkeypatch):
    app = _app(tmp_path)
    source = ("mt5-live" if app.settings.datafeed.primary == "mt5"
              else app.settings.datafeed.primary)
    seed = connect(tmp_path / "data" / "agentic.db")
    _seed_flat(seed, H - timedelta(hours=3), 6 * 60 + 1, source=source)
    seed.close()
    meta = PluginMeta(
        name="sig", kind="signal", path=Path("/not-used"), params={},
        timeframe="1h", pairs=("USDJPY",), max_bars=2, content_hash="a" * 64)
    producer, activity = SignalProducer(), Activity()
    holds = {"core": True}

    def sandbox(_meta, _payload, *, settings):
        if holds["core"]:
            app.conn_core.execute("BEGIN IMMEDIATE")
        return {"signals": [{"direction": "long", "strength": 0.8,
                             "rationale": "go"}]}

    real = service._run_signal_maintenance

    def maintenance(**kwargs):
        kwargs.update(
            signal_producer=_ProducerWithSandbox(producer, sandbox),
            approved=[meta], plugins_root=None, activity=activity,
            resolved_by_identity={(meta.name, meta.content_hash):
                                  ResolvedIndicatorSet.empty(tmp_path)})
        return real(**kwargs)

    monkeypatch.setattr(service, "_run_signal_maintenance", maintenance)
    key = (meta.name, meta.content_hash, "USDJPY")
    try:
        signal_conn = app.conn_signal
        signal_conn.execute("PRAGMA busy_timeout=50")
        before = _count(app.conn_core)

        app.scheduler.run_signal_maintenance(TICK)

        assert app.conn_core.in_transaction, "the core writer is still holding"
        assert app.conn_signal is signal_conn
        assert signal_conn.in_transaction is False
        assert _count(app.conn_core) == before == 0
        assert activity.names() == ["plugin_eval_failed"]
        summary = activity.events[0][1]
        assert "result=internal_error" in summary and "locked" not in summary
        assert producer._cursor.get(key) is None or producer._cursor[key] < H

        app.conn_core.rollback()
        holds["core"] = False
        app.scheduler.run_signal_maintenance(TICK)

        assert _count(app.conn_core) == 1
        assert producer._cursor[key] == H
        assert activity.names() == ["plugin_eval_failed"]
        assert signal_conn.in_transaction is False
    finally:
        app.close()
