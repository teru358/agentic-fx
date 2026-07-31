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
    tool_call = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c", "type": "function",
         "function": {"name": "nope", "arguments": "{}"}}]}
    runner = _runner_with_time([tool_call], FakeTime(advance=10.0))
    r = runner.run(_mission(max_turns=2, timeout_sec=15))
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
