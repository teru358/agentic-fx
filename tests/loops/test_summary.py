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
    assert "累計" in text and "-1,200" in text        # 累計 P&L (realized -1200)
