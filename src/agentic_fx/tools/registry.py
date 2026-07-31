"""ツールレジストリ — 1 定義から OpenAI スキーマ / 素関数の 2 形態 (MCP は Phase 2 で追加)。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

import jsonschema

_log = logging.getLogger("agentic_fx.tools")


@dataclass(frozen=True, slots=True)
class ToolDef:
    name: str
    description: str
    parameters: dict
    func: Callable[..., object]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def register_all(self, tools: list[ToolDef]) -> None:
        for t in tools:
            self.register(t)

    def names(self) -> list[str]:
        return list(self._tools)

    def openai_tools(self, allowed: list[str]) -> list[dict]:
        return [{"type": "function",
                 "function": {"name": t.name, "description": t.description,
                              "parameters": t.parameters}}
                for n, t in self._tools.items() if n in allowed]

    def execute(self, name: str, arguments: dict, allowed: list[str]) -> str:
        if name not in allowed or name not in self._tools:
            return json.dumps({"error": f"tool {name!r} not allowed"})
        tool = self._tools[name]
        try:
            jsonschema.validate(arguments, tool.parameters)
        except jsonschema.ValidationError as e:
            return json.dumps({"error": f"invalid arguments: {e.message}"})
        try:
            result = tool.func(**arguments)
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:  # noqa: BLE001 — LLM に返して継続
            _log.warning("tool %s failed: %s", name, e)
            return json.dumps({"error": f"{type(e).__name__}: {e}"})

    def func(self, name: str) -> Callable[..., object]:
        return self._tools[name].func
