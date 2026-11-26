import json

import jsonschema
import pytest

from agentic_fx.loops.signal_decision_contract import DECISION_SCHEMA
from agentic_fx.loops.summary import trade_intent_schema
from agentic_fx.runners.base import Mission
from agentic_fx.runners.local_runner import LocalRunner
from agentic_fx.runners.response_parser import parse_json_output
from tests.runners import turn_budget_support as tb

NAMES = [tb.SIGNAL_REPEAT, tb.SIGNAL_DISTINCT, tb.CRON_LOOP]


@pytest.mark.parametrize("name,total,assistants", [
    (tb.SIGNAL_REPEAT, 34, 16), (tb.SIGNAL_DISTINCT, 34, 16),
    (tb.CRON_LOOP, 38, 16)])
def test_transcript_fixture_keeps_real_message_shape(name, total, assistants):
    msgs = tb.load_messages(name)
    assert len(msgs) == total
    assert msgs[0]["role"] == "user"
    assert sum(1 for m in msgs if m["role"] == "assistant") == assistants
    issued: list[str] = []
    for m in msgs[1:]:
        if m["role"] == "assistant":
            assert m["content"] == ""
            for tc in m["tool_calls"]:
                assert isinstance(tc["id"], str) and len(tc["id"]) == 32
                assert tc["type"] == "function"
                assert isinstance(tc["function"]["arguments"], str)
                json.loads(tc["function"]["arguments"])
                issued.append(tc["id"])
        else:
            assert m["role"] == "tool"
            assert m["tool_call_id"] in issued
            assert isinstance(m["content"], str)
            json.loads(m["content"])


def test_first_signal_turn_has_two_calls_then_single_searches():
    msgs = tb.assistant_messages(tb.SIGNAL_REPEAT)
    assert [tc["function"]["name"] for tc in msgs[0]["tool_calls"]] == [
        "search_news", "get_positions"]
    for m in msgs[1:]:
        assert [tc["function"]["name"] for tc in m["tool_calls"]] == ["search_news"]


def test_repeat_fixture_repeats_queries_and_distinct_fixture_does_not():
    def queries(name):
        return [json.loads(tc["function"]["arguments"])["query"]
                for m in tb.assistant_messages(name)
                for tc in m["tool_calls"] if tc["function"]["name"] == "search_news"]
    repeat, distinct = queries(tb.SIGNAL_REPEAT), queries(tb.SIGNAL_DISTINCT)
    assert len(set(repeat)) < len(repeat)
    assert len(set(distinct)) == len(distinct) == 16


def test_cron_fixture_repeats_get_positions_with_empty_arguments():
    calls = [tc["function"] for m in tb.assistant_messages(tb.CRON_LOOP)
             for tc in m["tool_calls"] if tc["function"]["name"] == "get_positions"]
    assert len(calls) >= 2
    assert all(json.loads(c["arguments"]) == {} for c in calls)


@pytest.mark.parametrize("name", NAMES)
def test_fixture_plugin_keys_are_synthetic(name):
    keys = [k for m in tb.load_messages(name) if m["role"] == "tool"
            for k in _keys(json.loads(m["content"]))]
    plugin_keys = [k for k in keys if k.startswith("plugin:")]
    assert all(k.startswith("plugin:synthetic_") for k in plugin_keys)


def _keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from _keys(v)


def test_replay_drives_the_runner_through_a_real_shaped_transcript():
    replay = tb.Replay.from_fixture(tb.SIGNAL_REPEAT)
    tools = tb.StubTools(["search_news", "get_positions"])
    runner = LocalRunner(base_url="http://test/v1", model="m",
                         registry=tools.registry, transport=replay.transport())
    result = runner.run(Mission(
        prompt="p", tools=["search_news", "get_positions"],
        output_schema=DECISION_SCHEMA, max_turns=16, timeout_sec=30))
    assert result.status == "max_turns"
    assert len(replay.bodies) == 16
    assert tools.count("search_news") == 16
    assert tools.count("get_positions") == 1
    last = replay.bodies[-1]["messages"]
    assert last[-1]["role"] == "tool"


def test_replay_can_swap_the_tail_for_a_final_answer():
    final = tb.load_final_responses()["measured"]["signal_switch_answer"]
    replay = tb.Replay.from_fixture(tb.SIGNAL_DISTINCT, keep=3, then=[final])
    tools = tb.StubTools(["search_news", "get_positions"])
    runner = LocalRunner(base_url="http://test/v1", model="m",
                         registry=tools.registry, transport=replay.transport())
    result = runner.run(Mission(
        prompt="p", tools=["search_news", "get_positions"],
        output_schema=DECISION_SCHEMA, max_turns=16, timeout_sec=30))
    assert result.status == "completed"
    assert len(replay.bodies) == 4


def test_replay_fails_loudly_when_the_script_runs_out():
    replay = tb.Replay([{"role": "assistant", "content": "x"}])
    tools = tb.StubTools(["search_news"])
    runner = LocalRunner(base_url="http://test/v1", model="m",
                         registry=tools.registry, transport=replay.transport())
    with pytest.raises(AssertionError):
        runner.run(Mission(prompt="p", tools=["search_news"],
                           output_schema={"type": "object"}, max_turns=3,
                           timeout_sec=30))


def test_cron_fixture_replays_with_every_recorded_tool_name():
    names = tb.tool_names(tb.CRON_LOOP)
    assert "get_positions" in names and "search_news" in names
    replay = tb.Replay.from_fixture(tb.CRON_LOOP)
    tools = tb.StubTools(names)
    runner = LocalRunner(base_url="http://test/v1", model="m",
                         registry=tools.registry, transport=replay.transport())
    result = runner.run(Mission(prompt="p", tools=names,
                                output_schema={"type": "object"},
                                max_turns=16, timeout_sec=30))
    assert result.status == "max_turns"
    assert tools.count("get_positions") >= 2


def test_measured_final_answers_match_the_output_schemas():
    measured = tb.load_final_responses()["measured"]
    for key, msg in measured.items():
        output = parse_json_output(msg["content"])
        schema = (trade_intent_schema(["USDJPY"]) if key.startswith("cron")
                  else DECISION_SCHEMA)
        jsonschema.validate(output, schema)
        assert "tool_calls" not in msg


def test_synthetic_responses_cover_structured_and_text_tool_calls():
    synthetic = tb.load_final_responses()["synthetic"]
    assert synthetic["structured_tool_call_response"]["tool_calls"][0][
        "function"]["name"] == "search_news"
    assert len(synthetic["structured_two_calls_response"]["tool_calls"]) == 2
    for key in ("text_tool_call_json_response",
                "text_tool_call_with_decision_inside",
                "text_tool_call_function_form_response"):
        assert "<tool_call>" in synthetic[key]["content"]
        assert "tool_calls" not in synthetic[key]
