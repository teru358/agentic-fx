"""ツールレジストリ — 1 定義から OpenAI スキーマ / 素関数の 2 形態 (MCP は Phase 2 で追加)。"""
from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from typing import Callable

import jsonschema

from agentic_fx._safe_error import safe_error_text, safe_text

_log = logging.getLogger("agentic_fx.tools")


@dataclass(frozen=True, slots=True)
class ToolDef:
    name: str
    description: str
    parameters: dict
    func: Callable[..., object]


class ToolRegistry:
    def __init__(self, *, on_execute: Callable[[], None] | None = None) -> None:
        self._tools: dict[str, ToolDef] = {}
        self._on_execute = on_execute

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
                              "parameters": copy.deepcopy(t.parameters)}}
                for n, t in self._tools.items() if n in allowed]

    def execute(self, name: str, arguments: dict, allowed: list[str]) -> str:
        if self._on_execute is not None:
            self._on_execute()
        # F2: check isinstance first to avoid TypeError on unhashable name
        if not isinstance(name, str):
            return json.dumps({"error": f"tool name must be str, got {type(name).__name__!r}"}, ensure_ascii=False)
        if name not in allowed or name not in self._tools:
            return json.dumps({"error": f"tool {name!r} not allowed"}, ensure_ascii=False)
        tool = self._tools[name]
        # F1: Catch Exception to handle SchemaError and ValidationError
        try:
            jsonschema.validate(arguments, tool.parameters)
        except Exception as e:
            try:
                msg = safe_text(e.message if hasattr(e, "message") else str(e))
            except Exception:
                msg = type(e).__name__
            return json.dumps({"error": f"invalid arguments: {msg}"}, ensure_ascii=False)
        try:
            result = tool.func(**arguments)
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:  # noqa: BLE001 — LLM に返して継続
            # F3, F4: guard formatting, use safe_error_text
            try:
                error_msg = safe_error_text(e)
            except Exception:
                error_msg = type(e).__name__
            _log.warning("tool %s failed: %s", name, error_msg)
            return json.dumps({"error": error_msg}, ensure_ascii=False)

    def func(self, name: str) -> Callable[..., object]:
        return self._tools[name].func
