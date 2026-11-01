"""Dedicated signal-mission decision contract.

The LLM may only accept a strategy signal or reject it with one of the two
approved reasons.  Parsing is deliberately independent of the JSON schema:
callers must apply both defences before any decision is persisted.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agentic_fx.core.contracts import Direction, OrderStatus

REJECT_CODES = ("news_shock", "opposite_position")
SIGNAL_DECISION_TOOLS = ("search_news", "get_positions")

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["accept", "reject"]},
        "reject_code": {
            "type": ["string", "null"],
            "enum": [None, *REJECT_CODES],
        },
    },
    "required": ["decision", "reject_code"],
    "allOf": [
        {
            "if": {
                "properties": {"decision": {"type": "string", "const": "accept"}},
                "required": ["decision"],
            },
            "then": {
                "properties": {"reject_code": {"type": "null", "const": None}},
            },
        },
        {
            "if": {
                "properties": {"decision": {"type": "string", "const": "reject"}},
                "required": ["decision"],
            },
            "then": {
                "properties": {
                    "reject_code": {"type": "string", "enum": list(REJECT_CODES)},
                },
            },
        },
    ],
}


@dataclass(frozen=True, slots=True)
class Accept:
    """The signal remains executable; signal payload values stay authoritative."""


@dataclass(frozen=True, slots=True)
class Reject:
    """A permitted LLM rejection."""

    code: str


@dataclass(frozen=True, slots=True)
class Invalid:
    """A fail-closed result with a deliberately non-user-supplied detail code."""

    detail: str


def parse_decision(text_or_dict: str | dict[str, object]) -> Accept | Reject | Invalid:
    """Parse only the two canonical LLM response shapes.

    The result never includes LLM-provided text in ``Invalid.detail``.  This
    makes it safe for the mission failure path to record the fixed detail.
    """
    if isinstance(text_or_dict, str):
        try:
            value = json.loads(text_or_dict)
        except (json.JSONDecodeError, ValueError):
            return Invalid("invalid_json")
    else:
        value = text_or_dict
    if not isinstance(value, dict):
        return Invalid("invalid_shape")
    if set(value) != {"decision", "reject_code"}:
        return Invalid("invalid_shape")

    decision = value["decision"]
    reject_code = value["reject_code"]
    if decision == "accept" and reject_code is None:
        return Accept()
    if decision == "reject" and reject_code in REJECT_CODES:
        return Reject(reject_code)
    return Invalid("invalid_shape")


def _field(order: object, name: str) -> object:
    if isinstance(order, dict):
        return order.get(name)
    return getattr(order, name, None)


def _value(value: object) -> str | None:
    if isinstance(value, str):
        return value
    raw = getattr(value, "value", None)
    return raw if isinstance(raw, str) else None


def verify_opposite_position(reject: Reject, orders_for_pair: object, *,
                             pair: str | None, direction: Direction | str | None,
                             ) -> bool:
    """Validate the only order-dependent reject code deterministically.

    ``pair`` and ``direction`` are the claimed signal's identity, supplied by
    the caller.  For ``opposite_position`` any absent or malformed identity
    fails closed.  Other reject codes intentionally require no orders
    validation.
    """
    if reject.code != "opposite_position":
        return reject.code == "news_shock"
    signal_direction = _value(direction)
    if pair is None or signal_direction not in {"long", "short"}:
        return False
    inverse = "short" if signal_direction == "long" else "long"
    active = {
        OrderStatus.OPEN.value,
        OrderStatus.PENDING_FILL.value,
        OrderStatus.PROTECTION_PENDING.value,
    }
    try:
        iterator = iter(orders_for_pair)
    except TypeError:
        return False
    return any(
        _field(order, "pair") == pair
        and _value(_field(order, "direction")) == inverse
        and _value(_field(order, "status")) in active
        for order in iterator
    )
