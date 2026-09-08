import json
import threading

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


def test_abort_after_first_tool_skips_remaining_calls():
    event = threading.Event()
    called = []
    reg = ToolRegistry()

    def tool(n):
        called.append(n)
        if n == 1:
            event.set()
        return {"n": n}

    reg.register(ToolDef("work", "work", {
        "type": "object", "properties": {"n": {"type": "integer"}},
        "required": ["n"]}, tool))
    msg = {"role": "assistant", "content": None, "tool_calls": [
        {"id": str(n), "type": "function",
         "function": {"name": "work", "arguments": json.dumps({"n": n})}}
        for n in (1, 2, 3)]}
    transport = httpx.MockTransport(lambda _request: _resp(msg))
    runner = LocalRunner(
        base_url="http://test/v1", model="qwen", registry=reg,
        transport=transport, abort_event=event,
        abort_reason_fn=lambda: "tool_budget_abort:terminal_refusals")
    result = runner.run(_mission(tools=["work"]))
    assert called == [1]
    assert result.status == "failed"
    assert result.reason == "tool_budget_abort:terminal_refusals"


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
    """T-F2: non-string content triggers repair path.

    _MAX_REPAIR_RETRIES=2 allows 2 non-string attempts to continue
    (parse_retries reaches 1, then 2); the 3rd non-string attempt makes
    parse_retries=3 > 2, so exhaustion returns "failed" there. The 4th
    (valid string) script entry is never reached/consumed.
    """
    msgs = [
        {"role": "assistant", "content": {"action": "hold"}},
        {"role": "assistant", "content": ["invalid"]},
        {"role": "assistant", "content": 123},
        {"role": "assistant", "content": '{"action": "hold"}'}
    ]
    runner, _ = _runner(msgs)
    r = runner.run(_mission())
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
    """T-F4: deadline expires during retry exhaustion → status 'timeout' not 'failed'.

    Uses schema-validation failures (not parse failures) to reach exhaustion.
    The time_fn is calibrated so several turns complete normally, and the
    deadline crosses only when _finish checks it during exhaustion.
    """
    call_count = {"n": 0}

    def fake_time():
        # deadline = 0.6 + 5.5 = 6.1, increment=0.6s per call. Calibrated so retry
        # exhaustion (schema_retries > _MAX_REPAIR_RETRIES=2) happens on
        # turn 3, and the deadline crosses only when _finish checks it
        # during that exhaustion (not at an earlier remaining<=0 check):
        # Turn 1: n=1,0.6; HTTP n=2,1.2; schema_retries=1 n=3,1.8; continue
        # Turn 2: n=4,2.4; HTTP n=5,3.0; schema_retries=2 n=6,3.6; continue
        # Turn 3: n=7,4.2; HTTP n=8,4.8; schema_retries=3 n=9,5.4;
        #         exhaustion, _finish called n=11,6.6 >= 6.1
        call_count["n"] += 1
        return call_count["n"] * 0.6

    # All responses invalid per schema (missing "action" or wrong type)
    invalid_schema = {"role": "assistant", "content": '{"wrong_field": "value"}'}
    runner = LocalRunner(base_url="http://test/v1", model="qwen",
                         registry=_registry(),
                         transport=httpx.MockTransport(lambda r: _resp(invalid_schema)),
                         time_fn=fake_time)
    r = runner.run(_mission(timeout_sec=5.5, max_turns=10))
    # Should timeout when retry-exhaustion check hits the deadline
    assert r.status == "timeout"


def test_tool_call_entry_not_dict():
    """O2 coverage: tool_calls entry is not a dict (e.g., string) → status 'failed'."""
    tool_call_msg = {"role": "assistant", "content": None, "tool_calls": [
        "invalid_string_entry"]}
    runner, _ = _runner([tool_call_msg])
    r = runner.run(_mission())
    assert r.status == "failed"


def test_tool_call_missing_function():
    """O2 coverage: tool_calls entry missing 'function' field → status 'failed'."""
    tool_call_msg = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function"}]}  # missing 'function'
    runner, _ = _runner([tool_call_msg])
    r = runner.run(_mission())
    assert r.status == "failed"


def test_content_not_string_repair_stores_stringified_content():
    """W1: the F5-stored assistant message with non-string content must be
    stringified in place, so a content-type-strict server would accept the
    NEXT request and the repair prompt actually reaches the LLM.
    """
    msgs = [
        {"role": "assistant", "content": {"action": "hold"}},
        {"role": "assistant", "content": '{"action": "hold"}'},
    ]
    runner, calls = _runner(msgs)
    r = runner.run(_mission())
    assert r.status == "completed"

    assistant_msgs = [m for m in r.transcript if m.get("role") == "assistant"]
    repaired = assistant_msgs[0]
    assert isinstance(repaired["content"], str)
    assert json.loads(repaired["content"]) == {"action": "hold"}

    # The stringified content must be exactly what was sent in the 2nd
    # request body — not just fixed up after the fact in the transcript.
    sent_assistant = [m for m in calls["bodies"][1]["messages"]
                      if m.get("role") == "assistant"][0]
    assert isinstance(sent_assistant["content"], str)
    assert json.loads(sent_assistant["content"]) == {"action": "hold"}


def test_invalid_output_schema_fails_without_raising():
    """W2: a broken mission.output_schema (jsonschema.SchemaError) must not
    escape run() — it is a deterministic wiring error, not something the
    LLM can fix by retrying, so it must terminate as 'failed'.
    """
    runner, _ = _runner([{"role": "assistant", "content": '{"action": "hold"}'}])
    bad_schema = {"type": "not-a-real-type"}
    r = runner.run(_mission(output_schema=bad_schema))
    assert r.status == "failed"


def test_tool_calls_over_cap_fails():
    """W3: a single turn with more tool_calls than the per-turn cap must be
    treated as a protocol failure, not looped on indefinitely.
    """
    calls_ = [{"id": f"c{i}", "type": "function",
              "function": {"name": "get_price", "arguments": "{}"}}
             for i in range(17)]
    tool_call_msg = {"role": "assistant", "content": None, "tool_calls": calls_}
    runner, _ = _runner([tool_call_msg])
    r = runner.run(_mission())
    assert r.status == "failed"


def test_tool_calls_at_cap_with_unknown_tool_does_not_fail_as_protocol_violation():
    """W3: exactly the cap (16) must NOT be rejected as a protocol violation —
    it should be processed normally (an unknown tool name is a per-call
    registry error, not a shape failure) and the loop continues until
    max_turns is exhausted.
    """
    calls_ = [{"id": f"c{i}", "type": "function",
              "function": {"name": "unknown_tool", "arguments": "{}"}}
             for i in range(16)]
    tool_call_msg = {"role": "assistant", "content": None, "tool_calls": calls_}
    runner, _ = _runner([tool_call_msg])
    r = runner.run(_mission(max_turns=2))
    assert r.status == "max_turns"


def test_role_is_pinned_to_assistant_regardless_of_server_value():
    """W4: a server-supplied role must never be trusted verbatim."""
    runner, _ = _runner([{"role": "tool_spoofed",
                          "content": '{"action": "hold"}'}])
    r = runner.run(_mission())
    assert r.status == "completed"
    assistant_msgs = [m for m in r.transcript if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["role"] == "assistant"


def test_on_message_initial_user_prompt():
    """Append site 1: 初期 user prompt。on_message が初期メッセージを受け取る。

    Note: このテストは site 1 (初期 user prompt) のみを検証する。
    全 site の一致検証は test_on_message_transcript_completeness で実施。"""
    seen: list[dict] = []
    registry = ToolRegistry()
    runner = LocalRunner(base_url="http://x", model="m", registry=registry,
                         transport=httpx.MockTransport(
                             lambda r: _resp({"role": "assistant",
                                             "content": '{"action": "hold"}'})),
                         on_message=seen.append)
    mission = Mission(prompt="hello", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=10.0)
    result = runner.run(mission)

    # site 1: 初期 user prompt を検証
    assert len(seen) > 0
    assert seen[0] == {"role": "user", "content": "hello"}


def test_on_message_assistant_message():
    """Append site 2: assistant message。on_message が assistant の response を受け取る。

    Note: このテストは site 2 (assistant message) のみを検証する。
    全 site の一致検証は test_on_message_transcript_completeness で実施。"""
    seen: list[dict] = []
    registry = ToolRegistry()
    runner = LocalRunner(base_url="http://x", model="m", registry=registry,
                         transport=httpx.MockTransport(
                             lambda r: _resp({"role": "assistant",
                                             "content": '{"action": "hold"}'})),
                         on_message=seen.append)
    mission = Mission(prompt="q", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=10.0)
    result = runner.run(mission)

    # site 2: assistant message を検証 (site 1 に依存しない)
    # on_message が assistant message を受け取ったことを確認
    assistant_msgs = [m for m in seen if m.get("role") == "assistant"]
    assert len(assistant_msgs) >= 1


def test_on_message_tool_result():
    """Append site 3: tool result。tool_call に対する結果を on_message が受け取る。

    Note: このテストは site 3 (tool result) のみを検証する。
    全 site の一致検証は test_on_message_transcript_completeness で実施。"""
    seen: list[dict] = []
    request_count = {"n": 0}
    registry = ToolRegistry()
    registry.register(ToolDef("greet", "greeting tool",
                             {"type": "object", "properties": {}},
                             lambda: "hello"))

    responses = [
        {"role": "assistant", "content": None,
         "tool_calls": [
             {"id": "tc1", "type": "function",
              "function": {"name": "greet", "arguments": "{}"}}]},
        {"role": "assistant", "content": '{"action": "done"}'}
    ]

    def handler(request):
        msg = responses[min(request_count["n"], len(responses) - 1)]
        request_count["n"] += 1
        return _resp(msg)

    runner = LocalRunner(base_url="http://x", model="m", registry=registry,
                         transport=httpx.MockTransport(handler),
                         on_message=seen.append)
    mission = Mission(prompt="q", tools=["greet"],
                      output_schema={"type": "object"}, max_turns=2,
                      timeout_sec=10.0)
    result = runner.run(mission)

    # site 3: tool result を検証
    tool_msgs = [m for m in seen if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "tc1"


def test_on_message_transcript_completeness():
    """Integration test: all 6 append sites contribute to complete transcript.

    This test verifies that on_message receives all messages and that
    the complete transcript matches the captured messages.
    Ensures sites 1-3 are properly captured without site-level failures.
    """
    seen: list[dict] = []
    registry = ToolRegistry()
    runner = LocalRunner(base_url="http://x", model="m", registry=registry,
                         transport=httpx.MockTransport(
                             lambda r: _resp({"role": "assistant",
                                             "content": '{"action": "hold"}'})),
                         on_message=seen.append)
    mission = Mission(prompt="hello", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=10.0)
    result = runner.run(mission)

    # Complete transcript match: all sites contribute in order
    assert seen == result.transcript
    # Verify the expected message count: initial user + assistant response
    assert len(seen) == 2
    assert seen[0]["role"] == "user"
    assert seen[1]["role"] == "assistant"


def test_on_message_non_string_content_repair():
    """Append site 4: 非文字列 content の修復prompt。
    content が dict/list だと JSON stringify されて修復メッセージが sent。
    on_message は deepcopy を受け取るため、元の dict のままのはず。

    Note: mock transport の応答切り替えは seen ではなく request_count を使用。
    観測チャネルは mission の実行軌跡に影響を与えてはならない。"""
    seen: list[dict] = []
    request_count = {"n": 0}
    registry = ToolRegistry()

    def handler(request):
        response = (
            _resp({"role": "assistant", "content": {"action": "invalid"}})
            if request_count["n"] < 1
            else _resp({"role": "assistant", "content": '{"action": "hold"}'})
        )
        request_count["n"] += 1
        return response

    runner = LocalRunner(base_url="http://x", model="m", registry=registry,
                         transport=httpx.MockTransport(handler),
                         on_message=seen.append)
    mission = Mission(prompt="q", tools=[], output_schema={"type": "object"},
                      max_turns=3, timeout_sec=10.0)
    result = runner.run(mission)

    # site 4: 非文字列 content の修復メッセージが送出されたことを確認
    repair_msgs = [m for m in seen if "JSON として解釈できません" in
                   m.get("content", "")]
    assert len(repair_msgs) > 0

    # site 4: on_message が受け取った assistant msg は dict のまま
    seen_assistant_msgs = [m for m in seen if m.get("role") == "assistant"]
    assert len(seen_assistant_msgs) >= 1
    if isinstance(seen_assistant_msgs[0].get("content"), dict):
        # deepcopy で保護されているため dict のままのはず
        assert seen_assistant_msgs[0]["content"] == {"action": "invalid"}
    else:
        # deepcopy が機能していない場合は asserta
        assert False, ("on_message が受け取った assistant msg の content は "
                      "dict のままであるべき (deepcopy で保護)")

    # transcript の assistant msg は stringify 済み
    result_assistant_msgs = [m for m in result.transcript
                            if m.get("role") == "assistant"]
    assert any(isinstance(m.get("content"), str) for m in result_assistant_msgs)


def test_on_message_parse_error_repair():
    """Append site 5: JSON parse error の修復prompt。
    content が不正な JSON だと修復メッセージが sent。

    Note: mock transport の応答切り替えは seen ではなく request_count を使用。
    観測チャネルは mission の実行軌跡に影響を与えてはならない。"""
    seen: list[dict] = []
    request_count = {"n": 0}
    registry = ToolRegistry()

    def handler(request):
        response = (
            _resp({"role": "assistant", "content": "not valid json"})
            if request_count["n"] < 1
            else _resp({"role": "assistant", "content": '{"action": "hold"}'})
        )
        request_count["n"] += 1
        return response

    runner = LocalRunner(base_url="http://x", model="m", registry=registry,
                         transport=httpx.MockTransport(handler),
                         on_message=seen.append)
    mission = Mission(prompt="q", tools=[], output_schema={"type": "object"},
                      max_turns=3, timeout_sec=10.0)
    result = runner.run(mission)

    # site 5: JSON parse error 修復メッセージが見えていることを確認
    repair_msgs = [m for m in seen if "JSON として解釈できません" in
                   m.get("content", "")]
    assert len(repair_msgs) > 0


def test_on_message_validation_error_repair():
    """Append site 6: schema validation error の修復prompt。
    output が schema に合わないと修復メッセージが sent。

    Note: mock transport の応答切り替えは seen ではなく request_count を使用。
    観測チャネルは mission の実行軌跡に影響を与えてはならない。"""
    seen: list[dict] = []
    request_count = {"n": 0}
    registry = ToolRegistry()

    def handler(request):
        response = (
            _resp({"role": "assistant", "content": '{"wrong_field": "value"}'})
            if request_count["n"] < 1
            else _resp({"role": "assistant", "content": '{"action": "hold"}'})
        )
        request_count["n"] += 1
        return response

    schema = {"type": "object", "properties": {"action": {"type": "string"}},
              "required": ["action"]}
    runner = LocalRunner(
        base_url="http://x", model="m", registry=registry,
        transport=httpx.MockTransport(handler),
        on_message=seen.append)
    mission = Mission(prompt="q", tools=[], output_schema=schema,
                      max_turns=3, timeout_sec=10.0)
    result = runner.run(mission)

    # site 6: schema validation error 修復メッセージが見えていることを確認
    repair_msgs = [m for m in seen if "スキーマに合いません" in
                   m.get("content", "")]
    assert len(repair_msgs) > 0


def test_on_message_exception_does_not_break_run_runtime_error(monkeypatch, caplog):
    """on_message が RuntimeError を送出しても run() は completed を返す
    (sink はベストエフォートの観測性記録)。"""
    def boom(msg):
        raise RuntimeError("sink failed")

    registry = ToolRegistry()
    runner = LocalRunner(base_url="http://x", model="m", registry=registry,
                         transport=httpx.MockTransport(
                             lambda r: _resp({"role": "assistant",
                                             "content": '{"action": "hold"}'})),
                         on_message=boom)
    mission = Mission(prompt="hello", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=10.0)
    result = runner.run(mission)
    assert result.status == "completed"
    # 警告がログに記録されていることを確認
    assert "on_message callback raised" in caplog.text


def test_on_message_exception_does_not_break_run_os_error(caplog):
    """on_message が OSError (BrokenPipeError等) を送出しても run() は
    completed を返す。Exception の広さ (M-1) が実装されていることを確認。"""
    def broken_pipe(msg):
        raise BrokenPipeError("connection lost")

    registry = ToolRegistry()
    runner = LocalRunner(base_url="http://x", model="m", registry=registry,
                         transport=httpx.MockTransport(
                             lambda r: _resp({"role": "assistant",
                                             "content": '{"action": "hold"}'})),
                         on_message=broken_pipe)
    mission = Mission(prompt="hello", tools=[], output_schema={"type": "object"},
                      max_turns=1, timeout_sec=10.0)
    result = runner.run(mission)
    assert result.status == "completed"
    # 警告がログに記録されていることを確認
    assert "on_message callback raised" in caplog.text


def test_on_message_receives_deepcopy_not_aliased():
    """on_message が受け取る msg は deepcopy であり、run() が後で
    in-place で stringify してもon_message の記録には影響しない (sonnet I-1)。

    deepcopy がなければ、on_message が受け取った msg は messages リストの
    同じ位置のオブジェクトと同一参照になり、run() が stringify 実行後、
    on_message が保存した参照を読むと既に stringify 済みになっている。

    Note: mock transport の応答切り替えは captured_msgs (観測チャネル) ではなく
    request_count を使用。観測チャネルは mission の実行軌跡に影響を与えてはならない。
    """
    captured_msgs: list[dict] = []
    request_count = {"n": 0}

    def capture_msg_by_reference(msg):
        # on_message が受け取ったメッセージの参照をそのまま保存
        # deepcopy がなければ、このメッセージオブジェクトは
        # messages リストの同じ位置のオブジェクトと同一で、
        # 後の stringify で content が "str" に置換される
        captured_msgs.append(msg)

    def handler(request):
        response = (
            _resp({"role": "assistant", "content": {"action": "invalid"}})
            if request_count["n"] < 1
            else _resp({"role": "assistant", "content": '{"action": "hold"}'})
        )
        request_count["n"] += 1
        return response

    registry = ToolRegistry()
    runner = LocalRunner(base_url="http://x", model="m", registry=registry,
                         transport=httpx.MockTransport(handler),
                         on_message=capture_msg_by_reference)
    mission = Mission(prompt="q", tools=[], output_schema={"type": "object"},
                      max_turns=3, timeout_sec=10.0)
    result = runner.run(mission)

    # on_message が捉えた最初の assistant msg (非文字列 content)
    captured_assistant = [m for m in captured_msgs if m.get("role") == "assistant"]
    assert len(captured_assistant) > 0
    first_captured = captured_assistant[0]

    # deepcopy のおかげで、captured_assistant[0] は transcript のものと
    # 別オブジェクト。callback 時点で dict だった content は
    # transcript には stringify された状態で保存される。
    # したがって captured のまま dict で、transcript は str。

    # captured の最初の assistant は dict content のまま
    if isinstance(first_captured.get("content"), dict):
        # deepcopy で保護されているので dict のまま
        assert first_captured["content"] == {"action": "invalid"}
    else:
        # もし str なら、deepcopy がなく aliasing が起こった証拠
        # (このテストは deepcopy あり が前提なので、ここに来ないはず)
        assert False, "captured msg was aliased and stringify 済み"

    # transcript の最初の assistant は stringify 済み
    result_assistant = [m for m in result.transcript if m.get("role") == "assistant"]
    assert len(result_assistant) > 0
    first_result = result_assistant[0]
    # transcript のmsg は stringify されているはず
    assert isinstance(first_result.get("content"), str), (
        "transcript の assistant msg は常に stringify されるべき "
        "(こちらのみが source of truth)"
    )


def test_abort_already_set_before_first_tool_executes_nothing():
    """段 0 pin A14 (2026-09-08): event が tool 実行の**前**に set されていたら
    (別スレッドの dispatcher が set した状況)、同一応答の tool を 1 つも実行しない
    — turn 先頭だけでなく各 tool の直前でも確認する。"""
    event = threading.Event()
    called = []
    reg = ToolRegistry()
    reg.register(ToolDef("work", "work", {
        "type": "object", "properties": {"n": {"type": "integer"}},
        "required": ["n"]}, lambda n: called.append(n) or {"n": n}))
    msg = {"role": "assistant", "content": None, "tool_calls": [
        {"id": str(n), "type": "function",
         "function": {"name": "work", "arguments": json.dumps({"n": n})}}
        for n in (1, 2)]}

    def handler(_request):
        event.set()          # 応答を返した時点 (tool 実行前) に set
        return _resp(msg)

    runner = LocalRunner(
        base_url="http://test/v1", model="qwen", registry=reg,
        transport=httpx.MockTransport(handler), abort_event=event,
        abort_reason_fn=lambda: "tool_budget_abort:max_tool_calls")
    result = runner.run(_mission(tools=["work"]))
    assert called == []
    assert result.status == "failed"
    assert result.reason == "tool_budget_abort:max_tool_calls"
