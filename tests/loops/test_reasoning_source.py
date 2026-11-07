"""反応統計を見た判断の reasoning は、振り返りと RAG へ渡さない。"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops.reflection_cycle import ReflectionCycle
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.store import reflections

from tests.loops.test_trade_loop import NOW, SETTINGS, _loop
from tests.loops.test_trade_loop_signal import _add_signal

MARK = "REACTMARK-7777.25"
_OPEN = {"action": "open", "pair": "USDJPY", "direction": "long",
         "entry_type": "limit", "horizon": "day", "limit_price": 148.20,
         "expires_in": "4h", "stop_loss": 147.80, "take_profit": 149.00,
         "reasoning": f"過去の反応の range_median は {MARK} だった"}
_BASE = {"get_ohlcv": 1, "get_indicators": 1}
_WITHHELD = "振り返りに渡さない"
_NO_KEY = "判断理由の出所が記録されていない"


def _open_mission(tmp_path, calls, *, trigger="cron", output=None):
    conn, loop, runner, _ = _loop(tmp_path, [MissionResult(
        "completed", dict(output or _OPEN), [], market_tool_calls=calls)])
    out = loop.run_once(trigger)
    assert out["result"] == "pending"
    return conn, loop


def _source(conn):
    payload = json.loads(conn.execute(
        "SELECT payload_json FROM trade_intents").fetchone()[0])
    return payload.get("reasoning_source")


def _close_order(conn):
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    conn.execute(
        "UPDATE orders SET status='closed', quantity=0.1, realized_pnl=-1500.0, "
        "close_reason='sl', avg_fill_price=148.5, close_price=148.0 WHERE id=?",
        (oid,))
    conn.commit()
    return oid


def _reflect(tmp_path, conn):
    rag = MagicMock()
    runner = FakeRunner([MissionResult("completed", {"content": "学び"}, [])])
    cycle = ReflectionCycle(
        conn=conn, runner=runner, rag=rag, settings=SETTINGS,
        activity=ActivityLog(tmp_path / "r.log"), clock=FixedClock(NOW),
        core_lock=threading.RLock())
    assert cycle.run_pending() == 1
    return runner.missions[0].prompt, repr(rag.mock_calls)


def _entry_reasoning(prompt):
    body = prompt.split("## トレード詳細\n", 1)[1]
    return json.loads(body)["entry_reasoning"]


def test_reasoning_that_cites_reactions_never_reaches_reflection_or_rag(tmp_path):
    conn, _ = _open_mission(tmp_path, {**_BASE, "get_econ_reactions": 1})
    _close_order(conn)
    prompt, rag = _reflect(tmp_path, conn)
    assert MARK not in prompt and "7777" not in prompt
    assert MARK not in rag and "7777" not in rag
    assert _WITHHELD in _entry_reasoning(prompt)
    assert _source(conn) == "llm_with_reactions"


def test_reasoning_of_a_mission_that_did_not_call_it_is_passed(tmp_path):
    conn, _ = _open_mission(tmp_path, {**_BASE, "get_econ_reactions": 0})
    assert _source(conn) == "llm"
    _close_order(conn)
    prompt, _ = _reflect(tmp_path, conn)
    assert _entry_reasoning(prompt) == _OPEN["reasoning"]


def test_signal_derived_intent_reasoning_is_passed(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed", {"decision": "accept", "reject_code": None}, [],
        market_tool_calls={**_BASE, "get_econ_reactions": 5})])
    _add_signal(conn)
    loop.econ.window_view.return_value.in_window = False
    loop.executor.gather_open_snapshot = lambda intent, **kwargs: SimpleNamespace(
        captured_at=NOW)
    loop.executor.open_from_snapshot = lambda *a, **k: {
        "result": "submitted", "order_id": None, "reasons": []}
    loop.run_once("signal")
    assert _source(conn) == "strategy"


@pytest.mark.parametrize("trigger", ["cron", "manual", "ask-now"])
def test_every_non_signal_trigger_is_judged(tmp_path, trigger):
    conn, _ = _open_mission(
        tmp_path, {**_BASE, "get_econ_reactions": 3}, trigger=trigger)
    assert _source(conn) == "llm_with_reactions"


@pytest.mark.parametrize("calls", [
    None,
    {},
    dict(_BASE),
    {**_BASE, "get_econ_reactions": "1"},
    {**_BASE, "get_econ_reactions": True},
])
def test_unknown_call_count_is_treated_as_having_called(tmp_path, calls):
    conn, _ = _open_mission(tmp_path, calls)
    assert _source(conn) == "llm_with_reactions"


def test_failed_calls_count_as_calls_in_the_worker_counter():
    from agentic_fx import mission_worker
    counter = mission_worker.new_market_tool_counter()
    on_result, calls = counter
    for ok, err in ((False, "invalid_args"), (False, "exception"),
                    (False, "boom"), (True, None)):
        on_result("get_econ_reactions", ok, err)
    assert calls["get_econ_reactions"] == 4
    assert calls["get_ohlcv"] == 0


def test_registry_execution_reaches_the_counter_hook(tmp_path):
    from agentic_fx import mission_worker
    from agentic_fx.store.db import connect, init_db
    from agentic_fx.store.rag import Rag
    from agentic_fx.tools.mission_registry import build_mission_registry
    from tests.tools.test_mission_registry import FakeEmbedding

    on_result, calls = mission_worker.new_market_tool_counter()
    conn = connect(tmp_path / "x.db")
    init_db(conn)
    registry = build_mission_registry(
        "trade", conn, SETTINGS, FixedClock(NOW),
        Rag(tmp_path / "rag", embedding_function=FakeEmbedding()),
        activity=ActivityLog(tmp_path / "a.log"), readonly=True,
        on_tool_result=on_result)
    allowed = ["get_econ_reactions"]
    registry.execute("get_econ_reactions", {"pair": "USDJPY"}, allowed)
    registry.execute("get_econ_reactions", {}, allowed)
    registry.execute("get_econ_reactions", {"pair": "NOTAPAIR"}, allowed)
    assert calls["get_econ_reactions"] == 3
    assert calls["get_ohlcv"] == 0


def _intent_row(conn, payload):
    mid = conn.execute(
        "INSERT INTO missions (loop, runner, model, status, started_at) "
        "VALUES ('trade', 'fake', 'fake', 'completed', ?) RETURNING id",
        (NOW.isoformat(),)).fetchone()[0]
    iid = conn.execute(
        "INSERT INTO trade_intents (mission_id, payload_json, created_at) "
        "VALUES (?, ?, ?) RETURNING id",
        (mid, json.dumps(payload), NOW.isoformat())).fetchone()[0]
    conn.execute("UPDATE orders SET intent_id=?", (iid,))
    conn.commit()


@pytest.mark.parametrize("source,expect", [
    ("strategy", "R"), ("llm", "R"),
    ("llm_with_reactions", _WITHHELD), (None, _NO_KEY)])
def test_reflection_has_three_states_for_entry_reasoning(tmp_path, source, expect):
    from tests.loops.test_reflection_cycle import _closed_order, _cycle
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "x"}, [])])
    _closed_order(conn)
    payload = {"reasoning": "R"}
    if source is not None:
        payload["reasoning_source"] = source
    _intent_row(conn, payload)
    cyc.run_pending()
    got = _entry_reasoning(cyc.runner.missions[0].prompt)
    if expect == "R":
        assert got == "R"
    else:
        assert expect in got and "R" != got


def test_no_intent_stays_null(tmp_path):
    from tests.loops.test_reflection_cycle import _closed_order, _cycle
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "x"}, [])])
    _closed_order(conn)
    cyc.run_pending()
    assert _entry_reasoning(cyc.runner.missions[0].prompt) is None
