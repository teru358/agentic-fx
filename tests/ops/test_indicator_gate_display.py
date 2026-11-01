"""承認詳細の indicator gate 警告の ops 面: DTO が payload にあるキーだけ読める分に絞って
載せ、描画済みの行 (`gate_lines`) を運ぶ。client は CLI と同じ文を出す。壊れた値は
`gate_unreadable` で不在と区別し、例外にしない。"""
from __future__ import annotations

import json

import pytest

from agentic_fx import improve_notice_codes as codes
from agentic_fx.loops import approval_facts
from agentic_fx.ops import client as ops_client
from agentic_fx.ops.contracts import Principal
from tests.test_indicator_gate_display import EXPECTED, FULL, _payload, _show


def test_dto_carries_the_validated_keys_next_to_baseline(ops_env):
    approval_id = ops_env.raw_approval(payload=_payload(**FULL))
    detail = ops_env.service().approval_detail(Principal.OPERATOR, approval_id)
    assert detail["gate_warnings"] == FULL["gate_warnings"]
    assert detail["similar_to"] == FULL["similar_to"]
    assert detail["scale_columns"] == FULL["scale_columns"]


def test_dto_marks_a_broken_value_unreadable_and_keeps_the_readable_rows(ops_env):
    approval_id = ops_env.raw_approval(payload=_payload(
        gate_warnings=["bogus", "sparse_output"],
        similar_to=[{"name": "ema", "corr": 0.9}, {"name": "ema", "column": "v",
                                                  "kind": "similar"}],
        scale_columns="x"))
    detail = ops_env.service().approval_detail(Principal.OPERATOR, approval_id)
    assert detail["gate_warnings"] == ["sparse_output"]
    assert detail["similar_to"] == [{"name": "ema", "column": "v", "kind": "similar"}]
    assert detail["scale_columns"] == []
    assert detail["gate_unreadable"] is True


def test_dto_has_no_gate_key_when_the_payload_has_none(ops_env):
    legacy = ops_env.raw_approval(payload=_payload())
    detail = ops_env.service().approval_detail(Principal.OPERATOR, legacy)
    assert not {"gate_warnings", "similar_to", "scale_columns", "gate_lines",
                "gate_unreadable"} & set(detail)


def test_dto_has_no_gate_key_for_a_strategy_approval(ops_env):
    approval_id = ops_env.raw_approval(payload=_payload(kind="strategy"))
    detail = ops_env.service().approval_detail(Principal.OPERATOR, approval_id)
    assert not {"gate_warnings", "similar_to", "scale_columns", "gate_lines",
                "gate_unreadable"} & set(detail)


def test_dto_gate_lines_are_the_lines_the_cli_prints(tmp_path, ops_env):
    approval_id = ops_env.raw_approval(payload=_payload(**FULL))
    detail = ops_env.service().approval_detail(Principal.OPERATOR, approval_id)
    assert detail["gate_lines"] == EXPECTED
    assert detail["gate_lines"] == approval_facts.gate_lines(_payload(**FULL))
    cli_lines = _show(tmp_path, _payload(**FULL)).split("\n")
    first = cli_lines.index(EXPECTED[0])
    assert cli_lines[first:first + len(EXPECTED)] == detail["gate_lines"]


def test_dto_gate_lines_show_the_unreadable_count(ops_env):
    approval_id = ops_env.raw_approval(payload=_payload(gate_warnings=["bogus"]))
    detail = ops_env.service().approval_detail(Principal.OPERATOR, approval_id)
    assert detail["gate_lines"] == ["判読できない警告 1 件"]


def test_dto_never_carries_a_number_from_a_row(ops_env):
    approval_id = ops_env.raw_approval(payload=_payload(
        similar_to=[{"name": "ema", "column": "value", "kind": "similar",
                     "corr": 0.987}]))
    detail = ops_env.service().approval_detail(Principal.OPERATOR, approval_id)
    assert "0.987" not in json.dumps(detail)


def test_ops_client_prints_the_same_lines_as_the_cli(tmp_path, ops_env):
    approval_id = ops_env.raw_approval(payload=_payload(**FULL))
    detail = ops_env.service().approval_detail(Principal.OPERATOR, approval_id)
    client_lines = ops_client.format_approval_detail(detail).split("\n")
    cli_lines = _show(tmp_path, _payload(**FULL)).split("\n")
    shown = [line for line in client_lines if line.startswith("警告: ")]
    assert shown == EXPECTED
    assert [line for line in cli_lines if line.startswith("警告: ")] == shown


@pytest.mark.parametrize("data", [
    {"gate_warnings": "x", "similar_to": 3}, {"gate_warnings": ["bogus"]},
    {"similar_to": [{"corr": 1}]}, {}])
def test_ops_client_survives_a_broken_or_absent_dto(data):
    out = ops_client.format_approval_detail({"id": 1, "kind": "plugin", **data})
    assert "警告: 配備済み" not in out


def test_ops_client_prints_the_dto_lines_as_they_are():
    out = ops_client.format_approval_detail(
        {"id": 1, "kind": "plugin", "gate_lines": ["判読できない警告 2 件", 5]})
    assert "判読できない警告 2 件" in out.split("\n")


def test_the_daemon_event_schema_has_no_room_for_a_gate_field():
    from agentic_fx.ops import events
    for code, fields in events.FIELDS.items():
        assert not {"gate_warnings", "similar_to", "scale_columns"} & set(fields), code

