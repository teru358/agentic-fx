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
    # (149.00-148.50)*100000*0.1 = 5000 (quote_to_account_rate=1.0: 非退行)
    assert compute_pnl(long_row, 149.00, contract_size=100_000,
                       commission_per_lot=0.0,
                       quote_to_account_rate=1.0) == pytest.approx(5000)
    short_row = {"direction": "short", "avg_fill_price": 148.50,
                 "quantity": 0.1}
    assert compute_pnl(short_row, 149.00, contract_size=100_000,
                       commission_per_lot=0.0,
                       quote_to_account_rate=1.0) == pytest.approx(-5000)


def test_compute_pnl_converts_quote_to_account():
    # 設計書 §5: gross はクォート通貨建てで計算し、quote_to_account_rate で
    # 口座通貨へ換算する。rate≠1.0 で正しく掛かることを固定する (EURUSD
    # golden と対をなす非退行ピン)。
    row = {"direction": "long", "avg_fill_price": 1.1000, "quantity": 1.0}
    # gross_quote = (1.1010-1.1000)*100000*1.0 = 100 (USD)
    # → 100 * 163.665 = 16,366.5 JPY
    pnl = compute_pnl(row, 1.1010, contract_size=100_000,
                      commission_per_lot=0.0,
                      quote_to_account_rate=163.665)
    assert pnl == pytest.approx(16_366.5)


def test_compute_pnl_commission_not_converted():
    # commission_per_lot は口座通貨建てなので rate を掛けてはならない。
    row = {"direction": "long", "avg_fill_price": 1.1000, "quantity": 1.0}
    pnl = compute_pnl(row, 1.1010, contract_size=100_000,
                      commission_per_lot=500.0,
                      quote_to_account_rate=163.665)
    gross_account = (1.1010 - 1.1000) * 100_000 * 1.0 * 163.665
    assert pnl == pytest.approx(gross_account - 500.0)


def test_balance_reflects_realized_pnl(tmp_path):
    c, b = _broker(tmp_path)
    oid = orders.insert(c, pair="USDJPY", direction="long", entry_type="market",
                        horizon="day", status=OrderStatus.CLOSED, now=NOW,
                        quantity=0.1, realized_pnl=5000.0)
    balance, _ = b.equity()
    assert balance == 1_005_000


def test_balance_reflects_fees_swap(tmp_path):
    c, b = _broker(tmp_path)
    oid = orders.insert(c, pair="USDJPY", direction="long", entry_type="market",
                        horizon="day", status=OrderStatus.CLOSED, now=NOW,
                        quantity=0.1, realized_pnl=5000.0, fees_swap=1200.0)
    balance, _ = b.equity()
    # balance = 1_000_000 + 5000 (realized) - 1200 (swap fees) = 1_003_800
    assert balance == 1_003_800


def test_balance_with_mixed_fees_and_pnl(tmp_path):
    c, b = _broker(tmp_path)
    # Order 1: closed with pnl and fees
    orders.insert(c, pair="USDJPY", direction="long", entry_type="market",
                  horizon="day", status=OrderStatus.CLOSED, now=NOW,
                  quantity=0.1, realized_pnl=5000.0, fees_swap=500.0)
    # Order 2: closed with pnl, no fees (fees_swap is NULL)
    orders.insert(c, pair="EURUSD", direction="short", entry_type="market",
                  horizon="day", status=OrderStatus.CLOSED, now=NOW,
                  quantity=0.2, realized_pnl=3000.0)
    balance, _ = b.equity()
    # balance = 1_000_000 + 5000 + 3000 - 500 = 1_007_500
    assert balance == 1_007_500
