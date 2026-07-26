from datetime import datetime, timezone

from agentic_fx.core.contracts import OrderStatus
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


def test_insert_and_get(tmp_path):
    c = _conn(tmp_path)
    oid = orders.insert(c, pair="USDJPY", direction="long", entry_type="limit",
                        horizon="day", status=OrderStatus.PENDING_FILL, now=NOW,
                        quantity=0.1, stop_loss=147.8, requested_price=148.2)
    row = orders.get(c, oid)
    assert row["pair"] == "USDJPY"
    assert row["status"] == "pending_fill"
    assert row["quantity"] == 0.1
    assert orders.get(c, 9999) is None


def test_update_fields_touches_updated_at(tmp_path):
    c = _conn(tmp_path)
    oid = orders.insert(c, pair="USDJPY", direction="long", entry_type="market",
                        horizon="swing", status=OrderStatus.SUBMITTING, now=NOW)
    later = datetime(2026, 7, 26, 13, 0, tzinfo=timezone.utc)
    orders.update_fields(c, oid, now=later, status=OrderStatus.OPEN,
                         avg_fill_price=148.25, filled_quantity=0.1)
    row = orders.get(c, oid)
    assert row["status"] == "open"
    assert row["updated_at"] == later.isoformat()


def test_list_by_status(tmp_path):
    c = _conn(tmp_path)
    orders.insert(c, pair="USDJPY", direction="long", entry_type="limit",
                  horizon="day", status=OrderStatus.PENDING_FILL, now=NOW)
    orders.insert(c, pair="EURUSD", direction="short", entry_type="market",
                  horizon="day", status=OrderStatus.OPEN, now=NOW)
    rows = orders.list_by_status(c, OrderStatus.PENDING_FILL, OrderStatus.OPEN)
    assert len(rows) == 2
    assert len(orders.list_by_status(c, OrderStatus.CLOSED)) == 0
