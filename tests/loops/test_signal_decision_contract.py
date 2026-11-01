from __future__ import annotations

import json

import jsonschema
import pytest

from agentic_fx.core.contracts import Direction, OrderStatus
from agentic_fx.loops.signal_decision_contract import (
    Accept,
    DECISION_SCHEMA,
    Invalid,
    REJECT_CODES,
    Reject,
    SIGNAL_DECISION_TOOLS,
    parse_decision,
    verify_opposite_position,
)


def test_decision_schema_only_allows_the_two_canonical_shapes():
    jsonschema.Draft202012Validator.check_schema(DECISION_SCHEMA)
    jsonschema.validate({"decision": "accept", "reject_code": None}, DECISION_SCHEMA)
    for code in REJECT_CODES:
        jsonschema.validate({"decision": "reject", "reject_code": code}, DECISION_SCHEMA)

    for payload in (
        {"decision": "accept", "reject_code": "news_shock"},
        {"decision": "reject", "reject_code": None},
        {"decision": "reject", "reject_code": "technical_disagreement"},
        {"decision": "hold", "reject_code": None},
        {"action": "open", "pair": "USDJPY", "reasoning": "ordinary intent"},
        {"decision": "accept", "reject_code": None, "extra": True},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(payload, DECISION_SCHEMA)


@pytest.mark.parametrize(
    ("payload", "expected_type", "expected_value"),
    [
        ({"decision": "accept", "reject_code": None}, Accept, None),
        ({"decision": "reject", "reject_code": "news_shock"}, Reject, "news_shock"),
        ({"decision": "reject", "reject_code": "opposite_position"}, Reject, "opposite_position"),
    ],
)
def test_parse_decision_accepts_only_canonical_shapes(payload, expected_type, expected_value):
    result = parse_decision(json.dumps(payload))
    assert isinstance(result, expected_type)
    if isinstance(result, Reject):
        assert result.code == expected_value


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "accept", "reject_code": "news_shock"},
        {"decision": "reject", "reject_code": None},
        {"decision": "hold", "reject_code": None},
        {"action": "open", "pair": "USDJPY", "reasoning": "ordinary intent"},
        {"decision": "reject", "reject_code": "unknown"},
        {"decision": "accept", "reject_code": None, "unknown": "key"},
    ],
)
def test_parser_rejects_every_noncanonical_shape_even_when_schema_is_bypassed(payload):
    result = parse_decision(payload)
    assert isinstance(result, Invalid)
    assert result.detail in {"invalid_json", "invalid_shape"}
    assert "ordinary intent" not in result.detail


def test_parser_does_not_treat_non_object_or_bad_json_as_a_decision():
    assert parse_decision("not json") == Invalid("invalid_json")
    assert parse_decision("[]") == Invalid("invalid_shape")
    assert parse_decision([{"decision": "accept"}]) == Invalid("invalid_shape")


def test_schema_and_parser_are_independent_defences():
    payload = {"decision": "accept", "reject_code": "news_shock"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, DECISION_SCHEMA)
    assert parse_decision(payload) == Invalid("invalid_shape")


def test_opposite_position_reject_requires_an_active_order_in_the_inverse_direction():
    reject = Reject("opposite_position", pair="USDJPY", direction=Direction.LONG)
    orders = [
        {"pair": "USDJPY", "direction": "short", "status": OrderStatus.OPEN},
    ]
    assert verify_opposite_position(reject, orders) is True


@pytest.mark.parametrize(
    "orders",
    [
        [],
        [{"pair": "USDJPY", "direction": "long", "status": OrderStatus.OPEN}],
        [{"pair": "EURUSD", "direction": "short", "status": OrderStatus.OPEN}],
        [{"pair": "USDJPY", "direction": "short", "status": OrderStatus.CLOSED}],
    ],
)
def test_opposite_position_reject_rejects_missing_or_non_opposite_active_orders(orders):
    reject = Reject("opposite_position", pair="USDJPY", direction="long")
    assert verify_opposite_position(reject, orders) is False


def test_pending_fill_opposite_position_is_an_active_inverse_order():
    reject = Reject("opposite_position", pair="USDJPY", direction="short")
    orders = [{"pair": "USDJPY", "direction": "long", "status": "pending_fill"}]
    assert verify_opposite_position(reject, orders) is True


def test_protection_pending_opposite_position_is_an_active_inverse_order():
    reject = Reject("opposite_position", pair="USDJPY", direction="short")
    orders = [{"pair": "USDJPY", "direction": "long", "status": "protection_pending"}]
    assert verify_opposite_position(reject, orders) is True


def test_news_shock_does_not_require_orders_verification():
    assert verify_opposite_position(Reject("news_shock"), []) is True


def test_signal_decision_tools_are_limited_to_news_search_and_positions():
    assert SIGNAL_DECISION_TOOLS == ("search_news", "get_positions")
