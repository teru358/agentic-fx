"""slice worker の接続・構築・起動と停止。"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.ops.contracts import ShellCaller
from agentic_fx.core.econ_slice_worker import CapacityMeasurer, EconSliceWorker
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.store.instance_lock import (
    InstanceAlreadyRunning, acquire_instance_lock)
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
        assert busy == {"conn_econ_read", "conn_econ_write", "instance_lock"}
        assert sorted(app.close(busy_resources=busy)) == [
            "conn_econ_read", "conn_econ_write", "instance_lock"]
        assert read.execute("SELECT 1").fetchone()[0] == 1
        assert write.execute("SELECT 1").fetchone()[0] == 1
    finally:
        read.close()
        write.close()
        app.instance_lock.close()
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


def test_a_worker_that_does_not_stop_keeps_its_connections_and_the_lock(tmp_path):
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = threading.Event()
    release = threading.Event()
    closed = {}
    original_close = app.close
    original_write = app.activity.write
    stopped = []

    def spy_close(*, busy_resources=frozenset()):
        closed["busy"] = busy_resources
        return original_close(busy_resources=busy_resources)

    def spy_write(category, kind, *args, **kwargs):
        if kind == "service_stopped":
            stopped.append(args[0] if args else kwargs)
        return original_write(category, kind, *args, **kwargs)

    def stuck(stop):
        stop_event.set()
        release.wait(timeout=30)

    app.close = spy_close
    app.activity.write = spy_write
    app.econ_worker = _FakeWorker(stuck)
    try:
        with patch("agentic_fx.service._ECON_WORKER_JOIN_TIMEOUT_SEC", 0.2):
            assert _serve(tmp_path, app, stop_event) == 1
        assert {"conn_econ_read", "conn_econ_write",
                "instance_lock"} <= closed["busy"]
        assert "conn_core" not in closed["busy"]
        assert app.conn_econ_write.execute("SELECT 1").fetchone()[0] == 1
        assert len(stopped) == 1 and stopped[0].startswith("shutdown_timeout")
        with pytest.raises(InstanceAlreadyRunning):
            acquire_instance_lock(tmp_path / "data")
    finally:
        release.set()
        for t in _alive():
            t.join(timeout=30)
        app.conn_econ_read.close()
        app.conn_econ_write.close()
        app.instance_lock.close()
    assert _alive() == []


def test_a_worker_that_cannot_start_does_not_stop_the_service_or_skip_the_stop(tmp_path):
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = threading.Event()
    stop_event.set()
    original_start = threading.Thread.start
    written = []
    original_write = app.activity.write

    def start(self):
        if self.name == THREAD:
            raise RuntimeError("can't start new thread")
        return original_start(self)

    def spy_write(category, kind, *args, **kwargs):
        written.append(kind)
        return original_write(category, kind, *args, **kwargs)

    app.activity.write = spy_write
    with patch.object(threading.Thread, "start", start), \
         patch("agentic_fx.service._log") as log:
        assert _serve(tmp_path, app, stop_event) == 0
    assert "econ slice worker could not start" in [
        c.args[0] for c in log.exception.call_args_list]
    assert "econ_slice_worker_start_failed" in written
    assert written[-1] == "service_stopped"
    names = {t.name for t in threading.enumerate()}
    assert not names & {"scheduler", "watchdog", THREAD}
    with pytest.raises(sqlite3.ProgrammingError):
        app.conn_econ_write.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        app.conn_core.execute("SELECT 1")


def test_the_real_worker_runs_cycles_and_then_stops_on_the_stop_event(tmp_path):
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = threading.Event()
    cycled = threading.Event()
    cycle_threads = []
    original_cycle = app.econ_worker.run_cycle

    def run_cycle(*args, **kwargs):
        cycle_threads.append(threading.current_thread().name)
        try:
            return original_cycle(*args, **kwargs)
        finally:
            cycled.set()

    def stopper():
        if cycled.wait(timeout=60):
            stop_event.set()

    app.econ_worker.run_cycle = run_cycle
    helper = threading.Thread(target=stopper, name="test-stopper")
    helper.start()
    try:
        assert not stop_event.is_set()
        assert _serve(tmp_path, app, stop_event) == 0
    finally:
        cycled.set()
        stop_event.set()
        helper.join(timeout=30)
    assert not helper.is_alive()
    assert cycle_threads and set(cycle_threads) == {THREAD}
    assert _alive() == []
    with pytest.raises(sqlite3.ProgrammingError):
        app.conn_econ_write.execute("SELECT 1")


def test_status_shows_econ_signal_capacity_and_a_worker_not_started_yet(tmp_path):
    app = _build(tmp_path)
    try:
        econ = app.ops._status_provider()["econ_signal"]
        assert econ["slice_worker"] == "not_started"
        cap = econ["slice_capacity"]
        assert cap["rows"] == 0 and cap["row_level"] == "ok"
        assert cap["row_capacity"] > 0 and cap["byte_capacity"] > cap["row_capacity"]
        assert cap["free_bytes"] > 0 and cap["db_bytes"] > 0
        assert set(econ["abandoned"]["total"]) >= {"freshness_expired", "deploy_linked"}
    finally:
        app.close()


def test_status_reads_the_workers_bytes_measurement_and_never_measures_itself(tmp_path):
    app = _build(tmp_path)
    try:
        before = app.ops._status_provider()["econ_signal"]["slice_capacity"]
        assert before["slice_bytes"] is None
        assert before["slice_bytes_measured_at"] is None
        stamp = datetime(2026, 11, 6, 1, 2, 3, tzinfo=timezone.utc)
        app.econ_measurer._measurement = (4321, stamp)
        app.econ_status_cache.invalidate()
        from agentic_fx.loops import econ_signal_report
        econ_signal_report.empty_slice_bytes()
        with patch("agentic_fx.loops.econ_signal_report.slice_related_bytes") as probe:
            after = app.ops.status(ShellCaller.SHELL)["econ_signal"]["slice_capacity"]
        assert probe.call_count == 0
        assert after["slice_bytes"] == 4321
        assert after["slice_bytes_measured_at"] == stamp.isoformat()
    finally:
        app.close()


def test_status_follows_the_worker_thread_from_alive_to_stopped(tmp_path):
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = threading.Event()
    seen = []

    def watch(stop):
        seen.append(app.ops._status_provider()["econ_signal"]["slice_worker"])
        stop_event.set()

    app.econ_worker = _FakeWorker(watch)
    assert _serve(tmp_path, app, stop_event) == 0
    assert seen == ["alive"]
    assert app.econ_health.state() == "stopped"


def test_status_shows_a_worker_that_could_not_start(tmp_path):
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = threading.Event()
    stop_event.set()
    original_start = threading.Thread.start

    def start(self):
        if self.name == THREAD:
            raise RuntimeError("can't start new thread")
        return original_start(self)

    with patch.object(threading.Thread, "start", start), \
         patch("agentic_fx.service._log"):
        assert _serve(tmp_path, app, stop_event) == 0
    assert app.econ_health.state() == "start_failed"


def test_build_app_registers_the_parent_only_daily_report(tmp_path):
    app = _build(tmp_path)
    try:
        callback = app.scheduler._daily_report
        assert callback is not None
        close_at = datetime(2026, 7, 21, 21, 0, tzinfo=timezone.utc)
        callback(close_at)
        callback(close_at)
        reports = tmp_path / "data" / "improve_reports" / "econ_signal"
        assert [p.name for p in reports.iterdir()] == ["econ-signal-2026-07-21.md"]
        assert "## slice capacity" in (reports / "econ-signal-2026-07-21.md").read_text()
    finally:
        app.close()


MEASURE_THREAD = "afx-econ-capacity"


class _FakeMeasurer:
    measurement = None

    def __init__(self, on_run=None):
        self.runs = []
        self.on_run = on_run

    def run(self, stop):
        self.runs.append((threading.current_thread().name, stop))
        if self.on_run is not None:
            self.on_run(stop)


def test_build_app_builds_a_measurer_that_feeds_the_capacity_status(tmp_path):
    app = _build(tmp_path)
    try:
        assert isinstance(app.econ_measurer, CapacityMeasurer)
        assert not hasattr(app.econ_worker, "measurement")
    finally:
        app.close()


def test_the_measurer_runs_on_its_own_thread_and_stops_with_the_service(tmp_path):
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = threading.Event()
    seen = threading.Event()

    def on_run(stop):
        seen.set()
        stop_event.set()
        stop.wait(5)

    app.econ_measurer = _FakeMeasurer(on_run)
    app.econ_worker = _FakeWorker()
    assert _serve(tmp_path, app, stop_event) == 0
    assert seen.is_set()
    assert app.econ_measurer.runs[0][0] == MEASURE_THREAD
    assert MEASURE_THREAD not in {t.name for t in threading.enumerate()}


def test_a_measurer_that_does_not_stop_does_not_hold_the_workers_resources_or_the_exit_code(
        tmp_path):
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = threading.Event()
    release = threading.Event()
    written = []
    original_write = app.activity.write

    def spy_write(category, kind, *args, **kwargs):
        written.append((kind, args))
        return original_write(category, kind, *args, **kwargs)

    def stuck(stop):
        stop_event.set()
        release.wait(timeout=30)

    app.activity.write = spy_write
    app.econ_measurer = _FakeMeasurer(stuck)
    app.econ_worker = _FakeWorker()
    try:
        with patch("agentic_fx.service._ECON_WORKER_JOIN_TIMEOUT_SEC", 0.2), \
             patch("agentic_fx.service._log") as log:
            assert _serve(tmp_path, app, stop_event) == 0
        assert [a for k, a in written if k == "service_stopped"][0][0] == "graceful"
        assert not [k for k, _ in written if k == "close_skipped_resources"]
        assert any("measurer did not stop" in str(c.args[0])
                   for c in log.warning.call_args_list)
    finally:
        release.set()
        for t in threading.enumerate():
            if t.name == MEASURE_THREAD:
                t.join(timeout=30)
        for conn in (app.conn_econ_read, app.conn_econ_write):
            conn.close()
        app.instance_lock.close()


def test_a_worker_that_started_before_start_raised_is_not_called_start_failed(tmp_path):
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = threading.Event()
    seen = []

    def watch(stop):
        seen.append(app.econ_health.state())
        stop_event.set()

    app.econ_worker = _FakeWorker(watch)
    original_start = threading.Thread.start

    def start_then_raise(self):
        original_start(self)
        if self.name == THREAD:
            self.join(5)
            raise RuntimeError("late failure")

    with patch.object(threading.Thread, "start", start_then_raise), \
         patch("agentic_fx.service._log"):
        assert _serve(tmp_path, app, stop_event) == 0
    assert seen == ["alive"]
    assert app.econ_health.state() == "stopped"


def test_the_health_is_attached_before_the_thread_starts(tmp_path):
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = threading.Event()
    at_start = []
    original_start = threading.Thread.start

    def spy(self):
        if self.name == THREAD:
            at_start.append(app.econ_health._thread is self)
        return original_start(self)

    app.econ_worker = _FakeWorker(lambda stop: stop_event.set())
    with patch.object(threading.Thread, "start", spy):
        assert _serve(tmp_path, app, stop_event) == 0
    assert at_start == [True]


def test_status_computes_the_econ_section_outside_the_status_lock(tmp_path):
    app = _build(tmp_path)
    in_econ, release, second_in_lock = threading.Event(), threading.Event(), threading.Event()
    equity_calls = []
    original = app.econ_status_cache._compute

    def blocking():
        in_econ.set()
        release.wait(10)
        return original()

    from agentic_fx.core.paper_broker import PaperBroker
    real_equity = PaperBroker.equity

    def counting_equity(self):
        equity_calls.append(1)
        if len(equity_calls) == 2:
            second_in_lock.set()
        return real_equity(self)

    app.econ_status_cache._compute = blocking
    first = threading.Thread(target=app.ops._status_provider)
    second = threading.Thread(target=app.ops._status_provider)
    try:
        with patch.object(PaperBroker, "equity", counting_equity):
            first.start()
            assert in_econ.wait(10)
            second.start()
            assert second_in_lock.wait(5), "econ 集計が status_lock を持ったまま走っている"
    finally:
        release.set()
        first.join(10)
        second.join(10)
        app.close()


def test_the_shell_and_the_status_read_the_same_econ_cache(tmp_path):
    app = _build(tmp_path)
    try:
        assert app.commands.econ_status_source == app.econ_status_cache.get
        runs = []
        original = app.econ_status_cache._compute
        app.econ_status_cache._compute = lambda: runs.append(1) or original()
        app.ops._status_provider()
        app.ops._status_provider()
        app.commands.econ_status_source()
        assert len(runs) == 1
    finally:
        app.close()


def test_status_shows_the_measurers_last_outcome_and_age(tmp_path):
    app = _build(tmp_path)
    try:
        app.econ_measurer._last_outcome = "aborted"
        app.econ_measurer._last_elapsed = 30.4
        interval = app.econ_measurer.info()["interval_sec"]
        app.econ_measurer._measurement = (4321, NOW - timedelta(seconds=interval * 3))
        app.econ_status_cache.invalidate()
        cap = app.ops.status(ShellCaller.SHELL)["econ_signal"]["slice_capacity"]
        assert cap["measure_outcome"] == "aborted"
        assert cap["measure_elapsed_sec"] == 30.4
        assert cap["slice_bytes_stale"] is True
    finally:
        app.close()
