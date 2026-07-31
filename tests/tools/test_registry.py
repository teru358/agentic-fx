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
