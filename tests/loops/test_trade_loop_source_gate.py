"""専用 signal 採否の source lock と claim 後の終端条件。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.executor import snapshot_age_exceeded
from agentic_fx.loops import trade_loop
from agentic_fx.plugin import switch
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import plugin_deploy_ops, signal_decisions, signals

from tests.loops.test_trade_loop import NOW, SETTINGS, _loop
from tests.loops.test_trade_loop_signal import _add_signal


ACCEPT = {"decision": "accept", "reject_code": None}


def _prepare(tmp_path, *, requeue_count=0):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult("completed", ACCEPT, [])])
    signal_id = _add_signal(conn)
    if requeue_count:
        conn.execute("UPDATE signals SET requeue_count=? WHERE id=?",
                     (requeue_count, signal_id))
        conn.commit()
    loop._plugins_root = tmp_path / "plugins"
    loop.econ.window_view.return_value.in_window = False
    loop.executor.gather_open_snapshot = lambda *args, **kwargs: SimpleNamespace(
        captured_at=NOW)
    loop.executor.open_from_snapshot = lambda *args, **kwargs: {
        "result": "submitted", "order_id": None, "reasons": []}
    return conn, loop, signal_id


def _row(conn, signal_id):
    return dict(conn.execute(
        "SELECT status,requeue_count,abandon_reason,abandoned_at FROM signals "
        "WHERE id=?", (signal_id,)).fetchone())


def test_source_becoming_inactive_abandons_without_a_decision(tmp_path, monkeypatch):
    conn, loop, signal_id = _prepare(tmp_path)
    monkeypatch.setattr(trade_loop, "signal_source_active", lambda *_args: False)

    loop.run_once("signal")

    row = _row(conn, signal_id)
    assert (row["status"], row["abandon_reason"], row["abandoned_at"]) == (
        "abandoned", "source_inactive", NOW.isoformat())
    assert conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM signal_shadow").fetchone()[0] == 0


def test_source_lock_timeout_requeues_without_a_decision(tmp_path, monkeypatch):
    conn, loop, signal_id = _prepare(tmp_path)

    @contextmanager
    def busy(*_args, **_kwargs):
        raise switch.PluginBusyError("held")
        yield

    monkeypatch.setattr(trade_loop, "acquire_name_locks", busy)
    loop.run_once("signal")

    assert _row(conn, signal_id)["status"] == "pending"
    assert _row(conn, signal_id)["requeue_count"] == 1
    assert conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM signal_shadow").fetchone()[0] == 0


def test_retry_limit_abandons_claim_with_reason_and_time(tmp_path, monkeypatch):
    conn, loop, signal_id = _prepare(tmp_path, requeue_count=2)
    loop.settings = SETTINGS.model_copy(deep=True)
    loop.settings.plugin.signal_requeue_max = 2

    @contextmanager
    def busy(*_args, **_kwargs):
        raise switch.PluginBusyError("held")
        yield

    monkeypatch.setattr(trade_loop, "acquire_name_locks", busy)
    loop.run_once("signal")

    row = _row(conn, signal_id)
    assert (row["status"], row["abandon_reason"], row["abandoned_at"]) == (
        "abandoned", "retry_exhausted", NOW.isoformat())
    assert conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM signal_shadow").fetchone()[0] == 0


@pytest.mark.parametrize(("requeue_count", "reason"), [
    (0, "freshness_expired"), (1, "requeued_expired")])
def test_commit_after_fresh_until_abandons_with_history_reason(
        tmp_path, requeue_count, reason):
    conn, loop, signal_id = _prepare(tmp_path, requeue_count=requeue_count)
    late = NOW + timedelta(hours=2, seconds=1)

    class AdvancingRunner:
        def run(self, _mission):
            loop.clock = FixedClock(late)
            loop.executor.clock = loop.clock
            return MissionResult("completed", ACCEPT, [])

    loop.runner = AdvancingRunner()
    loop.run_once("signal")

    row = _row(conn, signal_id)
    assert (row["status"], row["abandon_reason"], row["abandoned_at"]) == (
        "abandoned", reason, late.isoformat())
    assert conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM signal_shadow").fetchone()[0] == 0


def test_runner_returning_after_fresh_until_abandons_from_one_minute_margin(
        tmp_path):
    conn, loop, signal_id = _prepare(tmp_path)
    fresh_until = NOW + timedelta(hours=1)
    loop.clock = FixedClock(fresh_until - timedelta(seconds=60))
    loop.executor.clock = loop.clock

    class AdvancingRunner:
        def run(self, _mission):
            late = fresh_until + timedelta(seconds=67)
            loop.clock = FixedClock(late)
            loop.executor.clock = loop.clock
            return MissionResult("completed", ACCEPT, [])

    loop.runner = AdvancingRunner()
    loop.run_once("signal")

    row = _row(conn, signal_id)
    assert (row["status"], row["abandon_reason"], row["abandoned_at"]) == (
        "abandoned", "freshness_expired",
        (fresh_until + timedelta(seconds=67)).isoformat())
    assert conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM signal_shadow").fetchone()[0] == 0


def test_stale_snapshot_requeues_while_signal_is_fresh(tmp_path):
    conn, loop, signal_id = _prepare(tmp_path)

    class AdvancingRunner:
        def run(self, _mission):
            loop.clock = FixedClock(NOW + timedelta(seconds=31))
            loop.executor.clock = loop.clock
            return MissionResult("completed", ACCEPT, [])

    loop.runner = AdvancingRunner()
    loop.executor.gather_open_snapshot = lambda *args, **kwargs: SimpleNamespace(
        captured_at=NOW)
    loop.run_once("signal")

    row = _row(conn, signal_id)
    assert (row["status"], row["requeue_count"]) == ("pending", 1)
    assert row["abandon_reason"] is None and row["abandoned_at"] is None
    assert conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM signal_shadow").fetchone()[0] == 0


def test_freshness_boundary_is_inclusive(tmp_path, monkeypatch):
    conn, loop, signal_id = _prepare(tmp_path)
    _, fresh_until = signals.freshness_window(
        "1h", (NOW - timedelta(hours=1)).isoformat(),
        SETTINGS.plugin.signal_freshness_bars)
    boundary = datetime.fromisoformat(fresh_until)
    loop.clock = FixedClock(boundary)
    loop.executor.clock = loop.clock
    loop.executor.gather_open_snapshot = lambda *args, **kwargs: SimpleNamespace(
        captured_at=boundary)
    monkeypatch.setattr(trade_loop, "signal_source_active", lambda *_args: True)

    loop.run_once("signal")

    assert _row(conn, signal_id)["status"] == "consumed"
    assert conn.execute("SELECT decision FROM signal_decisions WHERE signal_id=?",
                        (signal_id,)).fetchone()[0] == "accept"


def test_snapshot_age_uses_a_strict_shared_boundary(tmp_path):
    _, loop, _ = _prepare(tmp_path)
    snapshot = SimpleNamespace(captured_at=NOW)
    assert snapshot_age_exceeded(snapshot, NOW + timedelta(seconds=10), 10) is None
    assert "execution snapshot is stale" in snapshot_age_exceeded(
        snapshot, NOW + timedelta(seconds=10, microseconds=1), 10)


def _plain_indicator_world(tmp_path):
    root = tmp_path / "plugins"
    (root / "legacy_ind").mkdir(parents=True)
    resolved = SimpleNamespace(items=(SimpleNamespace(
        plugin_name="legacy_ind", content_hash="f" * 64),))
    return root, resolved


def _approve_strategy(conn):
    import json
    conn.execute(
        "INSERT INTO approval_requests (kind,status,payload_json,decided_at,"
        "created_at) VALUES ('plugin','approved',?,?,?)",
        (json.dumps({"name": "strategy", "content_hash": "a" * 64}),
         NOW.isoformat(), NOW.isoformat()))
    conn.commit()


def test_source_identity_accepts_a_matching_plain_indicator_dependency(tmp_path):
    from agentic_fx.plugin.source_state import source_state
    conn, _, _ = _prepare(tmp_path)
    root, resolved = _plain_indicator_world(tmp_path)
    strategy = SimpleNamespace(kind="plain", content_hash="a" * 64,
                               artifact_hash=None)
    live = SimpleNamespace(kind="plain", content_hash="0" * 64,
                           artifact_hash=None)
    _approve_strategy(conn)

    def identity(_root, name):
        return strategy if name == "strategy" else live

    with patch.object(plugin_deploy_ops, "live_identity", identity):
        assert trade_loop.signal_source_active(
            conn, root, "strategy", "a" * 64, resolved) is True
        assert source_state(conn, root, name="strategy", content_hash="a" * 64,
                            artifact_hash=None, indicators=resolved.items) is None


def test_source_identity_rejects_a_symlink_with_a_different_hash(tmp_path):
    conn, _, _ = _prepare(tmp_path)
    root, resolved = _plain_indicator_world(tmp_path)
    strategy = SimpleNamespace(kind="plain", content_hash="a" * 64,
                               artifact_hash=None)
    linked = SimpleNamespace(kind="symlink", content_hash="0" * 64,
                             artifact_hash="artifact")
    _approve_strategy(conn)

    def identity(_root, name):
        return strategy if name == "strategy" else linked

    with patch.object(plugin_deploy_ops, "live_identity", identity):
        assert trade_loop.signal_source_active(
            conn, root, "strategy", "a" * 64, resolved) is False


def test_source_lock_covers_accept_commit(tmp_path, monkeypatch):
    conn, loop, signal_id = _prepare(tmp_path)
    locked = False
    observed = []

    @contextmanager
    def held(*_args, **_kwargs):
        nonlocal locked
        locked = True
        try:
            yield
        finally:
            locked = False

    real_commit = signal_decisions.commit_accept

    def commit(*args, **kwargs):
        observed.append(locked)
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(trade_loop, "acquire_name_locks", held)
    monkeypatch.setattr(trade_loop, "signal_source_active", lambda *_args: True)
    monkeypatch.setattr(signal_decisions, "commit_accept", commit)
    loop.run_once("signal")

    assert observed == [True]
    assert _row(conn, signal_id)["status"] == "consumed"


@pytest.mark.parametrize("error", [OSError, ValueError])
def test_lock_error_requeues_and_fails_closed(tmp_path, monkeypatch, error):
    conn, loop, signal_id = _prepare(tmp_path)

    @contextmanager
    def broken(*_args, **_kwargs):
        raise error("lock file unavailable")
        yield

    monkeypatch.setattr(trade_loop, "acquire_name_locks", broken)
    loop.run_once("signal")

    assert _row(conn, signal_id)["status"] == "pending"
    assert _row(conn, signal_id)["requeue_count"] == 1
    assert conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 0
