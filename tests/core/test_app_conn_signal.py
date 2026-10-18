"""signal maintenance 専用 SQLite 接続の配線・回復テスト。"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import (
    App, _busy_resources_after_join, _scheduler_tick_once, build_app)
from agentic_fx.store import plugin_deploy_ops, signals
from agentic_fx.store.db import connect, init_db
from tests.store.test_rag import FakeEmbedding
from tests.test_service_app import _init


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _app(tmp_path):
    _init(tmp_path)
    return build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())


def test_maintenance_callback_uses_dedicated_connection(tmp_path, monkeypatch):
    """`conn_core` を callback に渡す変異はこの identity 契約で検出する。"""
    app = _app(tmp_path)
    received = []
    try:
        monkeypatch.setattr(
            "agentic_fx.service._run_signal_maintenance",
            lambda **kwargs: received.append(kwargs["conn"]),
        )
        app.scheduler.on_signal_maintenance(NOW)
        assert received == [app.conn_signal]
        assert app.conn_signal is not app.conn_core
    finally:
        app.close()


def test_rollback_failure_discards_connection_and_uses_replacement(
        tmp_path, monkeypatch):
    """rollback 不能な接続は再利用せず、差し替えた接続で保守する。"""
    app = _app(tmp_path)
    broken = MagicMock(in_transaction=True)
    broken.rollback.side_effect = RuntimeError("rollback failed")
    replacement = MagicMock(in_transaction=False)
    app.conn_signal = broken
    received = []
    try:
        monkeypatch.setattr("agentic_fx.service.connect", lambda _path: replacement)
        monkeypatch.setattr(
            "agentic_fx.service._run_signal_maintenance",
            lambda **kwargs: received.append(kwargs["conn"]),
        )
        app.scheduler.on_signal_maintenance(NOW)
        assert broken.close.called
        assert app.conn_signal is replacement
        assert received == [replacement]
    finally:
        app.close()


def test_three_connection_failures_latch_then_stop(tmp_path, monkeypatch):
    """1〜2 回目は skip、3 回連続目だけ fatal と stop を要求する。"""
    app = _app(tmp_path)
    app.conn_signal = None
    failures = []
    fatal = []
    try:
        monkeypatch.setattr("agentic_fx.service.connect",
                            MagicMock(side_effect=OSError("cannot connect")))
        monkeypatch.setattr(app.health_latch, "record_failure", failures.append)
        monkeypatch.setattr("agentic_fx.service._record_fatal",
                            lambda *_args: fatal.append("fatal"))
        for _ in range(3):
            app.scheduler.on_signal_maintenance(NOW)
        assert len(failures) == 3
        assert all(item.startswith("signal_conn_unavailable:") for item in failures)
        assert fatal == ["fatal"]
    finally:
        app.close()


def test_connection_failures_skip_without_progress_then_fatal(
        tmp_path, monkeypatch):
    """再生成失敗は skip (cursor 不進行・保守なし)、3 回目で fatal と stop。"""
    app = _app(tmp_path)
    app.conn_signal = None
    connect_mock = MagicMock(side_effect=OSError("cannot connect"))
    maintenance = MagicMock()
    failures = []
    fatal = []
    try:
        monkeypatch.setattr("agentic_fx.service.connect", connect_mock)
        monkeypatch.setattr("agentic_fx.service._run_signal_maintenance",
                            maintenance)
        monkeypatch.setattr(app.health_latch, "record_failure", failures.append)
        monkeypatch.setattr(
            "agentic_fx.service._record_fatal",
            lambda app_, stop, reason: (fatal.append(reason), stop.set()))
        for expected in (1, 2):
            app.scheduler.on_signal_maintenance(NOW)
            assert connect_mock.call_count == expected
            assert len(failures) == expected
            assert fatal == [] and not app.stop_event.is_set()
        app.scheduler.on_signal_maintenance(NOW)
        assert connect_mock.call_count == 3
        assert fatal == ["signal connection unavailable"]
        assert app.stop_event.is_set()
        assert all(f.startswith("signal_conn_unavailable:") for f in failures)
        maintenance.assert_not_called()
        assert app.conn_signal is None
    finally:
        app.close()


@pytest.mark.parametrize("state", ["degraded", "halted"])
def test_nonready_state_still_repairs_signal_connection_without_maintenance(
        tmp_path, monkeypatch, state):
    """The delayed wrapper remains active outside ready, but does not evaluate plugins."""
    app = _app(tmp_path)
    app.conn_signal = None
    maintenance = MagicMock()
    try:
        monkeypatch.setattr("agentic_fx.service.connect",
                            MagicMock(side_effect=OSError("database busy")))
        monkeypatch.setattr("agentic_fx.service._run_signal_maintenance",
                            maintenance)
        app.scheduler.state_fn = lambda: state

        app.scheduler.on_signal_maintenance(NOW)

        assert app.signal_connection_failures == 1
        maintenance.assert_not_called()
    finally:
        app.close()


@pytest.mark.parametrize("state,expected_calls", [
    ("ready", 1), ("degraded", 0), ("halted", 0)])
def test_maintenance_runs_only_when_ready_after_a_successful_repair(
        tmp_path, monkeypatch, state, expected_calls):
    """Repair succeeds in every state; only ready proceeds to evaluate plugins."""
    app = _app(tmp_path)
    app.conn_signal = None
    replacement = MagicMock(in_transaction=False)
    received = []
    try:
        monkeypatch.setattr("agentic_fx.service.connect", lambda _p: replacement)
        monkeypatch.setattr("agentic_fx.service._run_signal_maintenance",
                            lambda **kw: received.append(kw["conn"]))
        app.scheduler.state_fn = lambda: state

        app.scheduler.on_signal_maintenance(NOW)

        assert app.conn_signal is replacement
        assert app.signal_connection_failures == 0
        assert received == [replacement] * expected_calls
    finally:
        app.conn_signal = None
        app.close()


def test_successful_reconnect_resets_failure_counter(tmp_path, monkeypatch):
    app = _app(tmp_path)
    app.conn_signal = None
    replacement = MagicMock(in_transaction=False)
    fatal = []
    try:
        monkeypatch.setattr("agentic_fx.service._run_signal_maintenance",
                            lambda **_kw: None)
        monkeypatch.setattr("agentic_fx.service._record_fatal",
                            lambda *_a: fatal.append(1))
        monkeypatch.setattr("agentic_fx.service.connect",
                            MagicMock(side_effect=OSError("down")))
        app.scheduler.on_signal_maintenance(NOW)
        app.scheduler.on_signal_maintenance(NOW)
        assert app.signal_connection_failures == 2
        monkeypatch.setattr("agentic_fx.service.connect", lambda _p: replacement)
        app.scheduler.on_signal_maintenance(NOW)
        assert app.signal_connection_failures == 0
        assert app.conn_signal is replacement
        assert fatal == []
    finally:
        app.conn_signal = None
        app.close()


def test_exit_rollback_failure_replaces_connection_without_reuse(
        tmp_path, monkeypatch):
    """出口の rollback 失敗: 旧接続を close し、connect で差し替え、再利用しない。"""
    app = _app(tmp_path)
    old = MagicMock(in_transaction=False)
    replacement = MagicMock(in_transaction=False)
    app.conn_signal = old
    connect_mock = MagicMock(return_value=replacement)

    def leave_transaction_open(**_kwargs):
        old.in_transaction = True
        old.rollback.side_effect = RuntimeError("rollback failed")

    try:
        monkeypatch.setattr("agentic_fx.service.connect", connect_mock)
        monkeypatch.setattr("agentic_fx.service._run_signal_maintenance",
                            leave_transaction_open)
        app.scheduler.on_signal_maintenance(NOW)
        assert old.close.called
        assert connect_mock.call_count == 1
        assert app.conn_signal is replacement

        # 次 tick は新接続だけを使い、旧接続には触れない。
        old.reset_mock()
        received = []
        monkeypatch.setattr("agentic_fx.service._run_signal_maintenance",
                            lambda **kw: received.append(kw["conn"]))
        app.scheduler.on_signal_maintenance(NOW)
        assert received == [replacement]
        assert connect_mock.call_count == 1
        assert not old.method_calls
    finally:
        app.conn_signal = None
        app.close()


def test_exit_connect_failure_leaves_none_and_next_tick_retries(
        tmp_path, monkeypatch):
    app = _app(tmp_path)
    old = MagicMock(in_transaction=False)
    app.conn_signal = old
    replacement = MagicMock(in_transaction=False)
    connect_mock = MagicMock(side_effect=[OSError("down"), replacement])

    def leave_transaction_open(**_kwargs):
        old.in_transaction = True
        old.rollback.side_effect = RuntimeError("rollback failed")

    try:
        monkeypatch.setattr("agentic_fx.service.connect", connect_mock)
        monkeypatch.setattr("agentic_fx.service._run_signal_maintenance",
                            leave_transaction_open)
        app.scheduler.on_signal_maintenance(NOW)
        assert app.conn_signal is None
        assert app.signal_connection_failures == 1
        monkeypatch.setattr("agentic_fx.service._run_signal_maintenance",
                            lambda **_kw: None)
        app.scheduler.on_signal_maintenance(NOW)
        assert app.conn_signal is replacement
        assert app.signal_connection_failures == 0
    finally:
        app.conn_signal = None
        app.close()


def test_build_app_failure_closes_signal_connection(tmp_path, monkeypatch):
    """conn_signal 作成後の build_app 失敗で、その接続を close する。"""
    _init(tmp_path)
    created = []
    real_connect = connect

    def tracking_connect(path):
        conn = real_connect(path)
        created.append(conn)
        return conn

    monkeypatch.setattr("agentic_fx.service.connect", tracking_connect)
    monkeypatch.setattr("agentic_fx.service.seed_default_sources",
                        MagicMock(side_effect=RuntimeError("boom")))
    import pytest
    with pytest.raises(RuntimeError, match="boom"):
        build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                  embedding_fn=FakeEmbedding())
    assert len(created) >= 2
    signal_conn = created[1]
    import sqlite3
    with pytest.raises(sqlite3.ProgrammingError):
        signal_conn.execute("SELECT 1")


def test_close_orders_signal_before_core_and_busy_skips_both(tmp_path):
    """残存 scheduler は core/signal の両接続を close してはならない。"""
    order = []

    class Resource:
        def __init__(self, name):
            self.name = name

        def close(self):
            order.append(self.name)

    core = Resource("core")
    signal = Resource("signal")
    app = App(
        conn_core=core, conn_signal=signal, conn_shell=Resource("shell"),
        settings=None, state=None, activity=None, broker=None, executor=None,
        provider=None, econ=None, collector=None, rag=Resource("rag"),
        trade_loop=None, reflection=None, scheduler=None, commands=None,
        registry=None, inventory_result=None, approved_plugins=None,
        core_lock=None, mission_watch=None, notifier=None, runner=Resource("runner"),
        owns_runner=False, clock=None, instance_lock=Resource("instance"),
        supervisor=None, improve_supervisor=None,
        conn_supervisor=Resource("supervisor"),
    )
    assert {"conn_core", "conn_signal"} <= _busy_resources_after_join(True, False)
    app.close()
    assert order.index("signal") < order.index("core")

    order.clear()
    skipped = app.close(busy_resources=frozenset({"conn_core", "conn_signal"}))
    assert "conn_signal" in skipped and "conn_core" in skipped
    assert "signal" not in order and "core" not in order


def test_pending_exists_excludes_stale_and_deploy_linked_rows(tmp_path):
    """鮮度条件を外す変異は、消費不能な行で signal mission を起こす。"""
    conn = connect(tmp_path / "signals.db")
    init_db(conn)
    stale = signals.add(
        conn, plugin="stale", content_hash="s" * 64, pair="USDJPY",
        timeframe="1h", bar_ts=(NOW.replace(hour=8)).isoformat(), kind="signal",
        payload={}, now=NOW)
    linked = signals.add(
        conn, plugin="linked", content_hash="l" * 64, pair="USDJPY",
        timeframe="1h", bar_ts=NOW.isoformat(), kind="signal", payload={}, now=NOW)
    op_id = plugin_deploy_ops.insert_intent(
        conn, op_kind="disable_symlink", name="linked", from_kind="symlink",
        to_kind="absent", from_target="old", to_target=None, retired_path=None,
        subject_content_hash="l" * 64, subject_artifact_hash="a" * 64,
        target_content_hash=None, target_artifact_hash=None, approval_id=None,
        revoke=False, revoke_row_id=None, force=False, actor="test", now=NOW)
    plugin_deploy_ops.link_signals(conn, op_id, [(linked, "pending")])

    assert stale is not None and linked is not None
    assert not signals.pending_exists(conn, now=NOW, freshness_bars=2)


def test_tick_runs_signal_maintenance_once_on_a_normal_tick(tmp_path):
    app = _app(tmp_path)
    calls = []
    try:
        app.scheduler.on_signal_maintenance = calls.append
        _scheduler_tick_once(app)
        assert calls == [NOW]
    finally:
        app.close()


def test_tick_exception_still_runs_signal_maintenance_once(tmp_path, monkeypatch):
    app = _app(tmp_path)
    calls = []

    def boom(_now):
        raise RuntimeError("tick broke")

    try:
        app.scheduler.on_signal_maintenance = calls.append
        monkeypatch.setattr(app.scheduler, "_trade_mission_due", boom)
        with pytest.raises(RuntimeError, match="tick broke"):
            _scheduler_tick_once(app)
        assert calls == [NOW]
    finally:
        app.close()


def test_tick_exception_does_not_run_signal_maintenance_after_stop(
        tmp_path, monkeypatch):
    import threading
    app = _app(tmp_path)
    calls = []
    stop = threading.Event()
    stop.set()

    def boom(_now):
        raise RuntimeError("tick broke")

    try:
        app.scheduler.on_signal_maintenance = calls.append
        app.scheduler._stop_event = stop
        monkeypatch.setattr(app.scheduler, "tick", boom)
        with pytest.raises(RuntimeError):
            _scheduler_tick_once(app)
        assert calls == []
    finally:
        app.close()
