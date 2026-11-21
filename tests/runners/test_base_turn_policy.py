import pytest

from agentic_fx.runners.base import Mission, MissionResult


def _kw(**over):
    d = dict(prompt="p", tools=[], output_schema={}, max_turns=3, timeout_sec=1)
    d.update(over)
    return d


def test_policy_defaults_to_none_and_summary_to_none():
    assert Mission(**_kw()).turn_policy is None
    assert MissionResult("completed", None).turn_summary is None


@pytest.mark.parametrize("policy", ["signal", "cron"])
def test_known_policies_are_accepted(policy):
    assert Mission(**_kw(turn_policy=policy)).turn_policy == policy


@pytest.mark.parametrize("policy", ["other", "", 1, True, ["signal"]])
def test_unknown_policy_is_rejected_when_the_mission_is_built(policy):
    with pytest.raises(ValueError):
        Mission(**_kw(turn_policy=policy))
