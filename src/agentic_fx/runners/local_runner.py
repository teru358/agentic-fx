"""LocalRunner — llama-swap OpenAI 互換 API への自前 tool-calling loop (設計書 §4)。

deadline 規則: timeout_sec が常に優先。HTTP・ツール実行・最終検証のどの段階でも
期限を過ぎていれば timeout を返す。ツール実行自体は同期呼び出しのため個別
タイムアウトを持たない — ツール内の外部アクセスが自前 timeout を持つこと
(プラン 3) と、実行後の deadline 確認の 2 層で防御する。

httpx timeout は connect/write/read/pool 各フェーズに適用されるため、
壁時計ではマージンがなくなる場合がある; Phase 1 では localhost llama-swap
(monit watchdog 管理下) と事後の deadline 確認で妥協。"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

import httpx
import jsonschema

from agentic_fx._safe_error import safe_error_text
from agentic_fx.runners.base import AgentRunner, Mission, MissionResult
from agentic_fx.runners.response_parser import ParseError, parse_json_output
from agentic_fx.tools.registry import ToolRegistry

_log = logging.getLogger("agentic_fx.local_runner")
_MAX_REPAIR_RETRIES = 2


class LocalRunner(AgentRunner):
    def __init__(self, *, base_url: str, model: str, registry: ToolRegistry,
                 transport: httpx.BaseTransport | None = None,
                 time_fn: Callable[[], float] = time.monotonic) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._registry = registry
        self._client = httpx.Client(transport=transport)
        self._time = time_fn

    def run(self, mission: Mission) -> MissionResult:
        deadline = self._time() + mission.timeout_sec
        messages: list[dict] = [{"role": "user", "content": mission.prompt}]
        tools = self._registry.openai_tools(mission.tools)
        parse_retries = schema_retries = 0

        def timed_out() -> bool:
            return self._time() >= deadline

        def _finish(status: str) -> MissionResult:
            """Terminal return with timeout priority (F4)."""
            return MissionResult("timeout" if timed_out() else status, None,
                                 messages)

        for _turn in range(mission.max_turns):
            remaining = deadline - self._time()
            if remaining <= 0:
                return _finish("timeout")
            try:
                resp = self._client.post(
                    f"{self._base_url}/chat/completions",
                    json={"model": self._model, "messages": messages,
                          "tools": tools, "tool_choice": "auto"},
                    timeout=remaining)
                resp.raise_for_status()
            except httpx.TimeoutException:
                return MissionResult("timeout", None, messages)
            except httpx.HTTPError as e:
                _log.warning("llama-swap request failed: %s",
                             safe_error_text(e))
                return _finish("failed")
            if timed_out():
                return _finish("timeout")

            try:
                msg = resp.json()["choices"][0]["message"]
                if not isinstance(msg, dict):
                    raise TypeError("message is not an object")
            except Exception as e:  # noqa: BLE001 — 不正応答は failed
                _log.warning("malformed llama-swap response: %s",
                             safe_error_text(e))
                return _finish("failed")

            # F5: Normalize message to standard fields only
            normalized_msg = {"role": msg.get("role", "assistant")}
            if "content" in msg:
                normalized_msg["content"] = msg["content"]
            if "tool_calls" in msg:
                normalized_msg["tool_calls"] = msg["tool_calls"]
            messages.append(normalized_msg)

            # F1: Validate tool_calls structure defensively
            if normalized_msg.get("tool_calls"):
                if not isinstance(normalized_msg["tool_calls"], list):
                    _log.warning("tool_calls is not a list")
                    return _finish("failed")
                for tc in normalized_msg["tool_calls"]:
                    if not isinstance(tc, dict):
                        _log.warning("tool_call entry is not a dict")
                        return _finish("failed")
                    if "id" not in tc or not isinstance(tc.get("id"), str):
                        _log.warning("tool_call missing or non-string id")
                        return _finish("failed")
                    try:
                        func = tc.get("function", {})
                        if not isinstance(func, dict):
                            raise TypeError("function is not a dict")
                        func_name = func.get("name")
                        if not isinstance(func_name, str):
                            raise TypeError("function.name is not a string")
                        args_str = func.get("arguments")
                        # F6: arguments can be None, defaults to {}
                        if args_str is None:
                            args_str = "{}"
                        if not isinstance(args_str, str):
                            raise TypeError("arguments is not a string")
                        args = json.loads(args_str)
                        result = self._registry.execute(func_name, args,
                                                        mission.tools)
                    except json.JSONDecodeError as e:
                        result = json.dumps(
                            {"error": f"tool arguments are not valid JSON: {e}"})
                    except (TypeError, KeyError) as e:
                        _log.warning("tool_call shape validation failed: %s",
                                     safe_error_text(e))
                        return _finish("failed")
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": result})
                    if timed_out():
                        return _finish("timeout")
                continue

            # F2: Validate content is a string
            content = normalized_msg.get("content")
            if content is None:
                content = ""
            elif not isinstance(content, str):
                parse_retries += 1
                if parse_retries > _MAX_REPAIR_RETRIES:
                    return _finish("failed")
                messages.append({"role": "user",
                                 "content": f"出力を JSON として解釈できません "
                                            f"(content is not a string)"
                                            f"。JSON オブジェクトのみを"
                                            f"出力してください。"})
                continue

            # Check deadline before parse_json_output (F4)
            if timed_out():
                return _finish("timeout")

            try:
                output = parse_json_output(content)
            except ParseError as e:
                parse_retries += 1
                if parse_retries > _MAX_REPAIR_RETRIES:
                    return _finish("failed")
                messages.append({"role": "user",
                                 "content": f"出力を JSON として解釈できません "
                                            f"({e})。JSON オブジェクトのみを"
                                            f"出力してください。"})
                continue
            try:
                jsonschema.validate(output, mission.output_schema)
            except jsonschema.ValidationError as e:
                schema_retries += 1
                if schema_retries > _MAX_REPAIR_RETRIES:
                    return _finish("failed")
                messages.append({"role": "user",
                                 "content": f"出力がスキーマに合いません: "
                                            f"{e.message}。修正して JSON のみ"
                                            f"再出力してください。"})
                continue
            if timed_out():
                return _finish("timeout")
            return MissionResult("completed", output, messages)

        return _finish("max_turns")
