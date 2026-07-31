import json

import pytest

from agentic_fx.tools.registry import ToolDef, ToolRegistry


def _echo_tool():
    return ToolDef(
        name="echo", description="echo back",
        parameters={"type": "object",
                    "properties": {"msg": {"type": "string"}},
                    "required": ["msg"]},
        func=lambda msg: {"echoed": msg})


def test_register_and_names():
    reg = ToolRegistry()
    reg.register(_echo_tool())
    assert reg.names() == ["echo"]
    with pytest.raises(ValueError):
        reg.register(_echo_tool())


def test_openai_tools_filters_allowed():
    reg = ToolRegistry()
    reg.register(_echo_tool())
    reg.register(ToolDef("other", "d", {"type": "object", "properties": {}},
                         lambda: 1))
    schemas = reg.openai_tools(["echo"])
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "echo"
    assert schemas[0]["type"] == "function"


def test_execute_returns_json():
    reg = ToolRegistry()
    reg.register(_echo_tool())
    out = reg.execute("echo", {"msg": "hi"}, allowed=["echo"])
    assert json.loads(out) == {"echoed": "hi"}


def test_execute_not_allowed_returns_error_string():
    reg = ToolRegistry()
    reg.register(_echo_tool())
    out = reg.execute("echo", {"msg": "hi"}, allowed=[])
    assert "error" in out and "not allowed" in out


def test_execute_exception_returns_error_string():
    reg = ToolRegistry()
    reg.register(ToolDef("boom", "d", {"type": "object", "properties": {}},
                         func=lambda: 1 / 0))
    out = reg.execute("boom", {}, allowed=["boom"])
    assert "error" in out


def test_execute_invalid_arguments_rejected_before_call():
    called = []
    reg = ToolRegistry()
    reg.register(ToolDef(
        "typed", "d",
        {"type": "object",
         "properties": {"n": {"type": "integer", "minimum": 1}},
         "required": ["n"], "additionalProperties": False},
        func=lambda n: called.append(n)))
    out1 = reg.execute("typed", {"n": "abc"}, allowed=["typed"])
    out2 = reg.execute("typed", {"n": 1, "extra": True}, allowed=["typed"])
    out3 = reg.execute("typed", {}, allowed=["typed"])
    assert all("error" in o for o in (out1, out2, out3))
    assert called == []  # 検証不合格では関数を呼ばない


def test_raw_func_access():
    reg = ToolRegistry()
    reg.register(_echo_tool())
    assert reg.func("echo")(msg="direct") == {"echoed": "direct"}


# ===== Killer Tests (T1-T8) =====

def test_T1_invalid_schema_does_not_raise():
    """T1: registered tool with invalid schema doesn't raise execute()."""
    reg = ToolRegistry()
    # Register with invalid schema type
    reg.register(ToolDef(
        "bad_schema", "d",
        {"type": "not-a-real-type"},  # jsonschema.validate will raise SchemaError
        func=lambda: "ok"))
    # Should return error string, not raise
    out = reg.execute("bad_schema", {}, allowed=["bad_schema"])
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert "error" in parsed


def test_T2_unhashable_name_does_not_raise():
    """T2: unhashable name (e.g. dict) doesn't raise execute() when allowed list makes it reach self._tools check."""
    reg = ToolRegistry()
    reg.register(_echo_tool())
    # Unhashable name with allowed=[{}] forces comparison {} not in [{}] to be False,
    # which means it reaches `name not in self._tools` where unhashable name would raise TypeError.
    # The isinstance guard must prevent the TypeError from escaping.
    out = reg.execute({}, {}, allowed=[{}])  # type: ignore
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert "error" in parsed
    # Verify it's the isinstance guard that caught it (not short-circuit from allowed check)
    assert "str" in parsed["error"]


def test_T3_exception_with_broken_str_does_not_raise():
    """T3: tool raising exception whose __str__ raises doesn't crash execute()."""
    class BrokenStrException(Exception):
        def __str__(self):
            raise ValueError("__str__ is broken!")

    reg = ToolRegistry()
    reg.register(ToolDef(
        "broken_str", "d",
        {"type": "object", "properties": {}},
        func=lambda: (_ for _ in ()).throw(BrokenStrException("original"))))

    out = reg.execute("broken_str", {}, allowed=["broken_str"])
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert "error" in parsed
    # Should contain the exception type name even if __str__ fails
    assert "BrokenStrException" in parsed["error"]


def test_T4_exception_text_does_not_leak_secrets():
    """T4: exception with secrets is sanitized (no URLs, no token values)."""
    reg = ToolRegistry()
    reg.register(ToolDef(
        "secret_leaker", "d",
        {"type": "object", "properties": {}},
        func=lambda: (_ for _ in ()).throw(
            RuntimeError("failed for https://user:secret@example.test/x?token=SECRET&apikey=KEY"))))

    out = reg.execute("secret_leaker", {}, allowed=["secret_leaker"])
    parsed = json.loads(out)
    error_text = parsed["error"]

    # Must not leak:
    assert "example.test" not in error_text
    assert "SECRET" not in error_text
    assert "https://" not in error_text
    # But should indicate it was sanitized
    assert "<url>" in error_text or "apikey=***" in error_text


def test_T5_allowed_but_unregistered_returns_error_not_keyerror():
    """T5: tool in allowed but not registered returns error string, not KeyError."""
    reg = ToolRegistry()
    # Don't register "missing"
    out = reg.execute("missing", {}, allowed=["missing"])
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert "error" in parsed
    # Should mention "not allowed" (because it's not in self._tools)
    assert "not allowed" in parsed["error"] or "missing" in parsed["error"]


def test_T6_openai_tools_includes_full_schema_and_description():
    """T6: openai_tools result includes description and full parameters schema."""
    reg = ToolRegistry()
    reg.register(ToolDef(
        "test_tool", "This is the description",
        {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        func=lambda x: x))

    schemas = reg.openai_tools(["test_tool"])
    assert len(schemas) == 1
    func_def = schemas[0]["function"]

    # Must have description
    assert func_def["description"] == "This is the description"
    # Must have full parameters
    assert func_def["parameters"]["type"] == "object"
    assert "x" in func_def["parameters"]["properties"]
    assert "x" in func_def["parameters"]["required"]


def test_T7_non_ascii_output_not_escaped():
    """T7: tool raising with non-ASCII message in error path preserves literals, not \\uXXXX."""
    reg = ToolRegistry()
    reg.register(ToolDef(
        "unicode_error_tool", "d",
        {"type": "object", "properties": {}},
        # Tool raises with non-ASCII error message, testing error-path ensure_ascii=False
        func=lambda: (_ for _ in ()).throw(RuntimeError("円高エラー"))))

    out = reg.execute("unicode_error_tool", {}, allowed=["unicode_error_tool"])
    # Should contain literal 円 and 高 in the error message, not \u####
    # The error message goes through safe_error_text which calls str(e),
    # then safe_text, then json.dumps with ensure_ascii=False
    assert "円" in out
    assert "高" in out
    assert "\\u" not in out


def test_T8_mutating_openai_tools_result_does_not_affect_execute():
    """T8: mutating the dict from openai_tools doesn't affect subsequent execute validation."""
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer", "minimum": 1}},
        "required": ["n"]
    }
    reg = ToolRegistry()
    reg.register(ToolDef(
        "mutate_test", "d",
        schema,
        func=lambda n: {"result": n}))

    # Get openai_tools and mutate the returned schema
    schemas = reg.openai_tools(["mutate_test"])
    returned_schema = schemas[0]["function"]["parameters"]
    # Mutate to change validation constraint (increase minimum from 1 to 100)
    returned_schema["properties"]["n"]["minimum"] = 100

    # Now execute with n=5 should succeed (valid under original schema where min=1)
    # WITHOUT deepcopy, the mutation would propagate to the registry's internal schema,
    # and validation would reject n=5 (since min would be 100).
    # WITH deepcopy, the mutation only affects the returned copy, so validation passes.
    out = reg.execute("mutate_test", {"n": 5}, allowed=["mutate_test"])
    parsed = json.loads(out)
    # Should succeed and return the result, not an error
    assert "error" not in parsed
    assert "result" in parsed
    assert parsed["result"] == 5
