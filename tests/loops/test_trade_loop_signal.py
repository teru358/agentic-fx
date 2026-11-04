"""専用 signal mission の claim・採否 transaction・実行境界。"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import signals
from agentic_fx.core import transitions
from agentic_fx.ops.trade_event_sink import TradeEventEmitter, TradeEventSink

from tests.loops.test_trade_loop import NOW, SETTINGS, _loop


def _add_signal(conn, *, plugin="sig1", pair="USDJPY", timeframe="1h",
                bar_ts=None, direction="long"):
    bar_ts = bar_ts or (NOW - timedelta(hours=1))
    return signals.add(
        conn, plugin=plugin, content_hash=f"hash-{plugin}-{bar_ts.isoformat()}",
        pair=pair, timeframe=timeframe, bar_ts=bar_ts.isoformat(), kind="strategy",
        payload={"action": "open", "pair": pair, "direction": direction,
                 "entry_type": "market", "horizon": "day", "stop_loss": 147.0,
                 "take_profit": 151.5, "confidence": 0.5,
                 "reasoning": "strategy payload"},
        now=bar_ts, contract_version="executable_v1")


def _status(conn, signal_id):
    return dict(conn.execute(
        "SELECT status,requeue_count,claimed_by_mission_id FROM signals WHERE id=?",
        (signal_id,)).fetchone())


def test_signal_reject_commits_decision_shadow_and_consumes(tmp_path):
    conn, loop, runner, _ = _loop(tmp_path, [MissionResult(
        "completed", {"decision": "reject", "reject_code": "news_shock"}, [])])
    signal_id = _add_signal(conn)

    loop.run_once("signal")

    mission = runner.missions[0]
    assert set(mission.tools) == {"search_news", "get_positions"}
    assert mission.output_schema["properties"]["decision"]["enum"] == ["accept", "reject"]
    assert "get_signals" not in mission.tools
    decision = conn.execute(
        "SELECT decision,actor,reject_code,trade_intent_id FROM signal_decisions "
        "WHERE signal_id=?", (signal_id,)).fetchone()
    assert tuple(decision) == ("reject", "llm", "news_shock", None)
    assert _status(conn, signal_id)["status"] == "consumed"
    assert conn.execute(
        "SELECT state FROM signal_shadow WHERE signal_id=?", (signal_id,)
    ).fetchone()[0] == "pending_entry"
    assert conn.execute("SELECT COUNT(*) FROM trade_intents").fetchone()[0] == 0


def test_signal_accept_commits_fixed_payload_before_executor(tmp_path):
    conn, loop, runner, _ = _loop(tmp_path, [MissionResult(
        "completed", {"decision": "accept", "reject_code": None}, [])])
    signal_id = _add_signal(conn)
    loop.econ.window_view.return_value.in_window = False
    loop.executor.gather_open_snapshot = lambda intent, **kwargs: SimpleNamespace(
        captured_at=NOW)

    def execute(intent, iid, snapshot, **kwargs):
        decision = conn.execute(
            "SELECT trade_intent_id FROM signal_decisions WHERE signal_id=?",
            (signal_id,)).fetchone()
        assert decision is not None and decision[0] == iid
        return {"result": "submitted", "order_id": None, "reasons": []}

    loop.executor.open_from_snapshot = execute
    out = loop.run_once("signal")

    assert out["result"] == "submitted"
    decision = conn.execute(
        "SELECT trade_intent_id FROM signal_decisions WHERE signal_id=?",
        (signal_id,)).fetchone()
    payload = conn.execute(
        "SELECT payload_json FROM trade_intents WHERE id=?", (decision[0],)
    ).fetchone()[0]
    assert '"pair": "USDJPY"' in payload
    assert '"direction": "long"' in payload
    assert "signal" not in payload
    assert _status(conn, signal_id)["status"] == "consumed"


def test_signal_executor_runs_while_other_threads_are_excluded_by_core_lock(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"decision": "accept", "reject_code": None}, [])])
    _add_signal(conn)
    loop.econ.window_view.return_value.in_window = False
    loop.executor.gather_open_snapshot = lambda intent, **kwargs: SimpleNamespace(
        captured_at=NOW)
    observed = []
    notifications = []

    class NotifierProbe:
        def send(self, _message):
            acquired = []
            def check():
                value = loop._core_lock.acquire(blocking=False)
                acquired.append(value)
                if value:
                    loop._core_lock.release()
            checker = threading.Thread(target=check)
            checker.start()
            checker.join(timeout=2)
            notifications.extend(acquired)

    loop.notifier = NotifierProbe()

    def execute(*_args, **_kwargs):
        def check():
            acquired = loop._core_lock.acquire(blocking=False)
            observed.append(acquired)
            if acquired:
                loop._core_lock.release()
        checker = threading.Thread(target=check)
        checker.start()
        checker.join(timeout=2)
        loop.executor._notify("executor notification")
        return {"result": "submitted", "order_id": None, "reasons": []}

    loop.executor.open_from_snapshot = execute
    loop.run_once("signal")

    assert observed == [False]
    assert notifications == [True]


def test_snapshot_aging_after_accept_commit_does_not_reopen_claim(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"decision": "accept", "reject_code": None}, [])])
    signal_id = _add_signal(conn)
    loop.econ.window_view.return_value.in_window = False
    loop.executor.gather_open_snapshot = lambda intent, **kwargs: SimpleNamespace(
        captured_at=NOW)
    real_open = loop.executor.open_from_snapshot

    def age_snapshot(intent, iid, snapshot, **kwargs):
        loop.executor.clock = FixedClock(NOW + timedelta(seconds=31))
        return real_open(intent, iid, snapshot, **kwargs)

    loop.executor.open_from_snapshot = age_snapshot
    out = loop.run_once("signal")

    assert out["result"] == "rejected"
    assert "execution snapshot is stale" in out["reasons"][0]
    assert _status(conn, signal_id)["status"] == "consumed"
    assert conn.execute(
        "SELECT decision,actor FROM signal_decisions WHERE signal_id=?",
        (signal_id,)).fetchone()[:] == ("accept", "llm")


def test_accept_rechecked_inside_window_becomes_system_reject(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"decision": "accept", "reject_code": None}, [])])
    signal_id = _add_signal(conn)
    loop.econ.window_view.return_value.in_window = True
    loop.executor.gather_open_snapshot = lambda intent, **kwargs: SimpleNamespace(
        captured_at=NOW)
    loop.executor.open_from_snapshot = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("system-filtered decisions must not reach the executor"))

    out = loop.run_once("signal")

    assert out["reasons"] == ["econ_window"]
    decision = conn.execute(
        "SELECT decision,actor,reject_code,trade_intent_id FROM signal_decisions "
        "WHERE signal_id=?", (signal_id,)).fetchone()
    assert tuple(decision) == ("reject", "system", "econ_window", None)
    assert conn.execute("SELECT COUNT(*) FROM trade_intents").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM missions").fetchone()[0] == "system_filtered"


def test_signal_accept_event_keeps_signal_origin_through_sink(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"decision": "accept", "reject_code": None}, [])])
    signal_id = _add_signal(conn)
    loop.econ.window_view.return_value.in_window = False
    sink = TradeEventSink(tmp_path / "t.db", wall_clock=lambda: NOW,
                          busy_retry_sec=0.1)
    sink.start()
    observer = TradeEventEmitter(conn, sink, state_mode=None, ccy="JPY")
    token = transitions.observe(conn, observer)
    try:
        out = loop.run_once("signal")
    finally:
        transitions.unobserve(token)
        sink.stop()

    assert out["result"] == "opened", out
    event = conn.execute(
        "SELECT fields_json FROM ops_events WHERE code='order_state_changed' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert event is not None
    assert '"origin_code":"signal:sig1"' in event[0]
    intent = conn.execute(
        "SELECT payload_json FROM trade_intents WHERE id=(SELECT trade_intent_id "
        "FROM signal_decisions WHERE signal_id=?)", (signal_id,)).fetchone()
    assert json.loads(intent[0])["origin"] == "scheduler"


def test_cron_does_not_claim_or_inject_pending_signal(tmp_path):
    conn, loop, runner, _ = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "ok"}, [])])
    signal_id = _add_signal(conn)

    loop.run_once("cron")

    assert _status(conn, signal_id)["status"] == "pending"
    assert "この判断で扱う signal" not in runner.missions[0].prompt
    assert "get_signals" not in runner.missions[0].tools


def test_signal_with_no_claimable_row_is_skipped_without_runner_call(tmp_path):
    conn, loop, runner, _ = _loop(tmp_path, [])

    loop.run_once("signal")

    assert runner.missions == []
    assert conn.execute("SELECT status FROM missions").fetchone()[0] == "skipped"


def test_signal_runner_failure_requeues_and_finishes_mission(tmp_path):
    conn, loop, runner, _ = _loop(tmp_path, [MissionResult("timeout", None, [])])
    signal_id = _add_signal(conn)

    loop.run_once("signal")

    assert _status(conn, signal_id)["status"] == "pending"
    assert _status(conn, signal_id)["requeue_count"] == 1
    assert conn.execute("SELECT status FROM missions").fetchone()[0] == "timeout"
    assert conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 0


def test_invalid_decision_code_fails_and_requeues_without_decision(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"decision": "reject", "reject_code": "sell_now"}, [])])
    signal_id = _add_signal(conn)

    loop.run_once("signal")

    assert _status(conn, signal_id)["status"] == "pending"
    assert conn.execute("SELECT status FROM missions").fetchone()[0] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 0


@pytest.mark.parametrize("status", ["open", "pending_fill", "protection_pending"])
def test_opposite_position_reject_commits_for_an_active_inverse_order(tmp_path, monkeypatch,
                                                                      status):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"decision": "reject", "reject_code": "opposite_position"}, [])])
    signal_id = _add_signal(conn)
    from agentic_fx.core.contracts import OrderStatus
    from agentic_fx.store import orders as orders_store
    orders_store.insert(
        conn, pair="USDJPY", direction="short", entry_type="market",
        horizon="day", status=OrderStatus(status), now=NOW, quantity=0.1,
        remaining_quantity=0.1, requested_price=148.5, avg_fill_price=None,
        filled_quantity=0.0, stop_loss=150.0, take_profit=147.0)

    loop.run_once("signal")

    decision = conn.execute(
        "SELECT decision,actor,reject_code FROM signal_decisions WHERE signal_id=?",
        (signal_id,)).fetchone()
    assert tuple(decision) == ("reject", "llm", "opposite_position")
    assert _status(conn, signal_id)["status"] == "consumed"
    assert conn.execute("SELECT state FROM signal_shadow WHERE signal_id=?",
                        (signal_id,)).fetchone()[0] == "pending_entry"


def test_claim_uses_submission_signal_id_upper_bound(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"decision": "reject", "reject_code": "news_shock"}, [])])
    eligible = _add_signal(conn, bar_ts=NOW - timedelta(hours=2))
    later = _add_signal(conn, bar_ts=NOW - timedelta(hours=1), plugin="later")

    loop.run_once("signal", signal_max_id=eligible)

    assert _status(conn, eligible)["status"] == "consumed"
    assert _status(conn, later)["status"] == "pending"


def test_claim_exception_closes_mission_and_does_not_leave_claim(tmp_path):
    conn, loop, runner, _ = _loop(tmp_path, [])
    signal_id = _add_signal(conn)

    with patch("agentic_fx.loops.trade_loop.signals.claim_oldest",
               side_effect=RuntimeError("database unavailable")):
        loop.run_once("signal")

    assert runner.missions == []
    assert _status(conn, signal_id)["status"] == "pending"
    assert conn.execute("SELECT status FROM missions").fetchone()[0] == "failed"


def test_decision_after_freshness_boundary_is_abandoned_without_decision(tmp_path):
    conn, loop, runner, _ = _loop(tmp_path, [MissionResult(
        "completed", {"decision": "reject", "reject_code": "news_shock"}, [])])
    signal_id = _add_signal(conn)
    runner_before = runner.run

    def run_and_advance(mission):
        result = runner_before(mission)
        later = NOW + timedelta(hours=2, seconds=1)
        loop.clock = FixedClock(later)
        loop.executor.clock = loop.clock
        return result

    runner.run = run_and_advance
    loop.run_once("signal")

    assert _status(conn, signal_id)["status"] == "abandoned"
    assert conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 0
