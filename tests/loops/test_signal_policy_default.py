from __future__ import annotations

import jsonschema

from agentic_fx.loops.prompts_loader import load_prompt
from agentic_fx.loops.signal_decision_contract import DECISION_SCHEMA, Accept, parse_decision
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


def test_policy_default_does_not_change_the_signal_decision_contract():
    load_prompt("signal_policy_default")
    payload = {"decision": "accept", "reject_code": None}

    jsonschema.validate(payload, DECISION_SCHEMA)
    assert isinstance(parse_decision(payload), Accept)
