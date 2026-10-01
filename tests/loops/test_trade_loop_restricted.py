"""state が restricted の間の trade loop (signal 起動) の requeue と解除後の再 claim。"""
from datetime import timedelta

import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import missions, orders

from tests.loops.test_trade_loop import NOW, SETTINGS, _loop
from tests.loops.test_trade_loop_signal import _add_signal

OPEN_RESULT = {"action": "open", "pair": "USDJPY", "direction": "long",
               "entry_type": "market", "horizon": "day", "stop_loss": 147.0,
               "take_profit": 149.0, "reasoning": "x"}


def _signal_row(conn, sid):
    return dict(conn.execute(
        "SELECT status, requeue_count FROM signals WHERE id=?", (sid,)).fetchone())


def _set_now(loop, when):
    clock = FixedClock(when)
    loop.clock = clock
    loop.executor.clock = clock


@pytest.mark.parametrize("data_state", ["degraded", "restricted"])
def test_claimed_open_signal_is_abandoned_once_requeue_limit_is_reached(tmp_path, data_state):
    limit = SETTINGS.plugin.signal_requeue_max
    conn, loop, runner, _ = _loop(
        tmp_path, [MissionResult("completed", OPEN_RESULT, [])] * (limit + 1))
    loop.executor.state_fn = lambda: data_state
    sid = _add_signal(conn)

    for attempt in range(limit):
        loop.run_once("signal")
        assert _signal_row(conn, sid) == {"status": "pending", "requeue_count": attempt + 1}
    loop.run_once("signal")

    assert _signal_row(conn, sid)["status"] == "abandoned"
    assert orders.list_by_status(conn, "open", "pending_fill", "submitting") == []
    rejected = conn.execute(
        "SELECT count(*) FROM trade_intents WHERE gate_result='rejected' "
        "AND reject_category='risk_gate'").fetchone()[0]
    assert rejected == limit + 1


@pytest.mark.parametrize("data_state", ["degraded", "restricted"])
def test_after_the_data_state_clears_the_signal_is_claimed_again_only_after_the_min_interval(
        tmp_path, data_state):
    interval = SETTINGS.plugin.signal_min_interval_min
    conn, loop, _, _ = _loop(tmp_path, [
        MissionResult("completed", OPEN_RESULT, []),
        MissionResult("completed", {"action": "hold", "reasoning": "ok"}, [])])
    state = {"v": data_state}
    loop.executor.state_fn = lambda: state["v"]
    sid = _add_signal(conn)
    loop.run_once("signal")
    assert _signal_row(conn, sid)["status"] == "pending"

    # 直前の signal mission から min_interval 未満の間は、解除されていても起動しない
    state["v"] = "ready"
    assert not missions.signals_rate_ok(
        conn, NOW + timedelta(minutes=interval - 1), SETTINGS)
    assert missions.signals_rate_ok(conn, NOW + timedelta(minutes=interval), SETTINGS)

    # 間隔が経過し、まだ fresh (13:00 まで) なら再 claim されて consume される
    _set_now(loop, NOW + timedelta(minutes=interval))
    loop.run_once("signal")
    assert _signal_row(conn, sid)["status"] == "consumed"


def test_close_and_cancel_signals_are_consumed_and_executed_when_restricted(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [])
    loop.executor.state_fn = lambda: "restricted"
    open_oid = orders.insert(
        conn, pair="USDJPY", direction="long", entry_type="market", horizon="day",
        status="open", now=NOW, quantity=0.1, avg_fill_price=148.50)
    pending_oid = orders.insert(
        conn, pair="USDJPY", direction="long", entry_type="limit", horizon="day",
        status="pending_fill", now=NOW, quantity=0.1, remaining_quantity=0.1,
        requested_price=148.20, stop_loss=147.80, take_profit=149.00, expires_at=None)
    sid_close = _add_signal(conn, plugin="sigc")
    sid_cancel = _add_signal(conn, plugin="sigd")

    for result in ({"action": "close", "order_id": open_oid, "reasoning": "x"},
                   {"action": "cancel", "order_id": pending_oid, "reasoning": "x"}):
        loop.runner._results = [MissionResult("completed", result, [])]
        loop.run_once("signal")

    assert _signal_row(conn, sid_close)["status"] == "consumed"
    assert _signal_row(conn, sid_cancel)["status"] == "consumed"
    assert orders.get(conn, open_oid)["status"] == "closed"
    assert orders.get(conn, pending_oid)["status"] == "cancelled"
