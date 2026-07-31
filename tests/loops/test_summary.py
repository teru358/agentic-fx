from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock, OrderStatus
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.loops.summary import (
    ANSWER_SCHEMA, TRADE_INTENT_SCHEMA, build_state_summary,
    trade_intent_schema,
)
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def test_schemas_validate():
    jsonschema.validate({"action": "hold", "reasoning": "wait"},
                        TRADE_INTENT_SCHEMA)
    jsonschema.validate({"answer": "回答"}, ANSWER_SCHEMA)
    try:
        jsonschema.validate({"action": "buy"}, TRADE_INTENT_SCHEMA)
        raise AssertionError("should fail")
    except jsonschema.ValidationError:
        pass


def test_trade_intent_schema_pair_enum():
    """trade_intent_schema constrains pair to enum."""
    schema = trade_intent_schema(["USDJPY"])
    # Should validate with correct pair
    jsonschema.validate({"action": "open", "pair": "USDJPY",
                         "direction": "long", "entry_type": "market",
                         "horizon": "day", "reasoning": "test"},
                        schema)
    # Should fail with wrong pair
    try:
        jsonschema.validate({"action": "open", "pair": "EURUSD",
                             "direction": "long", "entry_type": "market",
                             "horizon": "day", "reasoning": "test"},
                            schema)
        raise AssertionError("should fail for wrong pair")
    except jsonschema.ValidationError:
        pass


def test_base_trade_intent_schema_pair_free():
    """TRADE_INTENT_SCHEMA itself allows any pair string."""
    jsonschema.validate({"action": "open", "pair": "XXXYYYZZZ",
                         "direction": "long", "entry_type": "market",
                         "horizon": "day", "reasoning": "test"},
                        TRADE_INTENT_SCHEMA)


def test_schemas_check_schema():
    """All schemas pass check_schema validation."""
    jsonschema.Draft202012Validator.check_schema(TRADE_INTENT_SCHEMA)
    jsonschema.Draft202012Validator.check_schema(ANSWER_SCHEMA)
    jsonschema.Draft202012Validator.check_schema(trade_intent_schema(["USDJPY"]))


def test_summary_contains_positions_and_trades(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    orders.insert(conn, pair="USDJPY", direction="long", entry_type="limit",
                  horizon="swing", status=OrderStatus.PENDING_FILL, now=NOW,
                  quantity=0.1, requested_price=148.2, stop_loss=147.8,
                  take_profit=149.0)
    oid = orders.insert(conn, pair="EURUSD", direction="short",
                        entry_type="market", horizon="day",
                        status=OrderStatus.CLOSED, now=NOW, quantity=0.1,
                        realized_pnl=-1200.0, close_reason="sl")
    econ = MagicMock()
    econ.upcoming.return_value = [
        {"ts": "2026-07-22T19:30:00+00:00", "country": "USD",
         "name": "CPI", "importance": 3}]
    text = build_state_summary(conn, PaperBroker(conn, SETTINGS,
                                                 FixedClock(NOW)),
                               econ, FixedClock(NOW),
                               SETTINGS.paper.starting_balance)
    assert "USDJPY" in text and "swing" in text      # 未約定指値 + horizon
    assert f"#{oid}" in text or str(oid) in text      # order_id 提示
    assert "sl" in text and "-1,200" in text          # 直近トレード要約
    assert "CPI" in text                              # 経済指標
    assert "998,800" in text or "998800" in text      # 残高 (after -1200 loss)
    # F3: Killer test for cumulative P&L (line-specific, not substring)
    pnl_line = next(l for l in text.splitlines() if "累計損益" in l)
    assert "-1,200" in pnl_line


def test_answer_schema_requires_answer(tmp_path):
    """F4: ANSWER_SCHEMA requires 'answer' field."""
    # Empty dict should fail
    try:
        jsonschema.validate({}, ANSWER_SCHEMA)
        raise AssertionError("empty dict should fail ANSWER_SCHEMA")
    except jsonschema.ValidationError:
        pass
    # Missing answer should fail
    try:
        jsonschema.validate({"foo": "bar"}, ANSWER_SCHEMA)
        raise AssertionError("missing answer should fail ANSWER_SCHEMA")
    except jsonschema.ValidationError:
        pass
    # Only answer property is allowed
    assert ANSWER_SCHEMA["properties"] == {"answer": {"type": "string"}}
    assert ANSWER_SCHEMA["required"] == ["answer"]


def test_realized_pnl_none_in_closed_order(tmp_path):
    """F1: Handle realized_pnl=None in closed orders (conversion rate unavailable)."""
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    # Insert closed order without realized_pnl (None case)
    orders.insert(conn, pair="USDJPY", direction="long", entry_type="market",
                  horizon="day", status=OrderStatus.CLOSED, now=NOW,
                  quantity=0.1, close_reason="manual")
    econ = MagicMock()
    econ.upcoming.return_value = []
    # Should not raise TypeError
    text = build_state_summary(conn, PaperBroker(conn, SETTINGS,
                                                 FixedClock(NOW)),
                               econ, FixedClock(NOW),
                               SETTINGS.paper.starting_balance)
    assert "未確定" in text  # Should show "未確定" for None pnl


def test_closed_trades_ordering(tmp_path):
    """F2: closed_at ordering with NULL and id DESC for determinism."""
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    later = datetime(2026, 7, 22, 13, 0, tzinfo=timezone.utc)
    # Insert orders: same closed_at, different ids
    id1 = orders.insert(conn, pair="USDJPY", direction="long", entry_type="market",
                        horizon="day", status=OrderStatus.CLOSED, now=NOW,
                        quantity=0.1, realized_pnl=-100.0, close_reason="sl",
                        closed_at=later)
    id2 = orders.insert(conn, pair="EURUSD", direction="short", entry_type="market",
                        horizon="day", status=OrderStatus.CLOSED, now=NOW,
                        quantity=0.1, realized_pnl=-200.0, close_reason="tp",
                        closed_at=later)
    econ = MagicMock()
    econ.upcoming.return_value = []
    text = build_state_summary(conn, PaperBroker(conn, SETTINGS,
                                                 FixedClock(NOW)),
                               econ, FixedClock(NOW),
                               SETTINGS.paper.starting_balance)
    # With same closed_at, should be ordered by id DESC, so id2 appears first
    lines = text.split("\n")
    id2_idx = next(i for i, l in enumerate(lines) if f"#{id2}" in l)
    id1_idx = next(i for i, l in enumerate(lines) if f"#{id1}" in l)
    assert id2_idx < id1_idx, "higher id should appear first in same closed_at"


def test_limit_10_trades_constraint(tmp_path):
    """F2: LIMIT 10 constraint for recent trades."""
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    # Insert 11 closed orders
    for i in range(11):
        orders.insert(conn, pair="USDJPY", direction="long", entry_type="market",
                      horizon="day", status=OrderStatus.CLOSED, now=NOW,
                      quantity=0.1, realized_pnl=-100.0 * (i + 1),
                      close_reason="sl")
    econ = MagicMock()
    econ.upcoming.return_value = []
    text = build_state_summary(conn, PaperBroker(conn, SETTINGS,
                                                 FixedClock(NOW)),
                               econ, FixedClock(NOW),
                               SETTINGS.paper.starting_balance)
    # Count trade lines (lines starting with "  - #")
    trade_lines = [l for l in text.split("\n") if l.strip().startswith("- #")]
    assert len(trade_lines) == 10, f"expected 10 trades, got {len(trade_lines)}"


def test_protection_pending_in_active_positions(tmp_path):
    """F5: PROTECTION_PENDING status appears in active positions."""
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    orders.insert(conn, pair="USDJPY", direction="long", entry_type="market",
                  horizon="day", status=OrderStatus.PROTECTION_PENDING, now=NOW,
                  quantity=0.1, avg_fill_price=148.0, stop_loss=147.0,
                  take_profit=149.0)
    econ = MagicMock()
    econ.upcoming.return_value = []
    text = build_state_summary(conn, PaperBroker(conn, SETTINGS,
                                                 FixedClock(NOW)),
                               econ, FixedClock(NOW),
                               SETTINGS.paper.starting_balance)
    assert "protection_pending" in text or "PROTECTION_PENDING" in text.lower()
    assert "ポジション/指値:" in text


def test_schema_isolation(tmp_path):
    """F7: Modifying input pairs or returned schema doesn't affect others."""
    pairs = ["USDJPY", "EURUSD"]
    schema1 = trade_intent_schema(pairs)

    # Modify input list
    pairs.append("GBPUSD")

    # Get schema again with original list content
    schema2 = trade_intent_schema(["USDJPY", "EURUSD"])

    # schema1 should not be affected by pairs modification
    assert schema1["properties"]["pair"]["enum"] == ["USDJPY", "EURUSD"]

    # Modifying schema1 shouldn't affect TRADE_INTENT_SCHEMA
    original_pair_spec = TRADE_INTENT_SCHEMA["properties"]["pair"]
    schema1["properties"]["pair"]["enum"].append("XYZABC")
    assert "enum" not in TRADE_INTENT_SCHEMA["properties"]["pair"]
    assert TRADE_INTENT_SCHEMA["properties"]["pair"] == original_pair_spec
