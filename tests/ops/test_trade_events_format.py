"""取引 event の形と数値書式 (AC-1, AC-2)。"""
from __future__ import annotations

import math

import pytest

from agentic_fx.ops import events as ev
from agentic_fx.ops import trade_events as te
from agentic_fx.ops.events import EventSchemaError, validate_fields

ORDER = {"state": "placed", "pair": "USDJPY", "side": "long", "entry_type": "limit",
         "qty": "0.1", "price": "150.123", "sl": "149.8", "tp": "150.9",
         "origin_code": "cron", "mode": "paper", "state_mode": "learning",
         "reason_code": None}
CLOSE = {"pair": "USDJPY", "side": "long", "qty": "0.1", "entry_price": "150.123",
         "close_price": "150.9", "pnl": "-1234.50", "ccy": "JPY", "pnl_r": "-0.50",
         "held_sec": 3600, "reason_code": "sl", "origin_code": "signal:rsi_div",
         "mode": "paper", "state_mode": None}


def test_ac1_fields_registered_with_exact_sets():
    assert len(ev.FIELDS["order_state_changed"]) == 12
    assert len(ev.FIELDS["position_closed"]) == 13
    assert ev.FIELDS["order_state_changed"] == frozenset(ORDER)
    assert ev.FIELDS["position_closed"] == frozenset(CLOSE)


def test_ac1_a_realistic_event_validates():
    validate_fields("order_state_changed", ORDER, "order:12")
    validate_fields("position_closed", CLOSE, "order:12")


@pytest.mark.parametrize("code,base", [("order_state_changed", ORDER), ("position_closed", CLOSE)])
def test_ac1_missing_extra_float_and_free_text_are_rejected(code, base):
    missing = dict(base)
    missing.pop("pair")
    with pytest.raises(EventSchemaError):
        validate_fields(code, missing, "order:1")
    with pytest.raises(EventSchemaError):
        validate_fields(code, {**base, "extra": "x"}, "order:1")
    with pytest.raises(EventSchemaError):
        validate_fields(code, {**base, "qty": 0.1}, "order:1")
    with pytest.raises(EventSchemaError):
        validate_fields(code, {**base, "pair": "free text"}, "order:1")
    with pytest.raises(EventSchemaError):
        validate_fields(code, {**base, "qty": "0.10000000"}, "order:1")


@pytest.mark.parametrize("ref,ok", [
    ("order:1", True), ("order:0", False), ("order:9223372036854775807", True),
    ("order:9223372036854775808", False), ("order:", False), ("order:-1", False),
    ("order:12345678901234567890", False), ("order:01", True), ("job:1", False)])
def test_ac1_ref_range(ref, ok):
    if ok:
        validate_fields("position_closed", CLOSE, ref)
    else:
        with pytest.raises(EventSchemaError):
            validate_fields("position_closed", CLOSE, ref)


def test_ac1_order_reason_code_only_for_cancelled():
    with pytest.raises(EventSchemaError):
        validate_fields("order_state_changed", {**ORDER, "reason_code": "llm_cancel"}, "order:1")
    validate_fields("order_state_changed",
                    {**ORDER, "state": "cancelled", "reason_code": "llm_cancel"}, "order:1")


@pytest.mark.parametrize("value,text", [
    (150.123, "150.123"), (149.80000, "149.8"), (0.01, "0.01"), (150.0, "150"),
    (1.12345, "1.12345"), (0.1, "0.1"), (1.2345678912, "1.23456789"),
    (110.001, "110.001"), (0.1 + 0.2, "0.3")])
def test_ac2_price_format_keeps_fx_precision(value, text):
    assert te.format_price(value) == text


@pytest.mark.parametrize("value", [
    math.nan, math.inf, -math.inf, 0.0, -1.0, 1e12, 1e300, 0.4e-8, 1e-9])
def test_ac2_price_rejects_unformattable(value):
    with pytest.raises(te.InvalidValue):
        te.format_price(value)


def test_ac2_price_boundary_just_below_limit():
    assert te.format_price(999999999999.0) == "999999999999"
    with pytest.raises(te.InvalidValue):
        te.format_price(0.5e-8 - 1e-12)


def test_ac2_money_and_r():
    assert te.format_money(-1234.5) == "-1234.50"
    assert te.format_money(-0.004) == "0.00"
    assert te.format_money(0.0) == "0.00"
    assert te.format_money(12.345) in ("12.35", "12.34")
    assert te.format_r(-0.001) == "0.00"
    assert te.format_r(2.4) == "2.40"
    for bad in (math.nan, math.inf, 1e15, -1e15):
        with pytest.raises(te.InvalidValue):
            te.format_money(bad)
    for bad in (math.nan, math.inf, 1e6, -1e6):
        with pytest.raises(te.InvalidValue):
            te.format_r(bad)


def test_ac2_none_stays_none_for_optional_inputs():
    assert te.format_money(None) is None
    assert te.format_r(None) is None


@pytest.mark.parametrize("name,bad", [
    ("qty", "0001.20"), ("qty", "+1.5"), ("qty", "1.50"), ("qty", "0"), ("qty", "1e3"),
    ("qty", "-1"), ("qty", "1" * 13), ("pnl", "abc"), ("pnl", "1e999"), ("pnl", "-0.00"),
    ("pnl", "1.5"), ("pnl_r", "1000000.00"), ("held_sec", -1), ("held_sec", 10 ** 10),
    ("held_sec", True), ("ccy", "jpy"), ("pair", "usdjpy"), ("pair", "US")])
def test_ac2_grammar_rejects(name, bad):
    with pytest.raises(EventSchemaError):
        validate_fields("position_closed", {**CLOSE, name: bad}, "order:1")


def test_ac2_origin_code_grammar():
    for ok in ("cron", "signal:rsi_div", None):
        validate_fields("position_closed", {**CLOSE, "origin_code": ok}, "order:1")
    for bad in ("Signal:x", "signal:", "signal:Bad", "cron ", "signal:" + "a" * 64):
        with pytest.raises(EventSchemaError):
            validate_fields("position_closed", {**CLOSE, "origin_code": bad}, "order:1")
