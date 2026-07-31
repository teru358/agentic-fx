import json

import httpx

from agentic_fx.runners.base import Mission
from agentic_fx.runners.local_runner import LocalRunner
from agentic_fx.tools.registry import ToolDef, ToolRegistry

SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}},
          "required": ["action"]}


def _registry():
    reg = ToolRegistry()
    reg.register(ToolDef("get_price", "price",
                         {"type": "object", "properties": {}},
                         lambda: {"price": 148.5}))
    return reg


def _mission(**over):
    d = dict(prompt="判断せよ", tools=["get_price"], output_schema=SCHEMA,
             max_turns=6, timeout_sec=30)
    d.update(over)
    return Mission(**d)


def _resp(message):
    return httpx.Response(200, json={"choices": [{"message": message}]})


def _runner(script):
    """script: list of response messages returned in order."""
    calls = {"i": 0, "bodies": []}

    def handler(request):
        calls["bodies"].append(json.loads(request.content))
        msg = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return _resp(msg)

    runner = LocalRunner(base_url="http://test/v1", model="qwen",
                         registry=_registry(),
                         transport=httpx.MockTransport(handler))
    return runner, calls


def test_direct_final_answer():
    runner, calls = _runner([{"role": "assistant",
                              "content": '{"action": "hold"}'}])
    r = runner.run(_mission())
    assert r.status == "completed"
    assert r.output == {"action": "hold"}
    assert calls["bodies"][0]["model"] == "qwen"
    assert calls["bodies"][0]["tools"][0]["function"]["name"] == "get_price"


def test_tool_call_loop():
    tool_call_msg = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "get_price", "arguments": "{}"}}]}
    final = {"role": "assistant", "content": '{"action": "open"}'}
    runner, calls = _runner([tool_call_msg, final])
    r = runner.run(_mission())
    assert r.status == "completed"
    # 2 回目のリクエストに tool 結果が渡っていること
    msgs = calls["bodies"][1]["messages"]
    assert msgs[-1]["role"] == "tool"
    assert json.loads(msgs[-1]["content"]) == {"price": 148.5}
    # transcript に全メッセージが残る
    assert any(m.get("role") == "tool" for m in r.transcript)


def test_think_tag_repaired():
    runner, _ = _runner([{"role": "assistant",
                          "content": '<think>hmm</think>{"action": "hold"}'}])
    assert runner.run(_mission()).status == "completed"


def test_tool_call_missing_id():
    """T-F1: tool_calls entry without 'id' → status 'failed'."""
    tool_call_msg = {"role": "assistant", "content": None, "tool_calls": [
        {"type": "function",
         "function": {"name": "get_price", "arguments": "{}"}}]}
    runner, _ = _runner([tool_call_msg])
    r = runner.run(_mission())
    assert r.status == "failed"


def test_content_not_string():
    """T-F2: non-string content triggers repair path."""
    # First message has dict content (not string), should be repaired
    # Second attempt still has list content
    # Third attempt still has int content
    # Fourth attempt has valid string (but we're out of retries)
    msgs = [
        {"role": "assistant", "content": {"action": "hold"}},
        {"role": "assistant", "content": ["invalid"]},
        {"role": "assistant", "content": 123},
        {"role": "assistant", "content": '{"action": "hold"}'}
    ]
    runner, _ = _runner(msgs)
    r = runner.run(_mission())
    # Should fail after 3 retries (default _MAX_REPAIR_RETRIES=2 allows up to 2 errors)
    assert r.status == "failed"


def test_tool_arguments_null():
    """F6: tool_call with arguments: null must execute with {}."""
    tool_call_msg = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "get_price", "arguments": None}}]}
    final = {"role": "assistant", "content": '{"action": "open"}'}
    runner, calls = _runner([tool_call_msg, final])
    r = runner.run(_mission())
    assert r.status == "completed"
    # Verify tool was called with empty args (price is 148.5 from lambda)
    msgs = calls["bodies"][1]["messages"]
    assert any(m.get("role") == "tool" for m in msgs)


def test_normalized_message_no_extra_fields():
    """T-F5: extra fields in message are not passed to next request."""
    tool_call_msg = {"role": "assistant", "content": None,
                     "reasoning_content": "internal reasoning",
                     "extra_field": "should be removed",
                     "tool_calls": [{"id": "c1", "type": "function",
                                      "function": {"name": "get_price",
                                                    "arguments": "{}"}}]}
    final = {"role": "assistant", "content": '{"action": "open"}'}
    runner, calls = _runner([tool_call_msg, final])
    r = runner.run(_mission())
    assert r.status == "completed"
    # Second request's message list should NOT have extra fields
    msgs = calls["bodies"][1]["messages"]
    assistant_msg = msgs[1]  # First non-user message in request 2
    assert "reasoning_content" not in assistant_msg
    assert "extra_field" not in assistant_msg
    assert "role" in assistant_msg
    assert "tool_calls" in assistant_msg


def test_timeout_during_parse_retry():
    """T-F4: deadline expires during last parse retry → status 'timeout'."""
    # Use a time function that advances rapidly
    call_count = {"n": 0}

    def fake_time():
        call_count["n"] += 1
        # Advance time 5 seconds per call
        return call_count["n"] * 5

    msgs = [
        {"role": "assistant", "content": '{"action": "invalid"}'},  # fails schema
        {"role": "assistant", "content": '{"action": "still_invalid"}'},  # fails again
    ]
    runner = LocalRunner(base_url="http://test/v1", model="qwen",
                         registry=_registry(),
                         transport=httpx.MockTransport(
                             lambda r: _resp(msgs[min(len(msgs) - 1, 1)])),
                         time_fn=fake_time)
    # timeout_sec=5 means deadline at 5s. With call_count advancing, we should hit
    # deadline during the second schema validation, causing timeout instead of failed.
    r = runner.run(_mission(timeout_sec=5))
    # Since time advances quickly, should hit timeout
    assert r.status == "timeout"
