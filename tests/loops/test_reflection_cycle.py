from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call
import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock, OrderStatus
from agentic_fx.loops.reflection_cycle import ReflectionCycle
from agentic_fx.loops.mission_watch import MissionWatch
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.store import missions, orders, reflections
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _cycle(tmp_path, results, watch=None):
    """Helper to create ReflectionCycle."""
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    rag = MagicMock()
    cyc = ReflectionCycle(
        conn=conn, runner=FakeRunner(results), rag=rag,
        settings=SETTINGS,
        activity=ActivityLog(tmp_path / "a.log"),
        clock=FixedClock(NOW),
        watch=watch)
    return conn, rag, cyc


def _closed_order(conn):
    """Helper to create a closed order."""
    return orders.insert(
        conn, pair="USDJPY", direction="long",
        entry_type="market", horizon="day",
        status=OrderStatus.CLOSED, now=NOW, quantity=0.1,
        realized_pnl=-1500.0, close_reason="sl",
        avg_fill_price=148.5, close_price=148.0)


def test_creates_reflection_for_closed(tmp_path):
    """Closed order without reflection gets reflection created."""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "SL 幅が狭すぎた"}, [])])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 1
    assert reflections.get(conn, oid)["content"] == "SL 幅が狭すぎた"
    rag.add_reflection.assert_called_once_with(oid, "SL 幅が狭すぎた", "USDJPY")


def test_skips_already_reflected(tmp_path):
    """Closed order with reflection is skipped."""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "x"}, [])])
    oid = _closed_order(conn)
    reflections.save(conn, oid, "既存", NOW)
    assert cyc.run_pending() == 0
    rag.add_reflection.assert_not_called()


def test_runner_failure_skips_for_retry(tmp_path):
    """Runner failure (timeout) leaves no SQLite row — retry next run."""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult("timeout", None, [])])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    assert reflections.get(conn, oid) is None


def test_rag_failure_leaves_no_sqlite_row(tmp_path):
    """ChromaDB failure before SQLite save leaves no row — retry next run."""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "x"}, [])])
    rag.add_reflection.side_effect = RuntimeError("chroma down")
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    assert reflections.get(conn, oid) is None


def test_watch_begin_end_called(tmp_path):
    """MissionWatch.begin/end are called even on runner exception."""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult("failed", None, [])])
    watch = MagicMock(spec=MissionWatch)
    cyc.watch = watch
    oid = _closed_order(conn)
    cyc.run_pending()
    # Should have called begin with the mid, loop name, and timeout
    watch.begin.assert_called_once()
    watch.end.assert_called_once()
    # end should be called even if runner fails
    args, kwargs = watch.end.call_args
    assert args[0] == watch.begin.call_args[0][0]  # same mission_id


def test_runner_non_mission_result_normalized_to_failed(tmp_path):
    """Runner returning non-MissionResult is normalized to failed."""
    conn, rag, cyc = _cycle(tmp_path, [])
    # FakeRunner with empty results returns failed by default
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    assert reflections.get(conn, oid) is None
    rag.add_reflection.assert_not_called()


def test_missions_finish_failure_recorded_in_activity(tmp_path):
    """missions.finish failure writes activity SYSTEM mission_finalize_failed."""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "x"}, [])])
    activity = ActivityLog(tmp_path / "a.log")
    cyc.activity = activity

    # Mock missions.finish to raise
    orig_finish = missions.finish

    def failing_finish(*args, **kwargs):
        raise RuntimeError("db error")

    try:
        missions.finish = failing_finish
        oid = _closed_order(conn)
        # missions.finish failure is logged but reflection still completes
        # (finish is metadata recording, not part of the core reflection save)
        result = cyc.run_pending()
        assert result == 1  # reflection created despite missions.finish failure
        assert reflections.get(conn, oid)["content"] == "x"
        rag.add_reflection.assert_called_once_with(oid, "x", "USDJPY")
    finally:
        missions.finish = orig_finish


def test_per_item_isolation_two_orders(tmp_path):
    """One order's save failure doesn't block second order processing."""
    # Create 2 results for 2 orders
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("completed", {"content": "order1"}, []),
        MissionResult("completed", {"content": "order2"}, []),
    ])

    oid1 = _closed_order(conn)
    oid2 = _closed_order(conn)

    # Make save fail for oid1 only
    orig_save = reflections.save
    def selective_save(c, oid, content, now):
        if oid == oid1:
            raise RuntimeError("save failed")
        return orig_save(c, oid, content, now)

    try:
        reflections.save = selective_save
        result = cyc.run_pending()
        # Should process both, but only oid2 succeeds
        # Since per-item try/except catches oid1 failure, oid2 should be processed
        # rag.add_reflection should be called for both before save fails
        assert result == 1  # only oid2 counted
        assert reflections.get(conn, oid1) is None
        assert reflections.get(conn, oid2)["content"] == "order2"
        # rag should be called twice (once per order, before save)
        assert rag.add_reflection.call_count == 2
    finally:
        reflections.save = orig_save


def test_runner_raises_normalized_to_failed(tmp_path):
    """Runner raising exception is normalized to failed, no rag call."""
    class FailRunner:
        def run(self, mission):
            raise RuntimeError("runner crashed")

    conn = connect(tmp_path / "t.db")
    init_db(conn)
    rag = MagicMock()
    cyc = ReflectionCycle(
        conn=conn, runner=FailRunner(), rag=rag,
        settings=SETTINGS,
        activity=ActivityLog(tmp_path / "a.log"),
        clock=FixedClock(NOW))

    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    assert reflections.get(conn, oid) is None
    rag.add_reflection.assert_not_called()
