import json
import threading

import httpx
import pytest

from agentic_fx.loops.signal_decision_contract import DECISION_SCHEMA
from agentic_fx.runners import turn_budget as tbm
from agentic_fx.runners.base import Mission
from agentic_fx.runners.local_runner import LocalRunner
from tests.runners import turn_budget_support as tb
from tests.runners.turn_budget_support import (
    ACCEPT, HOLD, REJECT, call_message, search_call, text_message)

SIGNAL_TOOLS = ["search_news", "get_positions"]


def _mission(policy="signal", max_turns=6, schema=None, tools=None, **over):
    d = dict(prompt="判断せよ", tools=tools or SIGNAL_TOOLS,
             output_schema=schema or (DECISION_SCHEMA if policy != "cron"
                                      else tb.CRON_SCHEMA),
             max_turns=max_turns, timeout_sec=30, turn_policy=policy)
    d.update(over)
    return Mission(**d)


def _run(script, mission=None, *, names=None, results=None, fail=None, **kw):
    replay = tb.Replay(script)
    tools = tb.StubTools(names or SIGNAL_TOOLS, results=results, fail=fail)
    runner = LocalRunner(base_url="http://test/v1", model="m",
                         registry=tools.registry, transport=replay.transport(),
                         **kw)
    result = runner.run(mission or _mission())
    return result, replay, tools


def _users(body):
    return [m["content"] for m in body["messages"] if m["role"] == "user"]


# ---- 無効のとき (policy None) は従来どおり ---------------------------------


def test_policy_none_keeps_request_body_and_prompt():
    result, replay, _ = _run([ACCEPT], _mission(policy=None))
    body = replay.bodies[0]
    assert body["tool_choice"] == "auto"
    assert [t["function"]["name"] for t in body["tools"]] == SIGNAL_TOOLS
    assert body["messages"][0]["content"] == "判断せよ"
    assert result.turn_summary is None


@pytest.mark.parametrize("max_turns", [1, 2])
def test_policy_none_still_accepts_small_max_turns(max_turns):
    result, replay, _ = _run([ACCEPT], _mission(policy=None, max_turns=max_turns))
    assert result.status == "completed"
    assert replay.bodies[0]["tool_choice"] == "auto"


@pytest.mark.parametrize("max_turns", [1, 2])
def test_policy_requires_three_turns_before_any_request(max_turns):
    replay = tb.Replay([ACCEPT])
    tools = tb.StubTools(SIGNAL_TOOLS)
    runner = LocalRunner(base_url="http://test/v1", model="m",
                         registry=tools.registry, transport=replay.transport())
    with pytest.raises(ValueError):
        runner.run(_mission(max_turns=max_turns))
    assert replay.bodies == []


# ---- 要求の形: 通常ターンと最終段階 -----------------------------------------


def test_final_stage_requests_omit_tools_and_switch_once_with_schema():
    script = [search_call(1), search_call(2), search_call(3), search_call(4),
              tb.call_message(("idfinal", "search_news", {"query": "q5"})),
              ACCEPT]
    result, replay, tools = _run(script, _mission(max_turns=6))
    assert result.status == "completed"
    assert len(replay.bodies) == 6
    for body in replay.bodies[:4]:
        assert body["tool_choice"] == "auto" and body["tools"]
    for body in replay.bodies[4:]:
        assert "tools" not in body and "tool_choice" not in body
    # 開始時に上限の固定文が prompt の末尾に入る
    first = replay.bodies[0]["messages"][0]["content"]
    assert first.startswith("判断せよ")
    assert "総上限は 6 ターン" in first and "調査は 4 ターンまで" in first
    assert "検索の実行予算は 5 回" in first
    schema_json = json.dumps(DECISION_SCHEMA, ensure_ascii=False)
    # 切替文は 4 回目の応答の後 (request 5) に 1 回、request 6 は再掲
    assert sum(tbm.SWITCH_TEXT in u for u in _users(replay.bodies[3])) == 0
    assert sum(tbm.SWITCH_TEXT in u for u in _users(replay.bodies[4])) == 1
    # request 6 は切替文を繰り返さない (履歴の 1 件のみ)
    assert sum(tbm.SWITCH_TEXT in u for u in _users(replay.bodies[5])) == 1
    assert tbm.SWITCH_TEXT not in _users(replay.bodies[5])[-1]
    assert schema_json in _users(replay.bodies[4])[-1]
    for body in replay.bodies:
        tb.assert_no_consecutive_user_rows(body)
    assert tools.count("search_news") == 4


def test_cron_policy_notice_has_no_search_budget_sentence():
    _, replay, _ = _run([HOLD], _mission(policy="cron"))
    first = replay.bodies[0]["messages"][0]["content"]
    assert "総上限は 6 ターン" in first
    assert "検索の実行予算" not in first


def test_final_stage_answer_completes_without_running_a_tool():
    script = [search_call(1), search_call(2), search_call(3), search_call(4),
              ACCEPT]
    result, replay, tools = _run(script, _mission(max_turns=6))
    assert result.status == "completed"
    assert result.output == {"decision": "accept", "reject_code": None}
    assert len(replay.bodies) == 5
    assert result.turn_summary["decided_in_final"] is True
    assert result.turn_summary["final_stage_suppressed"] == 0


def test_answer_before_the_final_stage_is_not_marked_final():
    result, replay, _ = _run([search_call(1), ACCEPT], _mission(max_turns=6))
    assert result.status == "completed"
    assert result.turn_summary["final_stage"] is False
    assert result.turn_summary["decided_in_final"] is False
    assert all("tools" in b for b in replay.bodies)


# ---- 最終段階のツール呼出し (構造化) ----------------------------------------


def test_structured_call_in_final_stage_is_stopped_per_id_and_repair_is_merged():
    two = call_message(("idA", "search_news", {"query": "a"}),
                       ("idB", "get_positions", {}))
    result, replay, tools = _run(
        [search_call(1), two, ACCEPT], _mission(max_turns=3))
    assert result.status == "completed"
    # request 2 (最終段階の最初) の後ろに tool 2 件 + 統合した user 1 件
    last = replay.bodies[2]["messages"]
    assert [m["role"] for m in last[-4:]] == ["assistant", "tool", "tool", "user"]
    assert [m["tool_call_id"] for m in last[-3:-1]] == ["idA", "idB"]
    stops = [json.loads(m["content"]) for m in last[-3:-1]]
    assert all(s["error"] == "final_answer_only" for s in stops)
    assert tbm.FINAL_ONLY_TEXT in last[-1]["content"]
    assert tbm.SWITCH_TEXT not in last[-1]["content"]
    assert tools.count("search_news") == 1 and tools.count("get_positions") == 0
    tb.assert_no_consecutive_user_rows(replay.bodies[2])


def test_invalid_arguments_in_final_stage_get_the_validation_error_not_the_stop():
    bad = call_message(("idA", "search_news", '{"query": '),
                       ("idB", "search_news", {"query": 5}),
                       ("idC", "search_news", "[1]"))
    result, replay, tools = _run([search_call(1), bad, HOLD],
                                 _mission(max_turns=3, policy="cron",
                                          schema=tb.CRON_SCHEMA))
    messages = replay.bodies[2]["messages"]
    results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"][1:]
    assert len(results) == 3
    assert all("error" in r and r.get("error") != "final_answer_only"
               for r in results)
    assert "not valid JSON" in results[0]["error"]
    assert "invalid arguments" in results[1]["error"]
    assert tools.count("search_news") == 1


@pytest.mark.parametrize("entry", [
    "not-a-dict",
    {"type": "function", "function": {"name": "search_news", "arguments": "{}"}},
    {"id": 7, "type": "function", "function": {"name": "search_news"}},
    {"id": "x", "type": "function", "function": "oops"},
    {"id": "x", "type": "function", "function": {"name": 5, "arguments": "{}"}},
    {"id": "x", "type": "function",
     "function": {"name": "search_news", "arguments": 5}},
])
def test_shape_violation_in_final_stage_fails_without_running_any_call(entry):
    good = {"id": "ok", "type": "function",
            "function": {"name": "get_positions", "arguments": "{}"}}
    msg = {"role": "assistant", "content": "", "tool_calls": [good, entry]}
    result, replay, tools = _run([search_call(1), msg, ACCEPT],
                                 _mission(max_turns=3))
    assert result.status == "failed"
    assert result.output is None
    assert len(replay.bodies) == 2
    assert tools.count("get_positions") == 0


def test_too_many_calls_in_final_stage_fails_without_running_any():
    many = call_message(*[(f"i{n}", "get_positions", {}) for n in range(17)])
    result, replay, tools = _run([search_call(1), many], _mission(max_turns=3))
    assert result.status == "failed"
    assert tools.count("get_positions") == 0


def test_null_arguments_in_final_stage_are_treated_as_empty_object():
    msg = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "n1", "type": "function",
         "function": {"name": "get_positions", "arguments": None}}]}
    result, replay, tools = _run([search_call(1), msg, ACCEPT],
                                 _mission(max_turns=3))
    assert result.status == "completed"
    stop = json.loads(replay.bodies[2]["messages"][-2]["content"])
    assert stop["error"] == "final_answer_only"
    assert tools.count("get_positions") == 0


def test_json_alongside_structured_call_in_final_stage_is_not_adopted():
    msg = call_message(("idA", "get_positions", {}),
                       content='{"decision": "accept", "reject_code": null}')
    result, replay, tools = _run([search_call(1), msg, REJECT],
                                 _mission(max_turns=3))
    assert result.status == "completed"
    assert result.output["decision"] == "reject"


def test_structured_stop_counts_one_parse_repair_per_response():
    """最終段階の呼出し 1 応答は call 数によらず parse 修復 1 回。2 回の
    parse 不良の後なら 3 回目で failed。"""
    many = call_message(("a", "get_positions", {}), ("b", "get_positions", {}),
                        ("c", "get_positions", {}))
    result, _, _ = _run(
        [text_message("x"), text_message("y"), search_call(3), many],
        _mission(max_turns=5))
    assert result.status == "failed"
    one_call = call_message(("a", "get_positions", {}))
    result2, replay2, _ = _run(
        [text_message("x"), search_call(2), search_call(3), one_call, ACCEPT],
        _mission(max_turns=5))
    assert result2.status == "completed"
    assert len(replay2.bodies) == 5


# ---- 最終段階のツール呼出し (文字列) ----------------------------------------


@pytest.mark.parametrize("key", [
    "text_tool_call_json_response", "text_tool_call_with_decision_inside",
    "text_tool_call_function_form_response",
    "text_tool_call_function_form_after_prose_response"])
def test_text_tool_call_in_final_stage_is_not_run_or_adopted(key):
    text = tb.load_final_responses()["synthetic"][key]
    result, replay, tools = _run([search_call(1), text, REJECT],
                                 _mission(max_turns=3))
    assert result.status == "completed"
    assert result.output["decision"] == "reject"
    assert tools.count("search_news") == 1
    last = replay.bodies[2]["messages"]
    assert [m["role"] for m in last[-2:]] == ["assistant", "user"]
    assert tbm.FINAL_ONLY_TEXT in last[-1]["content"]
    assert "tool_call_id" not in json.dumps(last[-1])
    tb.assert_no_consecutive_user_rows(replay.bodies[2])


def test_text_tool_call_at_the_last_turn_ends_without_another_request():
    text = tb.load_final_responses()["synthetic"]["text_tool_call_json_response"]
    result, replay, _ = _run([search_call(1), text_message("x"), text],
                             _mission(max_turns=3, policy="cron"))
    assert result.status == "max_turns"
    assert result.output is None
    assert result.reason == tbm.REASON_FINAL_ANSWER_MISSING
    assert len(replay.bodies) == 3
    assert result.transcript[-1]["role"] == "assistant"


def test_third_text_tool_call_exceeds_the_parse_repair_limit():
    text = tb.load_final_responses()["synthetic"]["text_tool_call_json_response"]
    result, replay, _ = _run([text_message("x"), text_message("y"), text],
                             _mission(max_turns=3, policy="cron"))
    assert result.status == "failed"
    assert len(replay.bodies) == 3


# ---- 最終回の未回答・不正 JSON ----------------------------------------------


def test_unanswered_last_turn_is_max_turns_with_a_fixed_reason():
    result, replay, _ = _run([search_call(1), search_call(2), search_call(3)],
                             _mission(max_turns=3, policy="cron"))
    assert result.status == "max_turns"
    assert result.reason == tbm.REASON_FINAL_ANSWER_MISSING
    assert result.output is None
    assert len(replay.bodies) == 3


def test_invalid_json_on_the_last_turn_adds_no_request_or_message():
    result, replay, _ = _run([search_call(1), text_message("y"), text_message("z")],
                             _mission(max_turns=3, policy="cron"))
    assert result.status == "max_turns"
    assert result.reason == tbm.REASON_FINAL_ANSWER_MISSING
    assert len(replay.bodies) == 3
    assert result.transcript[-1]["role"] == "assistant"


def test_repair_exceeded_in_final_stage_is_failed_with_the_fixed_reason():
    result, _, _ = _run(
        [text_message("x"), text_message("y"), search_call(3), text_message("z")],
        _mission(max_turns=5, policy="cron"))
    assert result.status == "failed"
    assert result.reason == tbm.REASON_FINAL_ANSWER_MISSING


def test_repair_exceeded_before_the_final_stage_keeps_the_plain_failure():
    result, _, _ = _run(
        [text_message("x"), text_message("y"), text_message("z")],
        _mission(max_turns=6, policy="cron"))
    assert result.status == "failed"
    assert result.reason is None


def test_schema_repair_in_final_stage_is_kept_and_completes():
    bad = text_message('{"decision": "maybe", "reject_code": null}')
    result, replay, _ = _run([search_call(1), bad, ACCEPT], _mission(max_turns=3))
    assert result.status == "completed"
    user = _users(replay.bodies[2])[-1]
    assert "スキーマに合いません" in user and tbm.SWITCH_TEXT not in user
    tb.assert_no_consecutive_user_rows(replay.bodies[2])


def test_parse_repair_before_the_switch_is_merged_with_the_switch_text():
    result, replay, _ = _run([text_message("x"), text_message("y"), ACCEPT],
                             _mission(max_turns=3))
    assert result.status == "completed"
    # 2 回目の要求 (t=2 = 最終段階の入口) の user は 1 件に統合される
    body = replay.bodies[1]
    tb.assert_no_consecutive_user_rows(body)
    last = body["messages"][-1]
    assert last["role"] == "user"
    assert "JSON として解釈できません" in last["content"]
    assert tbm.SWITCH_TEXT in last["content"]


# ---- 同一呼出し・検索予算 ----------------------------------------------------


def test_search_budget_is_five_executions_and_the_rest_get_a_neutral_reply():
    result_final = ACCEPT
    replay_script = tb.assistant_messages(tb.SIGNAL_DISTINCT)[:14] + [result_final]
    result, replay, tools = _run(replay_script, _mission(max_turns=16))
    assert result.status == "completed"
    assert tools.count("search_news") == 5
    tool_msgs = tb.tool_contents(replay.bodies[-1]["messages"])
    budget = [t for t in tool_msgs if isinstance(t, dict)
              and t.get("error") == "search_budget_exhausted"]
    assert len(budget) == 9
    assert budget[0]["message"] == "検索の予算に達した。これまでの材料で判断して JSON を出す"
    assert "accept" not in json.dumps(budget[0], ensure_ascii=False)
    assert result.turn_summary["search_over_budget"] == 9
    assert result.turn_summary["decided_after_budget"] is True
    # 予算切れの後も get_positions は呼べる
    assert tools.count("get_positions") == 1


def test_search_budget_holds_within_one_response():
    many = call_message(*[(f"s{n}", "search_news", {"query": f"q{n}"})
                          for n in range(8)])
    result, _, tools = _run([many, ACCEPT], _mission(max_turns=6))
    assert tools.count("search_news") == 5
    assert result.turn_summary["search_over_budget"] == 3


def test_repeated_query_is_not_run_again_and_gets_duplicate_call():
    script = tb.assistant_messages(tb.SIGNAL_REPEAT)[:14] + [ACCEPT]
    result, replay, tools = _run(script, _mission(max_turns=16))
    distinct = len({json.loads(tc["function"]["arguments"])["query"]
                    for m in script[:-1] for tc in m["tool_calls"]
                    if tc["function"]["name"] == "search_news"})
    assert tools.count("search_news") == min(distinct, 5)
    dup = [t for t in tb.tool_contents(replay.bodies[-1]["messages"])
           if isinstance(t, dict) and t.get("error") == "duplicate_call"]
    assert dup and dup[0]["message"] == "既に得た結果。判断して JSON を出す"
    assert result.turn_summary["duplicate_suppressed"] == len(dup)
    assert result.turn_summary["search_duplicate"] == len(dup)
    # 重複は検索の実行予算を消費しない
    assert result.turn_summary["search_executed"] == tools.count("search_news")


def test_cron_get_positions_repeat_runs_once_and_search_has_no_budget():
    names = tb.tool_names(tb.CRON_LOOP)
    script = tb.assistant_messages(tb.CRON_LOOP)[:14] + [HOLD]
    result, replay, tools = _run(
        script, _mission(policy="cron", max_turns=16, tools=names), names=names)
    assert result.status == "completed"
    assert tools.count("get_positions") == 1
    assert result.turn_summary["search_over_budget"] == 0
    assert result.turn_summary["duplicate_suppressed"] >= 1


def test_cron_search_has_no_five_call_cap():
    script = [search_call(n) for n in range(1, 8)] + [HOLD]
    result, _, tools = _run(script, _mission(policy="cron", max_turns=10),
                            names=SIGNAL_TOOLS)
    assert tools.count("search_news") == 7
    assert result.status == "completed"


def test_argument_formatting_is_ignored_but_string_contents_are_not():
    script = [
        call_message(("a", "search_news", '{"query":"usd yen"}')),
        call_message(("b", "search_news", '{ "query" : "usd yen" }')),
        call_message(("c", "search_news", '{"query": "USD yen"}')),
        call_message(("d", "search_news", '{"query": "usd  yen"}')),
        ACCEPT]
    result, _, tools = _run(script, _mission(max_turns=8))
    assert tools.count("search_news") == 3
    assert result.turn_summary["search_duplicate"] == 1


def test_tools_outside_the_dedupe_allowlist_run_every_time():
    names = ["get_price"]
    script = [call_message((f"p{n}", "get_price", {"pair": "USDJPY"}))
              for n in range(3)] + [text_message('{"action": "hold"}')]
    result, _, tools = _run(script, _mission(policy="cron", max_turns=8, tools=names),
                            names=names)
    assert tools.count("get_price") == 3
    assert result.turn_summary["duplicate_suppressed"] == 0


def test_transient_error_is_retried_once_and_counts_toward_the_budget():
    script = [search_call(1, "same"), search_call(2, "same"),
              search_call(3, "same"), ACCEPT]
    result, replay, tools = _run(script, _mission(max_turns=8),
                                 fail={"search_news": [True, False]})
    # 1 回目は失敗、2 回目 (同じ引数) は再試行で成功、3 回目は成功済みの重複
    assert tools.count("search_news") == 2
    assert result.status == "completed"
    assert result.turn_summary["search_executed"] == 2
    assert result.turn_summary["search_exec_error"] == 1
    assert result.turn_summary["search_succeeded"] == 1
    assert result.turn_summary["search_duplicate"] == 1


def test_a_key_that_failed_twice_is_not_run_a_third_time():
    script = [search_call(n, "same") for n in range(1, 4)] + [REJECT]
    result, replay, tools = _run(script, _mission(max_turns=8),
                                 fail={"search_news": [True, True]})
    assert tools.count("search_news") == 2
    last = tb.tool_contents(replay.bodies[-1]["messages"])
    assert last[2]["error"] == "duplicate_call"
    assert "失敗" in last[2]["message"]
    assert result.status == "completed"


def test_get_positions_transient_error_can_be_retried_once():
    script = [call_message(("a", "get_positions", {})),
              call_message(("b", "get_positions", {})), ACCEPT]
    result, _, tools = _run(script, _mission(max_turns=6),
                            fail={"get_positions": [True, False]})
    assert tools.count("get_positions") == 2


def test_state_is_not_shared_between_runs_of_one_runner():
    replay = tb.Replay([search_call(1, "same"), ACCEPT, search_call(2, "same"),
                        ACCEPT])
    tools = tb.StubTools(SIGNAL_TOOLS)
    runner = LocalRunner(base_url="http://test/v1", model="m",
                         registry=tools.registry, transport=replay.transport())
    runner.run(_mission(max_turns=6))
    runner.run(_mission(max_turns=6))
    assert tools.count("search_news") == 2


def test_abort_after_a_suppressed_call_still_stops_the_run():
    event = threading.Event()
    calls = {"n": 0}

    def after():
        calls["n"] += 1
        if calls["n"] == 2:
            event.set()

    script = [call_message(("a", "get_positions", {}), ("b", "get_positions", {}),
                           ("c", "get_positions", {})), ACCEPT]
    result, _, tools = _run(script, _mission(max_turns=6), abort_event=event,
                            abort_reason_fn=lambda: "aborted", after_tool_call=after)
    assert result.status == "failed"
    assert result.reason == "aborted"
    assert tools.count("get_positions") == 1


# ---- 検索の試行・成功と終端の優先 -------------------------------------------


def test_all_search_errors_then_accept_fails_with_search_unavailable():
    script = [search_call(1, "same"), search_call(2, "same"), ACCEPT]
    result, _, _ = _run(script, _mission(max_turns=8),
                        fail={"search_news": [True, True]})
    assert result.status == "failed"
    assert result.output is None
    assert result.reason == tbm.REASON_SEARCH_UNAVAILABLE
    assert result.turn_summary["search_succeeded"] == 0
    assert result.turn_summary["search_exec_error"] == 2


def test_failed_search_then_a_successful_one_allows_accept():
    script = [search_call(1, "a"), search_call(2, "b"), ACCEPT]
    result, _, _ = _run(script, _mission(max_turns=8),
                        fail={"search_news": [True, False]})
    assert result.status == "completed"


@pytest.mark.parametrize("arguments", ['{"query": 123}', '{"query": ', "[1]"])
def test_validation_errors_alone_count_as_attempted_without_success(arguments):
    script = [call_message(("a", "search_news", arguments)), ACCEPT]
    result, _, tools = _run(script, _mission(max_turns=6))
    assert result.status == "failed"
    assert result.reason == tbm.REASON_SEARCH_UNAVAILABLE
    assert tools.count("search_news") == 0
    assert result.turn_summary["search_attempted"] is True
    assert result.turn_summary["search_executed"] == 0
    assert result.turn_summary["search_invalid"] == 1
    assert result.turn_summary["search_over_budget"] == 0


def test_validation_errors_do_not_consume_the_execution_budget():
    bad = [call_message((f"b{n}", "search_news", {"query": n})) for n in range(6)]
    good = [search_call(n) for n in range(1, 6)]
    result, _, tools = _run(bad + good + [ACCEPT], _mission(max_turns=14))
    assert tools.count("search_news") == 5
    assert result.status == "completed"


def test_accept_without_any_search_attempt_is_a_normal_completion():
    result, _, _ = _run([call_message(("a", "get_positions", {})), ACCEPT],
                        _mission(max_turns=6))
    assert result.status == "completed"
    assert result.turn_summary["search_attempted"] is False


def test_an_empty_search_result_is_a_success():
    result, _, tools = _run([search_call(1), ACCEPT], _mission(max_turns=6),
                            results={"search_news": []})
    assert result.status == "completed"
    assert result.turn_summary["search_succeeded"] == 1


def test_search_first_tried_in_the_final_stage_does_not_block_a_later_accept():
    result, _, tools = _run([call_message(("a", "get_positions", {})),
                             search_call(2), ACCEPT], _mission(max_turns=3))
    assert tools.count("search_news") == 0
    assert result.status == "completed"
    assert result.turn_summary["search_attempted"] is False


def test_valid_reject_after_failed_searches_is_a_normal_completion():
    script = [search_call(1), REJECT]
    result, _, _ = _run(script, _mission(max_turns=6),
                        fail={"search_news": [True]})
    assert result.status == "completed"
    assert result.output["decision"] == "reject"


def test_no_valid_answer_after_failed_searches_is_search_unavailable():
    script = [search_call(1), search_call(2), search_call(3)]
    result, _, _ = _run(script, _mission(max_turns=3),
                        fail={"search_news": [True, True, True]})
    assert result.status == "failed"
    assert result.reason == tbm.REASON_SEARCH_UNAVAILABLE


def test_search_unavailable_after_a_failed_search_applies_to_signal_only():
    result, _, _ = _run([search_call(1), HOLD],
                        _mission(policy="cron", max_turns=6),
                        fail={"search_news": [True]})
    assert result.status == "completed"


def test_http_failure_keeps_its_own_reason_over_search_unavailable():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(200, json={"choices": [
                {"message": search_call(1)}]})
        return httpx.Response(500, json={"error": {"message": "boom"}})

    tools = tb.StubTools(SIGNAL_TOOLS, fail={"search_news": [True]})
    runner = LocalRunner(base_url="http://test/v1", model="m",
                         registry=tools.registry,
                         transport=httpx.MockTransport(handler))
    result = runner.run(_mission(max_turns=6))
    assert result.status == "failed"
    assert result.reason == "boom"
    assert result.turn_summary["terminal"] == "failed"
    assert result.turn_summary["search_attempted"] is True
    assert result.turn_summary["search_succeeded"] == 0


def test_timeout_keeps_priority_over_search_unavailable():
    clock = {"now": 0.0}
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(200, json={"choices": [
                {"message": search_call(1)}]})
        clock["now"] = 1000.0
        return httpx.Response(200, json={"choices": [{"message": ACCEPT}]})

    tools = tb.StubTools(SIGNAL_TOOLS, fail={"search_news": [True]})
    runner = LocalRunner(base_url="http://test/v1", model="m",
                         registry=tools.registry,
                         transport=httpx.MockTransport(handler),
                         time_fn=lambda: clock["now"])
    result = runner.run(_mission(max_turns=6))
    assert result.status == "timeout"
    assert result.turn_summary["terminal"] == "timeout"
    assert result.turn_summary["search_attempted"] is True


def test_malformed_response_keeps_the_plain_failure():
    def handler(request):
        return httpx.Response(200, json={"choices": []})

    tools = tb.StubTools(SIGNAL_TOOLS)
    runner = LocalRunner(base_url="http://test/v1", model="m",
                         registry=tools.registry,
                         transport=httpx.MockTransport(handler))
    result = runner.run(_mission(max_turns=6))
    assert result.status == "failed"
    assert result.reason is None
    assert result.turn_summary["requests"] == 1


def test_invalid_output_schema_keeps_the_plain_failure():
    result, _, _ = _run([ACCEPT], _mission(max_turns=6,
                                           schema={"type": "nonsense"}))
    assert result.status == "failed"
    assert result.reason is None


# ---- 終端要約 ---------------------------------------------------------------


def test_summary_carries_counts_and_no_query_text():
    script = [search_call(1, "sensitive words"), search_call(2, "sensitive words"),
              ACCEPT]
    result, replay, _ = _run(script, _mission(max_turns=6))
    summary = result.turn_summary
    assert summary["requests"] == 3
    assert summary["terminal"] == "completed"
    assert summary["reason"] is None
    assert "sensitive" not in json.dumps(summary)
    for body in replay.bodies:
        text = json.dumps(body, ensure_ascii=False)
        assert "duplicate_suppressed" not in text
        assert "final_stage_suppressed" not in text


def test_summary_marks_a_decision_taken_in_the_final_stage_after_budget():
    script = [search_call(n) for n in range(1, 8)] + [ACCEPT]
    result, _, _ = _run(script, _mission(max_turns=9))
    assert result.status == "completed"
    assert result.turn_summary["decided_in_final"] is True
    assert result.turn_summary["decided_after_budget"] is True
    assert result.turn_summary["final_stage"] is True


def test_final_stage_counts_the_calls_it_suppressed():
    three = call_message(("a", "get_positions", {}), ("b", "get_positions", {}),
                         ("c", "search_news", {"query": "x"}))
    result, _, tools = _run([search_call(1), three, three],
                            _mission(max_turns=3))
    assert result.status == "max_turns"
    assert result.turn_summary["final_stage_suppressed"] == 6
    assert tools.count("search_news") == 1


def test_a_search_made_only_in_the_final_stage_is_not_an_attempt():
    gp = [call_message((f"g{n}", "get_positions", {})) for n in range(3)]
    result, _, _ = _run(gp + [search_call(4), ACCEPT], _mission(max_turns=5))
    assert result.status == "completed"
    assert result.turn_summary["search_attempted"] is False
