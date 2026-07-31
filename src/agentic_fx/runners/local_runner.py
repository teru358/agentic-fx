"""LocalRunner — llama-swap OpenAI 互換 API への自前 tool-calling loop (設計書 §4)。

deadline 規則: timeout_sec が常に優先。HTTP・ツール実行・最終検証のどの段階でも
期限を過ぎていれば timeout を返す。ツール実行自体は同期呼び出しのため個別
タイムアウトを持たない — ツール内の外部アクセスが自前 timeout を持つこと
(プラン 3) と、実行後の deadline 確認の 2 層で防御する。"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

import httpx
import jsonschema

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

        for _turn in range(mission.max_turns):
            remaining = deadline - self._time()
            if remaining <= 0:
                return MissionResult("timeout", None, messages)
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
                _log.warning("llama-swap request failed: %s", e)
                return MissionResult("failed", None, messages)
            if timed_out():
                return MissionResult("timeout", None, messages)

            try:
                msg = resp.json()["choices"][0]["message"]
                if not isinstance(msg, dict):
                    raise TypeError("message is not an object")
            except Exception as e:  # noqa: BLE001 — 不正応答は failed
                _log.warning("malformed llama-swap response: %s", e)
                return MissionResult("failed", None, messages)
            messages.append(msg)

            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    try:
                        args = json.loads(tc["function"]["arguments"] or "{}")
                        result = self._registry.execute(
                            tc["function"]["name"], args, mission.tools)
                    except json.JSONDecodeError as e:
                        # 引数破損は LLM に通知して再呼び出しさせる
                        result = json.dumps(
                            {"error": f"tool arguments are not valid JSON: {e}"})
                    messages.append({"role": "tool",
                                     "tool_call_id": tc["id"],
                                     "content": result})
                    if timed_out():
                        return MissionResult("timeout", None, messages)
                continue

            content = msg.get("content") or ""
            try:
                output = parse_json_output(content)
            except ParseError as e:
                parse_retries += 1
                if parse_retries > _MAX_REPAIR_RETRIES:
                    return MissionResult("failed", None, messages)
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
                    return MissionResult("failed", None, messages)
                messages.append({"role": "user",
                                 "content": f"出力がスキーマに合いません: "
                                            f"{e.message}。修正して JSON のみ"
                                            f"再出力してください。"})
                continue
            if timed_out():
                return MissionResult("timeout", None, messages)
            return MissionResult("completed", output, messages)

        status = "timeout" if timed_out() else "max_turns"
        return MissionResult(status, None, messages)
