"""改善 event (`improve_run_changed`) の field 集合と値の文法。

検査は文法だけで、語の列挙では照合しない: daemon が新しい reason_code を足しても、受け側は
止まらずに表示側の汎用の描画へ倒れる。組合せの厳格な検査 (どの result_code にどの reason_code
が許されるか、started の値が null か) は daemon が持つ。
"""
from __future__ import annotations

import re

CODE = "improve_run_changed"
FIELDS = frozenset({"state", "trigger", "result_code", "reason_code", "tool_calls",
                    "repeat_run", "repeat_max_runs", "repeat_elapsed_sec",
                    "repeat_duration_sec"})
_REPEAT_KEYS = ("repeat_run", "repeat_max_runs", "repeat_elapsed_sec", "repeat_duration_sec")
_REPEAT_FIELDS = frozenset(_REPEAT_KEYS)
REF_PATTERN = re.compile(r"improve:[0-9]{1,18}\Z")

STATES = frozenset({"started", "finished"})
TRIGGERS = frozenset({"manual", "scheduled"})
RESULT_CODES = frozenset({"submitted", "observation", "report", "gate_failed", "failed"})
MAX_TOOL_CALLS = 1_000_000

_REASON = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


def valid_result_code(value: object) -> bool:
    return value is None or (type(value) is str and value in RESULT_CODES)


def valid_reason_code(value: object) -> bool:
    return value is None or (type(value) is str and _REASON.fullmatch(value) is not None)


def valid_tool_calls(value: object) -> bool:
    return value is None or (type(value) is int and 0 <= value <= MAX_TOOL_CALLS)


def valid_fields(fields: object) -> bool:
    """field 集合の完全一致と、各値の文法・範囲を満たすか。"""
    if type(fields) is not dict or not FIELDS - _REPEAT_FIELDS <= set(fields) <= FIELDS:
        return False
    repeat_values = tuple(fields.get(key) for key in _REPEAT_KEYS)
    if any(value is not None and (type(value) is not int or value < 0)
           for value in repeat_values):
        return False
    if any(value is None for value in repeat_values) and any(value is not None for value in repeat_values):
        return False
    if (repeat_values[0] is not None and (repeat_values[0] < 1
                                           or repeat_values[1] < repeat_values[0]
                                           or repeat_values[3] < 60)):
        return False
    return (type(fields["state"]) is str and fields["state"] in STATES
            and type(fields["trigger"]) is str and fields["trigger"] in TRIGGERS
            and valid_result_code(fields["result_code"])
            and valid_reason_code(fields["reason_code"])
            and valid_tool_calls(fields["tool_calls"]))
