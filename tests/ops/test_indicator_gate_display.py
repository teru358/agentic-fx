"""承認詳細の indicator gate 警告の ops 面: DTO が検証済みの 3 キーを `baseline` の隣に
載せ、client が CLI と同じ文を出す。壊れた値は null / 行なしで例外にしない。"""
from __future__ import annotations

import json

import pytest

from agentic_fx import improve_notice_codes as codes
from agentic_fx.ops import client as ops_client
from agentic_fx.ops.contracts import Principal
from tests.test_indicator_gate_display import EXPECTED, FULL, _payload, _show


def test_dto_carries_the_validated_keys_next_to_baseline(ops_env):
    approval_id = ops_env.raw_approval(payload=_payload(**FULL))
    detail = ops_env.service().approval_detail(Principal.OPERATOR, approval_id)
    assert detail["gate_warnings"] == FULL["gate_warnings"]
    assert detail["similar_to"] == FULL["similar_to"]
    assert detail["scale_columns"] == FULL["scale_columns"]


def test_dto_leaves_a_broken_section_null_and_a_legacy_payload_null(ops_env):
    bad = ops_env.raw_approval(payload=_payload(
        gate_warnings=["bogus"], similar_to=[{"name": "ema", "corr": 0.9}],
        scale_columns="x"))
    legacy = ops_env.raw_approval(payload=_payload())
    for approval_id in (bad, legacy):
        detail = ops_env.service().approval_detail(Principal.OPERATOR, approval_id)
        assert detail["gate_warnings"] is None
        assert detail["similar_to"] is None
        assert detail["scale_columns"] is None


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
    assert "警告" not in out


def test_the_daemon_event_schema_has_no_room_for_a_gate_field():
    from agentic_fx.ops import events
    for code, fields in events.FIELDS.items():
        assert not {"gate_warnings", "similar_to", "scale_columns"} & set(fields), code

