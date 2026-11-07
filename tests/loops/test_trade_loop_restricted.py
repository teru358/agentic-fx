"""専用採否経路は data state が使えない間、判断確定を保留する。"""
from types import SimpleNamespace

from tests.loops.test_trade_loop import NOW
from agentic_fx.runners.base import MissionResult

from tests.loops.test_trade_loop import _loop
from tests.loops.test_trade_loop_signal import _add_signal


def test_non_ready_state_requeues_without_decision_or_intent(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"decision": "accept", "reject_code": None}, [])])
    loop.executor.state_fn = lambda: "restricted"
    signal_id = _add_signal(conn)
    loop.executor.gather_open_snapshot = lambda intent, **kwargs: SimpleNamespace(
        captured_at=NOW)

    loop.run_once("signal")

    row = conn.execute(
        "SELECT status,requeue_count FROM signals WHERE id=?", (signal_id,)
    ).fetchone()
    assert tuple(row) == ("pending", 1)
    assert conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trade_intents").fetchone()[0] == 0


def test_ready_state_accepts_payload_and_executor_runs_after_commit(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"decision": "accept", "reject_code": None}, [])])
    signal_id = _add_signal(conn)
    loop.econ.window_view.return_value.in_window = False
    loop.executor.gather_open_snapshot = lambda intent, **kwargs: SimpleNamespace(
        captured_at=NOW)

    def execute(intent, iid, snapshot, **kwargs):
        assert conn.execute(
            "SELECT 1 FROM signal_decisions WHERE signal_id=? AND trade_intent_id=?",
            (signal_id, iid)).fetchone()
        return {"result": "submitted", "order_id": None, "reasons": []}

    loop.executor.open_from_snapshot = execute
    out = loop.run_once("signal")

    assert out["result"] == "submitted"
    assert conn.execute(
        "SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()[0] == "consumed"
