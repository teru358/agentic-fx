"""ツールレジストリ — 1 定義から OpenAI スキーマ / 素関数の 2 形態 (MCP は Phase 2 で追加)。"""
from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import jsonschema

from agentic_fx._safe_error import safe_error_text, safe_text

if TYPE_CHECKING:
    from agentic_fx.runners.turn_budget import TurnGuard

_log = logging.getLogger("agentic_fx.tools")


@dataclass(frozen=True, slots=True)
class ToolDef:
    name: str
    description: str
    parameters: dict
    func: Callable[..., object]


class ToolRegistry:
    def __init__(self, *, on_execute: Callable[[], None] | None = None,
                 on_result: Callable[[str, bool, str | None], None] | None = None) -> None:
        self._tools: dict[str, ToolDef] = {}
        self._on_execute = on_execute
        # run8 是正 [tool-exception-bypasses-refusal-streak] (2026-09-09): tool の
        # 例外 / 引数不正 / `{"error": …}` 応答は LLM に返して継続する設計だが、
        # それを誰も数えていなかったため、壊れた tool の反復 (293 回・23 分) を
        # refusal streak が止められなかった。許可済み tool の実行結果ごとに
        # (name, ok, error) を通知する。error は失敗種別の識別子 (`exception` /
        # `invalid_args` / tool が返した error 文字列) — codex 是正束レビュー
        # Important 1: 業務エラー (loader_rejected / no_history 等) を種別ごとに
        # 分けて数える。許可外の名前は tool 呼び出しでないので通知しない。
        # hook の例外は握る (Important 2: execute は例外を送出しない契約 —
        # dispatcher / LocalRunner がそれを前提にしている)。
        self._on_result = on_result

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

    def execute(self, name: str, arguments: dict, allowed: list[str],
                guard: "TurnGuard | None" = None) -> str:
        if guard is not None:
            return self._execute_guarded(name, arguments, allowed, guard)
        if self._on_execute is not None:
            try:
                self._on_execute()
            except Exception:  # noqa: BLE001 — 観測フックは実行を止めない
                _log.warning("on_execute hook raised", exc_info=True)
        # F2: check isinstance first to avoid TypeError on unhashable name
        if not isinstance(name, str):
            return json.dumps({"error": f"tool name must be str, got {type(name).__name__!r}"}, ensure_ascii=False)
        if name not in allowed or name not in self._tools:
            return json.dumps({"error": f"tool {name!r} not allowed"}, ensure_ascii=False)
        tool = self._tools[name]
        invalid = self._validate_arguments(name, tool, arguments)
        if invalid is not None:
            return invalid
        return self._invoke(name, tool, arguments)[0]

    def _validate_arguments(self, name: str, tool: ToolDef,
                            arguments: dict) -> str | None:
        # F1: Catch Exception to handle SchemaError and ValidationError
        try:
            jsonschema.validate(arguments, tool.parameters)
        except Exception as e:
            try:
                msg = safe_text(e.message if hasattr(e, "message") else str(e))
            except Exception:
                msg = type(e).__name__
            self._notify_result(name, False, "invalid_args")
            return json.dumps({"error": f"invalid arguments: {msg}"}, ensure_ascii=False)
        return None

    def _execute_guarded(self, name: str, arguments: dict, allowed: list[str],
                         guard: "TurnGuard") -> str:
        """手数制御の guard 付き実行。検証 → 抑止判定 → 実行通知 → 実行の順
        (抑止した呼出しは実行として通知しない)。"""
        tool = self._tools.get(name) if isinstance(name, str) else None
        invalid = None
        if not isinstance(name, str):
            invalid = json.dumps(
                {"error": f"tool name must be str, got {type(name).__name__!r}"},
                ensure_ascii=False)
        elif name not in allowed or tool is None:
            invalid = json.dumps({"error": f"tool {name!r} not allowed"},
                                 ensure_ascii=False)
        else:
            invalid = self._validate_arguments(name, tool, arguments)
        if invalid is not None:
            self._fire_on_execute()
            guard.note_invalid(name)
            return invalid
        suppressed = guard.decide(name, arguments)
        if suppressed is not None:
            return suppressed
        self._fire_on_execute()
        payload, ok = self._invoke(name, tool, arguments)
        guard.record(name, arguments, ok)
        return payload

    def _fire_on_execute(self) -> None:
        if self._on_execute is not None:
            try:
                self._on_execute()
            except Exception:  # noqa: BLE001 — 観測フックは実行を止めない
                _log.warning("on_execute hook raised", exc_info=True)

    def _invoke(self, name: str, tool: ToolDef, arguments: dict) -> tuple[str, bool]:
        """検証済みの呼出しを実行する。(LLM に返す文字列, 成功か)。"""
        try:
            result = tool.func(**arguments)
        except Exception as e:  # noqa: BLE001 — LLM に返して継続
            # F3, F4: guard formatting, use safe_error_text
            try:
                error_msg = safe_error_text(e)
            except Exception:
                error_msg = type(e).__name__
            _log.warning("tool %s failed: %s", name, error_msg)
            self._notify_result(name, False, "exception")
            return json.dumps({"error": error_msg}, ensure_ascii=False), False
        try:
            payload = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as e:
            # 直列化できない戻り値は「届かない結果」— 成功として streak を戻さない
            # (codex Minor 4)。LLM には error として返す。
            self._notify_result(name, False, "exception")
            return json.dumps(
                {"error": f"unserializable tool result: {type(e).__name__}"},
                ensure_ascii=False), False
        if isinstance(result, dict) and "error" in result:
            self._notify_result(name, False, str(result["error"]))
            return payload, False
        self._notify_result(name, True, None)
        return payload, True

    def _notify_result(self, name: str, ok: bool, error: str | None) -> None:
        if self._on_result is None:
            return
        try:
            self._on_result(name, ok, error)
        except Exception:  # noqa: BLE001 — 観測フックは実行を止めない
            _log.warning("on_result hook raised", exc_info=True)

    def func(self, name: str) -> Callable[..., object]:
        return self._tools[name].func
