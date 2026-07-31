import json

import httpx

from agentic_fx.runners.base import Mission
from agentic_fx.runners.local_runner import LocalRunner
from agentic_fx.tools.registry import ToolRegistry

SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}},
          "required": ["action"]}


def _mission(**over):
    d = dict(prompt="p", tools=[], output_schema=SCHEMA, max_turns=8,
             timeout_sec=30)
    d.update(over)
    return Mission(**d)


def _runner(script_or_handler):
    if callable(script_or_handler):
        handler = script_or_handler
    else:
        state = {"i": 0}

        def handler(request):  # noqa: ANN001
            msg = script_or_handler[
                min(state["i"], len(script_or_handler) - 1)]
            state["i"] += 1
            return httpx.Response(200,
                                  json={"choices": [{"message": msg}]})
    return LocalRunner(base_url="http://test/v1", model="m",
                       registry=ToolRegistry(),
                       transport=httpx.MockTransport(handler))


def test_unparsable_output_retried_then_failed():
    runner = _runner([{"role": "assistant", "content": "not json at all"}])
    r = runner.run(_mission())
    assert r.status == "failed"
    # 初回 + リトライ 2 回 = user 修復要求 2 件が transcript に残る
    repair_msgs = [m for m in r.transcript
                   if m.get("role") == "user" and "JSON" in m.get("content", "")]
    assert len(repair_msgs) == 2


def test_schema_violation_retried_then_success():
    bad = {"role": "assistant", "content": '{"wrong": 1}'}
    good = {"role": "assistant", "content": '{"action": "hold"}'}
    runner = _runner([bad, good])
    r = runner.run(_mission())
    assert r.status == "completed"
    assert r.output == {"action": "hold"}


def test_max_turns():
    tool_call = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c", "type": "function",
         "function": {"name": "nope", "arguments": "{}"}}]}
    runner = _runner([tool_call])  # 永遠にツールを呼び続ける
    r = runner.run(_mission(max_turns=3))
    assert r.status == "max_turns"


def test_timeout_before_any_call():
    runner = _runner([{"role": "assistant", "content": '{"action": "hold"}'}])
    r = runner.run(_mission(timeout_sec=0.0))
    assert r.status == "timeout"


def test_http_error_fails():
    def handler(request):  # noqa: ANN001
        return httpx.Response(500, text="boom")
    r = _runner(handler).run(_mission())
    assert r.status == "failed"


def test_network_timeout_is_timeout():
    def handler(request):  # noqa: ANN001
        raise httpx.ConnectTimeout("slow")
    r = _runner(handler).run(_mission())
    assert r.status == "timeout"


class FakeTime:
    """呼ばれるたびに advance 秒進む単調時計。"""

    def __init__(self, advance=0.0):
        self.t = 0.0
        self.advance = advance

    def __call__(self):
        v = self.t
        self.t += self.advance
        return v


def _runner_with_time(script, time_fn):
    state = {"i": 0}

    def handler(request):  # noqa: ANN001
        msg = script[min(state["i"], len(script) - 1)]
        state["i"] += 1
        return httpx.Response(200, json={"choices": [{"message": msg}]})

    from agentic_fx.runners.local_runner import LocalRunner
    return LocalRunner(base_url="http://test/v1", model="m",
                       registry=ToolRegistry(),
                       transport=httpx.MockTransport(handler),
                       time_fn=time_fn)


def test_deadline_checked_after_response():
    # 応答自体は返るが、その間に期限超過 → completed でなく timeout
    good = {"role": "assistant", "content": '{"action": "hold"}'}
    runner = _runner_with_time([good], FakeTime(advance=20.0))
    r = runner.run(_mission(timeout_sec=30))  # 2 回目の時刻参照で 20s、3 回目で 40s
    assert r.status == "timeout"


def test_final_turn_over_deadline_is_timeout_not_max_turns():
    # Calibrated to exercise final loop-exit with deadline expired (F4 timeout priority).
    # time_fn calls: c0(deadline=35), c1(remaining chk), c2(post-HTTP),
    # c3(post-tool), c_final(at _finish). With advance=10: t=0,10,20,30,40.
    # In-loop checks see t<35; final check sees 40>=35 → must return "timeout".
    tool_call = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c", "type": "function",
         "function": {"name": "nope", "arguments": "{}"}}]}
    runner = _runner_with_time([tool_call], FakeTime(advance=10.0))
    r = runner.run(_mission(max_turns=1, timeout_sec=35))
    assert r.status == "timeout"  # ループ上限と期限超過が同時なら timeout 優先


def test_malformed_response_is_failed():
    def handler(request):  # noqa: ANN001
        return httpx.Response(200, json={"choices": []})
    r = _runner(handler).run(_mission())
    assert r.status == "failed"


def test_broken_tool_arguments_reported_to_llm():
    tool_call = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "nope", "arguments": "{broken json"}}]}
    good = {"role": "assistant", "content": '{"action": "hold"}'}
    runner = _runner([tool_call, good])
    r = runner.run(_mission())
    assert r.status == "completed"
    tool_msgs = [m for m in r.transcript if m.get("role") == "tool"]
    assert tool_msgs and "not valid JSON" in tool_msgs[0]["content"]


def test_tool_calls_with_dict_content_stringified():
    """tool_calls + dict content は content を文字列化してプロトコル互換を保つ。

    Verify that in the 2nd request body, the assistant message content is stringified.
    """
    # First turn: tool_calls with non-string (dict) content
    bad_content = {"role": "assistant", "content": {"x": 1}, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "nope", "arguments": "{}"}}]}
    # Second turn: valid JSON output
    good = {"role": "assistant", "content": '{"action": "hold"}'}

    # Use a handler that captures request bodies
    calls = {"i": 0, "bodies": []}
    def handler(request):  # noqa: ANN001
        calls["bodies"].append(json.loads(request.content))
        msg = [bad_content, good][min(calls["i"], 1)]
        calls["i"] += 1
        return httpx.Response(200, json={"choices": [{"message": msg}]})

    runner = LocalRunner(base_url="http://test/v1", model="m",
                        registry=ToolRegistry(),
                        transport=httpx.MockTransport(handler))
    r = runner.run(_mission())
    assert r.status == "completed"

    # Check transcript: first assistant message should have stringified content
    assistant_msgs = [m for m in r.transcript if m.get("role") == "assistant"]
    assert len(assistant_msgs) >= 1
    first_msg = assistant_msgs[0]
    assert "content" in first_msg
    assert isinstance(first_msg["content"], str), "Content should be stringified"

    # Check request body: 2nd request should have stringified content in messages
    assert len(calls["bodies"]) >= 2
    second_req_msgs = calls["bodies"][1]["messages"]
    # Find the assistant message in the 2nd request that had dict content
    assistant_msgs_in_req = [m for m in second_req_msgs if m.get("role") == "assistant"]
    assert len(assistant_msgs_in_req) >= 1
    # The first assistant message (with originally dict content) should be stringified
    first_assistant_in_req = assistant_msgs_in_req[0]
    assert isinstance(first_assistant_in_req["content"], str), \
        "Request body must have stringified content for protocol compatibility"


def test_close_method_exists():
    """LocalRunner.close() can be called without exception."""
    runner = _runner({"role": "assistant", "content": '{"action": "hold"}'})
    # Should not raise
    runner.close()


def test_broken_tool_arguments_with_utf8():
    """W5: ensure_ascii=False in JSONDecodeError handling preserves UTF-8 (W5, code pin)."""
    # Tool argument is broken JSON containing Japanese characters.
    # The error handling uses ensure_ascii=False to preserve UTF-8 if it appears
    # in the error message or related text.
    broken_json = '{壊れた'  # Broken JSON with Japanese chars
    tool_call = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "nope", "arguments": broken_json}}]}
    good = {"role": "assistant", "content": '{"action": "hold"}'}
    runner = _runner([tool_call, good])
    r = runner.run(_mission())
    assert r.status == "completed"
    tool_msgs = [m for m in r.transcript if m.get("role") == "tool"]
    assert len(tool_msgs) >= 1
    error_json_str = tool_msgs[0]["content"]
    # Verify result is valid JSON (ensure_ascii=False produces valid JSON)
    error_dict = json.loads(error_json_str)
    assert "error" in error_dict
    assert "not valid JSON" in error_dict["error"]
    # Verify no unnecessary escape sequences where UTF-8 should be raw
    # (if ensure_ascii=True was used, non-ASCII chars would be \uXXXX)
    assert "\\u" not in error_json_str or error_json_str.count("\\u") == 0
