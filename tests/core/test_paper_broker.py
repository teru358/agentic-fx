from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock, OrderStatus
from agentic_fx.core.paper_broker import PaperBroker, compute_pnl
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _broker(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c, PaperBroker(c, SETTINGS, FixedClock(NOW))


def test_initial_equity_is_starting_balance(tmp_path):
    _, b = _broker(tmp_path)
    balance, equity = b.equity()
    assert balance == equity == 1_000_000


def test_submit_returns_ok_with_paper_id(tmp_path):
    c, b = _broker(tmp_path)
    oid = orders.insert(c, pair="USDJPY", direction="long", entry_type="market",
                        horizon="day", status=OrderStatus.SUBMITTING, now=NOW,
                        quantity=0.1)
    r = b.submit(orders.get(c, oid), entry_price=148.50)
    assert r.status == "ok"
    assert r.broker_order_id == f"paper-{oid}"


def test_compute_pnl_long_short():
    long_row = {"direction": "long", "avg_fill_price": 148.50,
                "quantity": 0.1}
    # (149.00-148.50)*100000*0.1 = 5000
    assert compute_pnl(long_row, 149.00, contract_size=100_000,
                       commission_per_lot=0.0) == pytest.approx(5000)
    short_row = {"direction": "short", "avg_fill_price": 148.50,
                 "quantity": 0.1}
    assert compute_pnl(short_row, 149.00, contract_size=100_000,
                       commission_per_lot=0.0) == pytest.approx(-5000)


def test_balance_reflects_realized_pnl(tmp_path):
    c, b = _broker(tmp_path)
    oid = orders.insert(c, pair="USDJPY", direction="long", entry_type="market",
                        horizon="day", status=OrderStatus.CLOSED, now=NOW,
                        quantity=0.1, realized_pnl=5000.0)
    balance, _ = b.equity()
    assert balance == 1_005_000
