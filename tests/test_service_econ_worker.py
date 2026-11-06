"""slice worker の接続・構築・起動と停止。"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.econ_slice_worker import EconSliceWorker
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import (
    _busy_resources_after_join, build_app, run_service)
from tests.store.test_rag import FakeEmbedding
from tests.test_service_app import _init, _no_real_network, _seam_app

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
THREAD = "afx-econ-slices"


def _busy_timeout(conn) -> int:
    return conn.execute("PRAGMA busy_timeout").fetchone()[0]


def _build(tmp_path):
    _init(tmp_path)
    return build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())


def test_build_app_builds_the_worker_with_its_own_connections(tmp_path):
    app = _build(tmp_path)
    try:
        assert isinstance(app.econ_worker, EconSliceWorker)
        read, write = app.conn_econ_read, app.conn_econ_write
        assert len({id(read), id(write), id(app.conn_core), id(app.conn_shell),
                    id(app.conn_signal), id(app.conn_admission)}) == 6
        assert _busy_timeout(write) == 0
        assert _busy_timeout(app.conn_core) == 5000
        assert _busy_timeout(app.conn_shell) == 5000
        assert _busy_timeout(app.conn_signal) == 5000
        assert _busy_timeout(app.conn_admission) == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            read.execute("DELETE FROM ohlcv_slices")
        worker = app.econ_worker
        assert worker._read is read and worker._write_conn is write
        assert worker._core_lock is app.core_lock
        assert worker._admission is app.writer_admission
        assert worker._on_complete is None
        assert worker._now() == NOW
    finally:
        app.close()


def test_the_worker_reads_quote_pip_and_assumed_spread_like_the_trade_loop(tmp_path):
    app = _build(tmp_path)
    try:
        pair = app.settings.pairs[0]
        seen = []
        app.executor.quote_fn = lambda p: seen.append(p) or "quote"
        assert app.econ_worker._quote_fn(pair) == "quote" and seen == [pair]
        assert app.econ_worker._pip_size_fn(pair) == app.executor.spec_fn(pair).pip_size
        rule = app.settings.risk.pair_rules[pair]
        assert app.econ_worker._assumed_fn(pair) == rule.assumed_spread_pips
        assert app.econ_worker._assumed_fn("NOPAIR") == 0.0
    finally:
        app.close()


def test_close_closes_the_worker_connections(tmp_path):
    app = _build(tmp_path)
    read, write = app.conn_econ_read, app.conn_econ_write
    assert app.close() == []
    for conn in (read, write):
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_close_leaves_the_worker_connections_open_while_the_worker_is_busy(tmp_path):
    app = _build(tmp_path)
    read, write = app.conn_econ_read, app.conn_econ_write
    try:
        busy = _busy_resources_after_join(False, False, econ_still_busy=True)
        assert busy == {"conn_econ_read", "conn_econ_write"}
        assert sorted(app.close(busy_resources=busy)) == [
            "conn_econ_read", "conn_econ_write"]
        assert read.execute("SELECT 1").fetchone()[0] == 1
        assert write.execute("SELECT 1").fetchone()[0] == 1
    finally:
        read.close()
        write.close()
    assert _busy_resources_after_join(False, False) == frozenset()


class _FakeWorker:
    def __init__(self, on_run=None):
        self.runs = []
        self.on_run = on_run

    def run(self, stop):
        self.runs.append((threading.current_thread().name, stop))
        if self.on_run is not None:
            self.on_run(stop)


def _serve(tmp_path, app, stop_event):
    with _no_real_network(), \
         patch("agentic_fx.service.build_app", return_value=app), \
         patch("agentic_fx.service.signal.signal"):
        return run_service(tmp_path, daemon=True, _stop_event=stop_event)


def _alive():
    return [t for t in threading.enumerate() if t.name == THREAD]


def test_run_service_runs_the_worker_in_its_thread_until_stop(tmp_path):
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = threading.Event()
    syncs = []

    def sync(*, now=None):
        syncs.append(threading.current_thread().name)

    def wait_for_stop(stop):
        stop_event.set()
        assert stop.wait(timeout=10)

    app.econ.sync_sets = sync
    app.econ_worker = _FakeWorker(wait_for_stop)
    assert _serve(tmp_path, app, stop_event) == 0
    # 起動時補完は build_app が済ませる。worker の thread は台帳を揃え直さない。
    assert syncs == []
    [(name, stop)] = app.econ_worker.runs
    assert name == THREAD and stop is stop_event
    assert _alive() == []


def test_a_worker_that_raises_does_not_stop_the_daemon(tmp_path):
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = threading.Event()

    def boom(stop):
        stop_event.set()
        raise RuntimeError("worker broke")

    app.econ_worker = _FakeWorker(boom)
    with patch("agentic_fx.service._log") as log:
        assert _serve(tmp_path, app, stop_event) == 0
    assert [c.args[0] for c in log.exception.call_args_list] == [
        "econ slice worker stopped"]
    assert _alive() == []


def test_the_real_worker_stops_on_the_stop_event_and_run_service_closes_cleanly(tmp_path):
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = threading.Event()
    stop_event.set()
    assert _serve(tmp_path, app, stop_event) == 0
    assert _alive() == []
    with pytest.raises(sqlite3.ProgrammingError):
        app.conn_econ_write.execute("SELECT 1")


def test_a_worker_that_does_not_stop_keeps_its_connections_open(tmp_path):
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = threading.Event()
    release = threading.Event()
    closed = {}
    original_close = app.close

    def spy_close(*, busy_resources=frozenset()):
        closed["busy"] = busy_resources
        return original_close(busy_resources=busy_resources)

    def stuck(stop):
        stop_event.set()
        release.wait(timeout=30)

    app.close = spy_close
    app.econ_worker = _FakeWorker(stuck)
    try:
        with patch("agentic_fx.service._ECON_WORKER_JOIN_TIMEOUT_SEC", 0.2):
            assert _serve(tmp_path, app, stop_event) == 0
        assert {"conn_econ_read", "conn_econ_write"} <= closed["busy"]
        assert "conn_core" not in closed["busy"]
        assert app.conn_econ_write.execute("SELECT 1").fetchone()[0] == 1
    finally:
        release.set()
        for t in _alive():
            t.join(timeout=30)
        app.conn_econ_read.close()
        app.conn_econ_write.close()
    assert _alive() == []
