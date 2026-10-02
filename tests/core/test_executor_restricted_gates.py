"""state が restricted の間の executor。

新規 OPEN は snapshot 入口で止まり、CLOSE と CANCEL は通常どおり実行される。
(同期経路の OPEN 拒否と、submit 直前の state 変化は test_executor.py /
test_executor_snapshot.py が pin している。)
"""
import pytest

from agentic_fx.core.contracts import OrderStatus as S, Origin, TradeIntent
from agentic_fx.store import orders

from tests.core.test_executor_snapshot import (
    NOW, _close_intent, _insert_intent, _insert_open_order, _make_executor,
    _open_intent, _start_trade_mission,
)


@pytest.mark.parametrize("data_state", ["degraded", "restricted"])
def test_open_from_snapshot_is_rejected_at_the_entry_when_not_ready(tmp_path, data_state):
    ex = _make_executor(tmp_path)
    intent = _open_intent(pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    iid = _insert_intent(ex.conn, mid, intent)
    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=[])
    ex.state_fn = lambda: data_state

    out = ex.open_from_snapshot(intent, iid, snapshot, max_snapshot_age_sec=999.0)

    assert out["result"] == "rejected"
    assert out["reasons"] == [f"data state {data_state} (fail closed)"]
    assert orders.list_by_status(ex.conn, S.OPEN, S.PENDING_FILL, S.SUBMITTING,
                                 S.REJECTED) == []
    row = ex.conn.execute("SELECT gate_result, reject_category FROM trade_intents "
                          "WHERE id=?", (iid,)).fetchone()
    assert dict(row) == {"gate_result": "rejected", "reject_category": "risk_gate"}


def test_close_intent_is_executed_when_restricted(tmp_path):
    ex = _make_executor(tmp_path)
    ex.state_fn = lambda: "restricted"
    mid = _start_trade_mission(ex.conn)
    oid = _insert_open_order(ex.conn, "USDJPY")["id"]

    out = ex.handle_intent(_close_intent(oid), mid)

    assert out["result"] == "closed"
    assert orders.get(ex.conn, oid)["status"] == "closed"


def test_cancel_intent_is_executed_when_restricted(tmp_path):
    ex = _make_executor(tmp_path)
    ex.state_fn = lambda: "restricted"
    mid = _start_trade_mission(ex.conn)
    oid = orders.insert(
        ex.conn, pair="USDJPY", direction="long", entry_type="limit", horizon="day",
        status=S.PENDING_FILL, now=NOW, quantity=0.1, remaining_quantity=0.1,
        requested_price=148.20, stop_loss=147.80, take_profit=149.00,
        expires_at=None)
    intent = TradeIntent.from_llm_dict({"action": "cancel", "order_id": oid},
                                       origin=Origin.SCHEDULER)

    out = ex.handle_intent(intent, mid)

    assert out["result"] == "cancelled"
    assert orders.get(ex.conn, oid)["status"] == "cancelled"


def test_while_an_observe_failure_is_marked_open_is_rejected_and_close_is_executed(
        tmp_path, monkeypatch):
    import sqlite3
    from datetime import datetime, timedelta, timezone

    from agentic_fx.datafeed.outage import EMPTY_REPORT, OutageStateMachine

    ex = _make_executor(tmp_path)
    machine = OutageStateMachine(
        ex.conn, hard_keys=frozenset({("USDJPY", "1m")}),
        interval_widths={"1m": timedelta(minutes=1)}, grace=timedelta(seconds=30),
        storage_source="mt5-live")
    ex.state_fn = lambda: machine.state

    def crash(**kwargs):
        raise sqlite3.OperationalError("injected crash")

    monkeypatch.setattr(machine, "_save_state", crash)
    with pytest.raises(sqlite3.OperationalError):
        machine.observe(datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc), EMPTY_REPORT)

    intent = _open_intent(pair="USDJPY")
    mid = _start_trade_mission(ex.conn)
    iid = _insert_intent(ex.conn, mid, intent)
    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=[])
    out = ex.open_from_snapshot(intent, iid, snapshot, max_snapshot_age_sec=999.0)
    assert out["result"] == "rejected"
    assert out["reasons"] == ["data state degraded (fail closed)"]

    oid = _insert_open_order(ex.conn, "USDJPY")["id"]
    assert ex.handle_intent(_close_intent(oid), mid)["result"] == "closed"
