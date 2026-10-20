"""取引 event の allowlist と値の grammar (AC-3 の afx-ops 側、AC-21)。

producer と同じ書式の fixture だけを通し、grammar に合っても canonical でない値、
範囲外・非有限・長さ超過・未知の field は event 全体として拒否する。
"""
from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from afx_ops.notify.payload import ALLOWED_FIELDS, PayloadError, encode_event

TS = "2026-10-20T00:00:00+00:00"


def _order(**fields) -> dict:
    base = {"state": "placed", "pair": "USDJPY", "side": "long", "entry_type": "limit",
            "qty": "0.1", "price": "150.123", "sl": "149.8", "tp": "150.9",
            "origin_code": "signal:rsi_div", "mode": "paper", "state_mode": "learning",
            "reason_code": None}
    base.update(fields)
    return {"id": 7, "ts": TS, "code": "order_state_changed", "ref": "order:12",
            "fields": base}


def _close(**fields) -> dict:
    base = {"pair": "USDJPY", "side": "long", "qty": "0.1", "entry_price": "150.123",
            "close_price": "150.9", "pnl": "7770.00", "ccy": "JPY", "pnl_r": "2.40",
            "held_sec": 11520, "reason_code": "tp", "origin_code": "signal:rsi_div",
            "mode": "paper", "state_mode": "learning"}
    base.update(fields)
    return {"id": 8, "ts": TS, "code": "position_closed", "ref": "order:12", "fields": base}


def test_ac3_allowlist_has_the_two_trade_codes_with_exact_field_sets():
    assert ALLOWED_FIELDS["order_state_changed"] == frozenset({
        "state", "pair", "side", "entry_type", "qty", "price", "sl", "tp", "origin_code",
        "mode", "state_mode", "reason_code"})
    assert len(ALLOWED_FIELDS["order_state_changed"]) == 12
    assert ALLOWED_FIELDS["position_closed"] == frozenset({
        "pair", "side", "qty", "entry_price", "close_price", "pnl", "ccy", "pnl_r", "held_sec",
        "reason_code", "origin_code", "mode", "state_mode"})
    assert len(ALLOWED_FIELDS["position_closed"]) == 13


def test_ac3_a_realistic_order_and_close_event_encode_into_the_webhook_shape():
    order = encode_event(_order())
    assert order == {"event_id": 7, "ts": TS, "code": "order_state_changed", "ref": "order:12",
                     **_order()["fields"]}
    close = encode_event(_close(pnl="-1234.50"))
    assert close["pnl"] == "-1234.50" and close["held_sec"] == 11520


def test_ac3_none_is_allowed_only_for_the_nullable_fields():
    encode_event(_order(tp=None, origin_code=None, state_mode=None, reason_code=None))
    encode_event(_close(pnl=None, pnl_r=None, held_sec=None, origin_code=None, state_mode=None))
    for name in ("state", "pair", "side", "entry_type", "qty", "price", "sl", "mode"):
        with pytest.raises(PayloadError):
            encode_event(_order(**{name: None}))
    for name in ("pair", "side", "qty", "entry_price", "close_price", "ccy", "reason_code", "mode"):
        with pytest.raises(PayloadError):
            encode_event(_close(**{name: None}))


@pytest.mark.parametrize("bad", ["0001.20", "0.10000000", "+1.5", "1.50", "1e999", "nan",
                                 "inf", "-1", "0", "0.0", "1.", ".5", " 1", "1 ", "1_0",
                                 "１２", "1234567890123", "1.123456789", "", "abc"])
def test_ac3_ac21_non_canonical_or_out_of_range_prices_reject_the_whole_event(bad):
    for name in ("qty", "price", "sl", "tp"):
        with pytest.raises(PayloadError):
            encode_event(_order(**{name: bad}))
    for name in ("qty", "entry_price", "close_price"):
        with pytest.raises(PayloadError):
            encode_event(_close(**{name: bad}))


def test_ac3_price_accepts_the_producer_format_boundaries():
    for good in ("150", "0.1", "0.01", "0.00000001", "999999999999", "150.12345678", "1.12345"):
        encode_event(_order(price=good))


@pytest.mark.parametrize("bad", ["abc", "1e999", "nan", "inf", "-0.00", "+1.50", "1.5", "1.500",
                                 "01.50", "1000000000000000.00", "-1000000000000000.00", "7,770.00",
                                 "7770", 7770, 7770.0, True])
def test_ac3_pnl_that_is_not_the_two_decimal_canonical_form_is_rejected(bad):
    with pytest.raises(PayloadError):
        encode_event(_close(pnl=bad))


@pytest.mark.parametrize("good", ["0.00", "-0.01", "7770.00", "-999999999999999.99"])
def test_ac3_pnl_two_decimal_canonical_values_pass(good):
    assert encode_event(_close(pnl=good))["pnl"] == good


@pytest.mark.parametrize("bad", ["1e999", "nan", "-0.00", "1000000.00", "-1000000.00", "2.4",
                                 "02.40", "+2.40", 2.4])
def test_ac3_pnl_r_rejects_non_canonical_or_out_of_range(bad):
    with pytest.raises(PayloadError):
        encode_event(_close(pnl_r=bad))


@pytest.mark.parametrize("bad", [-1, 10 ** 10, 1.5, "10", True])
def test_ac3_held_sec_must_be_a_non_negative_bounded_int(bad):
    with pytest.raises(PayloadError):
        encode_event(_close(held_sec=bad))


def test_ac3_a_float_value_is_rejected_even_when_it_looks_right():
    with pytest.raises(PayloadError):
        encode_event(_order(price=150.123))
    with pytest.raises(PayloadError):
        encode_event(_close(pnl=7770.0))


def test_ac21_a_value_over_64_chars_is_rejected():
    with pytest.raises(PayloadError):
        encode_event(_order(price="1" * 65))
    with pytest.raises(PayloadError):
        encode_event(_order(origin_code="signal:" + "a" * 64))
    with pytest.raises(PayloadError):
        encode_event(_order(reason_code="a" * 33))


@pytest.mark.parametrize("origin", ["@everyone", "<@123>", "signal:RSI", "signal:", "cron ",
                                    "signal:rsi\nx", "cron\n", "signal:a b", "**bold**",
                                    "Cron", "signal:1abc", "https://example.test"])
def test_ac22_origin_code_outside_the_grammar_is_rejected(origin):
    with pytest.raises(PayloadError):
        encode_event(_order(origin_code=origin))
    with pytest.raises(PayloadError):
        encode_event(_close(origin_code=origin))


def test_ac22_origin_code_with_cron_and_a_plugin_name_pass():
    encode_event(_order(origin_code="cron"))
    encode_event(_order(origin_code="signal:" + "a" * 57))


@pytest.mark.parametrize("name,value", [
    ("state", "pending"), ("side", "buy"), ("entry_type", "stop"), ("mode", "demo"),
    ("state_mode", "paused"), ("pair", "usd/jpy"), ("pair", "usdjpy"), ("pair", "@everyone"),
    ("reason_code", "Llm Cancel"), ("reason_code", "<@1>")])
def test_ac21_values_outside_the_enumerations_or_safe_forms_are_rejected(name, value):
    with pytest.raises(PayloadError):
        encode_event(_order(**{name: value}))


def test_ac21_an_unknown_reason_code_in_the_safe_form_is_carried_through():
    assert encode_event(_order(reason_code="brand_new"))["reason_code"] == "brand_new"
    assert encode_event(_close(reason_code="brand_new"))["reason_code"] == "brand_new"


def test_ac21_ccy_must_be_three_upper_case_letters():
    for bad in ("jpy", "JP", "JPYY", "J1Y", ""):
        with pytest.raises(PayloadError):
            encode_event(_close(ccy=bad))


def test_ac21_missing_extra_or_renamed_fields_reject_the_whole_event():
    event = _order()
    del event["fields"]["tp"]
    with pytest.raises(PayloadError):
        encode_event(event)
    event = _order()
    event["fields"]["reasoning"] = "free text"
    with pytest.raises(PayloadError):
        encode_event(event)
    event = _close()
    event["fields"]["strategy_code"] = event["fields"].pop("origin_code")
    with pytest.raises(PayloadError):
        encode_event(event)


@pytest.mark.parametrize("ref,ok", [
    ("order:1", True), ("order:12", True), ("order:9223372036854775807", True),
    ("order:0", False), ("order:00", False), ("order:9223372036854775808", False),
    ("order:99999999999999999999", False), ("order:", False), ("order:-1", False),
    ("order:1a", False), ("approval:1", False), ("order:１", False), ("order:1\n", False)])
def test_ac1_ref_is_an_order_id_in_1_to_2_63_minus_1(ref, ok):
    for event in (_order(), _close()):
        event = copy.deepcopy(event)
        event["ref"] = ref
        if ok:
            assert encode_event(event)["ref"] == ref
        else:
            with pytest.raises(PayloadError):
                encode_event(event)


def _producer_price(x: float) -> str:
    text = f"{x:.8f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


@pytest.mark.parametrize("x,expected", [
    (150.123, "150.123"), (150.1234, "150.1234"), (1.12345, "1.12345"), (1.123456, "1.123456"),
    (0.01, "0.01"), (0.1, "0.1"), (150.0, "150"), (0.00000001, "0.00000001"),
    (149.805, "149.805"), (0.57, "0.57"), (1.00005, "1.00005")])
def test_ac3_eight_decimals_cover_fx_precision_and_round_trip(x, expected):
    formatted = _producer_price(x)
    assert formatted == expected
    assert encode_event(_order(price=formatted))["price"] == expected
    assert Decimal(formatted) == Decimal(expected)


def test_ac3_a_value_that_formats_to_zero_is_not_accepted():
    assert _producer_price(4e-9) == "0"
    with pytest.raises(PayloadError):
        encode_event(_order(price="0"))
