"""専用 signal mission の claim・採否 transaction・実行境界。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from agentic_fx.core.contracts import FixedClock
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import signals

from tests.loops.test_trade_loop import NOW, SETTINGS, _loop


def _add_signal(conn, *, plugin="sig1", pair="USDJPY", timeframe="1h",
                bar_ts=None, direction="long"):
    bar_ts = bar_ts or (NOW - timedelta(hours=1))
    return signals.add(
        conn, plugin=plugin, content_hash=f"hash-{plugin}-{bar_ts.isoformat()}",
        pair=pair, timeframe=timeframe, bar_ts=bar_ts.isoformat(), kind="strategy",
        payload={"action": "open", "pair": pair, "direction": direction,
                 "entry_type": "market", "horizon": "day", "stop_loss": 147.0,
                 "take_profit": 150.0, "confidence": 0.5,
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


def test_opposite_position_reject_requires_matching_active_opposite_order(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"decision": "reject", "reject_code": "opposite_position"}, [])])
    signal_id = _add_signal(conn)

    loop.run_once("signal")

    assert _status(conn, signal_id)["status"] == "pending"
    assert conn.execute("SELECT status FROM missions").fetchone()[0] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 0


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
