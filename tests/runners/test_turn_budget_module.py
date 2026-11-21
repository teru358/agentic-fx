import pytest

from agentic_fx.runners import turn_budget as tbm


def test_canonical_key_ignores_object_key_order_and_spacing_only():
    key = tbm.canonical_key
    assert key("t", {"a": 1, "b": 2}) == key("t", {"b": 2, "a": 1})
    assert key("t", {"a": [1, 2]}) != key("t", {"a": [2, 1]})
    assert key("t", {"a": "x"}) != key("t", {"a": "X"})
    assert key("t", {"a": "x y"}) != key("t", {"a": "x  y"})
    assert key("t", {"a": 1}) != key("t", {"a": "1"})
    assert key("t", {"a": 1}) != key("u", {"a": 1})
    assert key("t", {"a": "é"}) != key("t", {"a": "e"})


def test_final_stage_is_the_last_two_turns():
    guard = tbm.TurnGuard("signal", 16)
    states = []
    for turn in range(1, 17):
        guard.begin_request(turn)
        states.append(guard.final_stage)
    assert states == [False] * 14 + [True, True]
    assert guard.requests == 16


def test_notice_mentions_the_budget_only_for_signal():
    assert "5 回" in tbm.turn_notice("signal", 16)
    assert "5 回" not in tbm.turn_notice("cron", 16)
    assert "総上限は 16 ターン" in tbm.turn_notice("cron", 16)
    assert "調査は 14 ターンまで" in tbm.turn_notice("cron", 16)


@pytest.mark.parametrize("text,expected", [
    ("<tool_call>x</tool_call>", True),
    ("先に<function=a>", True),
    ('{"decision": "accept", "reject_code": null}', False),
    ("普通の文章", False)])
def test_text_tool_call_detection(text, expected):
    assert tbm.is_text_tool_call(text) is expected


def _summary(**over):
    base = TurnGuardSummary.make()
    base.update(over)
    return base


class TurnGuardSummary:
    @staticmethod
    def make():
        return tbm.TurnGuard("signal", 6).summary("completed", None)


def test_summary_round_trips_through_the_validator():
    summary = _summary(reason="search_unavailable", terminal="failed")
    assert tbm.parse_turn_summary(summary) == summary


@pytest.mark.parametrize("mutate", [
    lambda s: s.pop("requests"),
    lambda s: s.update(extra=1),
    lambda s: s.update(requests=True),
    lambda s: s.update(requests=-1),
    lambda s: s.update(final_stage=1),
    lambda s: s.update(terminal="weird"),
    lambda s: s.update(reason="x" * 500),
    lambda s: s.update(reason=5),
])
def test_validator_rejects_malformed_summaries(mutate):
    summary = _summary()
    mutate(summary)
    assert tbm.parse_turn_summary(summary) is None


def test_validator_rejects_non_dict():
    assert tbm.parse_turn_summary([1]) is None
    assert tbm.parse_turn_summary(None) is None


def test_format_has_no_free_text_and_marks_missing():
    line = tbm.format_turn_summary(_summary())
    assert "\n" not in line and "requests=0" in line and "terminal=completed" in line
    assert tbm.format_turn_summary(tbm.MISSING_SUMMARY) == "missing=1"
