"""改善 event の allowlist と値の文法。

文法だけを見る: 語は列挙で照合しないので、daemon が新しい reason_code を足しても止まらない。
文法に反する値・field の過不足・不正な ref は event 全体を拒否する。
"""
from __future__ import annotations

import pytest

from afx_ops.notify.payload import ALLOWED_FIELDS, PayloadError, encode_event

TS = "2026-10-25T00:00:00+00:00"
FIELDS = {"state": "finished", "trigger": "scheduled", "result_code": "gate_failed",
          "reason_code": "unprofitable", "tool_calls": 10}


def _event(ref: str = "improve:2043", **fields) -> dict:
    return {"id": 7, "ts": TS, "code": "improve_run_changed", "ref": ref,
            "fields": {**FIELDS, **fields}}


def test_the_allowlist_has_the_improve_code_with_its_exact_field_set():
    assert ALLOWED_FIELDS["improve_run_changed"] == frozenset(
        {"state", "trigger", "result_code", "reason_code", "tool_calls"})


def test_a_finished_event_is_encoded_flat_with_the_fixed_fields():
    assert encode_event(_event()) == {"event_id": 7, "ts": TS, "code": "improve_run_changed",
                                      "ref": "improve:2043", **FIELDS}


def test_a_started_event_with_null_outcome_fields_is_accepted():
    encode_event(_event(state="started", trigger="manual", result_code=None, reason_code=None,
                        tool_calls=None))


@pytest.mark.parametrize("result", ["submitted", "observation", "report", "gate_failed",
                                    "failed", None])
def test_every_result_code_and_null_is_accepted(result):
    encode_event(_event(result_code=result))


@pytest.mark.parametrize("word", ["zzz_new_word", "a", "not_submitted", "a" * 32, "a_1"])
def test_an_unknown_reason_word_of_the_right_grammar_is_accepted(word):
    assert encode_event(_event(reason_code=word))["reason_code"] == word


@pytest.mark.parametrize("bad", ["pf:7.7777", "Bad", "a" * 33, "1abc", "", "a b", "a-b", "a.b",
                                 "a:1", "_a", 7, True, ["a"], "é", "a\n"])
def test_a_reason_outside_the_grammar_is_rejected(bad):
    with pytest.raises(PayloadError):
        encode_event(_event(reason_code=bad))


@pytest.mark.parametrize("field,bad", [
    ("state", "done"), ("state", None), ("state", "Started"), ("state", 1),
    ("trigger", "auto"), ("trigger", None), ("trigger", "Manual"), ("trigger", 0),
    ("result_code", "unknown"), ("result_code", "Failed"), ("result_code", 3),
    ("result_code", ""),
])
def test_an_unknown_state_trigger_or_result_code_is_rejected(field, bad):
    with pytest.raises(PayloadError):
        encode_event(_event(**{field: bad}))


@pytest.mark.parametrize("bad", [True, False, -1, 1_000_001, 1.5, 3.0, "3", [3]])
def test_tool_calls_must_be_an_integer_in_range_or_null(bad):
    with pytest.raises(PayloadError):
        encode_event(_event(tool_calls=bad))


@pytest.mark.parametrize("good", [0, 1, 1_000_000, None])
def test_tool_calls_boundaries_are_accepted(good):
    assert encode_event(_event(tool_calls=good))["tool_calls"] == good


@pytest.mark.parametrize("ref", ["improve:", "improve:0x", "improve:1234567890123456789",
                                 "order:1", "improve:-1", "improve:1\n", "improve", "instance",
                                 "improve:１"])
def test_a_ref_outside_the_improve_form_is_rejected(ref):
    with pytest.raises(PayloadError):
        encode_event(_event(ref))


@pytest.mark.parametrize("ref", ["improve:0", "improve:2043", "improve:" + "9" * 18])
def test_a_ref_of_up_to_eighteen_digits_is_accepted(ref):
    assert encode_event(_event(ref))["ref"] == ref


def test_a_missing_or_extra_field_is_rejected():
    for name in FIELDS:
        event = _event()
        del event["fields"][name]
        with pytest.raises(PayloadError):
            encode_event(event)
    event = _event()
    event["fields"]["extra"] = "x"
    with pytest.raises(PayloadError):
        encode_event(event)


def test_other_codes_keep_their_behaviour():
    health = {"id": 3, "ts": TS, "code": "service_health_changed", "ref": "instance",
              "fields": {"component_code": "api", "health_code": "ok"}}
    assert encode_event(health)["health_code"] == "ok"
    with pytest.raises(PayloadError):
        encode_event({**health, "fields": {"component_code": "api", "health_code": "o k"}})
