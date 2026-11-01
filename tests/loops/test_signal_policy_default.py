from __future__ import annotations

import json

import jsonschema
import pytest

from agentic_fx.loops import prompts_loader
from agentic_fx.loops.prompts_loader import load_prompt
from agentic_fx.loops.signal_decision_contract import DECISION_SCHEMA, Accept, Reject, parse_decision, verify_opposite_position
from agentic_fx.policy_projection import _render_directive


def test_signal_policy_default_renders_as_one_bounded_managed_policy_line():
    text = load_prompt("signal_policy_default")
    rendered = _render_directive(text)

    assert "\n" not in rendered
    assert len(rendered) <= 4000
    assert "方向と自分のテクニカル判断の一致" not in rendered
    assert "accept" in rendered
    assert "news_shock" in rendered
    assert "opposite_position" in rendered


J3_TEXT = (
    "strategy signal の採否 mission では採用 (accept) を既定とする。却下 (reject) は "
    "news_shock (重大ニュースが signal の前提を壊した) か opposite_position "
    "(同じ pair に逆方向の建玉・約定待ち注文がある) に当たるときだけにし、"
    "自分のテクニカル判断と方向が合わないことを却下の理由にしない。")


def test_signal_policy_default_equals_the_approved_j3_text_exactly():
    assert load_prompt("signal_policy_default").strip() == J3_TEXT
    assert _render_directive(load_prompt("signal_policy_default")) == "- " + J3_TEXT


def _contract_snapshot():
    payloads = [
        {"decision": "accept", "reject_code": None},
        {"decision": "reject", "reject_code": "news_shock"},
        {"decision": "reject", "reject_code": "opposite_position"},
        {"decision": "reject", "reject_code": None},
        {"decision": "hold", "reject_code": None},
    ]
    orders = [{"pair": "USDJPY", "direction": "short", "status": "open"}]
    results = [parse_decision(p) for p in payloads]
    verified = [verify_opposite_position(r, orders, pair="USDJPY", direction="long")
                for r in results if isinstance(r, Reject)]
    return json.dumps(DECISION_SCHEMA, sort_keys=True), results, verified


def test_policy_default_does_not_change_the_signal_decision_contract(monkeypatch, tmp_path):
    before = _contract_snapshot()
    load_prompt("signal_policy_default")
    assert _contract_snapshot() == before

    monkeypatch.setattr(prompts_loader, "_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        load_prompt("signal_policy_default")
    assert _contract_snapshot() == before
    jsonschema.validate({"decision": "accept", "reject_code": None}, DECISION_SCHEMA)
    assert isinstance(parse_decision({"decision": "accept", "reject_code": None}), Accept)
