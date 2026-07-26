from datetime import datetime, timezone

import pytest

from agentic_fx.core.contracts import (
    Action, Direction, EntryType, Horizon, IntentParseError,
    Origin, OrderStatus, FixedClock, TradeIntent,
)


def _open_dict(**over):
    d = {
        "action": "open", "pair": "USDJPY", "direction": "long",
        "entry_type": "limit", "horizon": "day",
        "limit_price": 148.20, "expires_in": "4h",
        "stop_loss": 147.80, "take_profit": 149.00,
        "confidence": 0.7, "reasoning": "test",
    }
    d.update(over)
    return d


def test_from_llm_dict_open_limit():
    it = TradeIntent.from_llm_dict(_open_dict(), origin=Origin.SCHEDULER)
    assert it.action is Action.OPEN
    assert it.direction is Direction.LONG
    assert it.entry_type is EntryType.LIMIT
    assert it.horizon is Horizon.DAY
    assert it.expires_in_h == 4.0
    assert it.origin is Origin.SCHEDULER


def test_open_requires_stop_loss():
    with pytest.raises(IntentParseError, match="stop_loss"):
        TradeIntent.from_llm_dict(_open_dict(stop_loss=None), origin=Origin.SCHEDULER)


def test_open_requires_horizon():
    d = _open_dict()
    del d["horizon"]
    with pytest.raises(IntentParseError, match="horizon"):
        TradeIntent.from_llm_dict(d, origin=Origin.SCHEDULER)


def test_limit_requires_limit_price_and_expiry():
    with pytest.raises(IntentParseError, match="limit_price"):
        TradeIntent.from_llm_dict(_open_dict(limit_price=None), origin=Origin.SCHEDULER)


def test_market_rejects_limit_fields():
    d = _open_dict(entry_type="market")
    with pytest.raises(IntentParseError, match="limit"):
        TradeIntent.from_llm_dict(d, origin=Origin.SCHEDULER)


def test_close_requires_order_id():
    with pytest.raises(IntentParseError, match="order_id"):
        TradeIntent.from_llm_dict({"action": "close"}, origin=Origin.SCHEDULER)


def test_close_rejects_bool_true_order_id():
    with pytest.raises(IntentParseError, match="order_id"):
        TradeIntent.from_llm_dict({"action": "close", "order_id": True},
                                  origin=Origin.SCHEDULER)


def test_close_rejects_bool_false_order_id():
    with pytest.raises(IntentParseError, match="order_id"):
        TradeIntent.from_llm_dict({"action": "close", "order_id": False},
                                  origin=Origin.SCHEDULER)


def test_cancel_rejects_zero_order_id():
    with pytest.raises(IntentParseError, match="order_id"):
        TradeIntent.from_llm_dict({"action": "cancel", "order_id": 0},
                                  origin=Origin.SCHEDULER)


def test_cancel_rejects_negative_order_id():
    with pytest.raises(IntentParseError, match="order_id"):
        TradeIntent.from_llm_dict({"action": "cancel", "order_id": -1},
                                  origin=Origin.SCHEDULER)


def test_close_accepts_positive_order_id():
    it = TradeIntent.from_llm_dict({"action": "close", "order_id": 42},
                                   origin=Origin.SCHEDULER)
    assert it.order_id == 42


def test_hold_minimal():
    it = TradeIntent.from_llm_dict({"action": "hold", "reasoning": "様子見"},
                                   origin=Origin.SCHEDULER)
    assert it.action is Action.HOLD


def test_unknown_action_rejected():
    with pytest.raises(IntentParseError, match="action"):
        TradeIntent.from_llm_dict({"action": "buy"}, origin=Origin.SCHEDULER)


def test_nan_price_rejected():
    with pytest.raises(IntentParseError, match="finite"):
        TradeIntent.from_llm_dict(_open_dict(stop_loss=float("nan")),
                                  origin=Origin.SCHEDULER)


def test_order_status_has_all_states():
    names = {s.value for s in OrderStatus}
    assert {"approval_pending", "submitting", "submitted", "pending_fill",
            "protection_pending", "open", "closing", "closed", "cancelling",
            "cancelled", "cancel_unknown", "close_unknown", "submit_unknown",
            "rejected", "expired", "invalidated"} == names


def test_fixed_clock():
    dt = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    assert FixedClock(dt).now() == dt
