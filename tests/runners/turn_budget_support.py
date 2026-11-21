"""取引判断の手数制御の試験で使う、fixture の読込みと fake LLM の再生。

実 transcript の形を保った匿名 fixture (`fixtures/turn_budget/`) から、過去の
assistant の応答を順に再生し、終盤だけ別の応答に差し替えられる。要求 body は
すべて捕捉する。来歴は fixtures/turn_budget/README.md。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx

from agentic_fx.tools.registry import ToolDef, ToolRegistry

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "turn_budget"

SIGNAL_REPEAT = "signal_repeat_queries"
SIGNAL_DISTINCT = "signal_distinct_queries"
CRON_LOOP = "cron_get_positions_loop"


def load_messages(name: str) -> list[dict]:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text("utf-8"))["messages"]


def load_final_responses() -> dict:
    return json.loads((FIXTURE_DIR / "final_stage_responses.json").read_text("utf-8"))


def assistant_messages(name: str) -> list[dict]:
    return [copy.deepcopy(m) for m in load_messages(name)
            if m["role"] == "assistant"]


def tool_names(name: str) -> list[str]:
    seen: list[str] = []
    for m in load_messages(name):
        for tc in m.get("tool_calls") or []:
            n = tc["function"]["name"]
            if n not in seen:
                seen.append(n)
    return seen


class Replay:
    """要求の順に assistant の応答を返す fake LLM。

    `responses` を 1 要求ごとに 1 件ずつ返す。尽きた後の要求は失敗にする
    (再生が足りない試験の誤りを黙って通さない)。`bodies` に要求 body を残す。
    """

    def __init__(self, responses: list[dict]) -> None:
        self.responses = [copy.deepcopy(r) for r in responses]
        self.bodies: list[dict] = []

    @classmethod
    def from_fixture(cls, name: str, *, keep: int | None = None,
                     then: list[dict] | None = None) -> "Replay":
        """fixture の assistant 応答の先頭 `keep` 件 (None なら全件) の後に
        `then` を続ける。"""
        msgs = assistant_messages(name)
        if keep is not None:
            msgs = msgs[:keep]
        return cls(msgs + list(then or []))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        index = len(self.bodies) - 1
        if index >= len(self.responses):
            raise AssertionError(
                f"replay exhausted: request {index + 1} of {len(self.responses)}")
        return httpx.Response(
            200, json={"choices": [{"message": self.responses[index]}]})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class StubTools:
    """名前ごとの固定の結果を返す registry。実行の記録を残す。"""

    def __init__(self, names: list[str], *,
                 results: dict[str, object] | None = None,
                 fail: dict[str, list[bool]] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._results = results or {}
        self._fail = {k: list(v) for k, v in (fail or {}).items()}
        self.registry = ToolRegistry()
        for name in names:
            self.registry.register(ToolDef(
                name, name,
                self._parameters(name),
                self._make(name)))

    @staticmethod
    def _parameters(name: str) -> dict:
        if name == "search_news":
            return {"type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]}
        return {"type": "object"}

    def _make(self, name: str):
        def func(**kwargs):
            self.calls.append((name, kwargs))
            plan = self._fail.get(name)
            if plan and plan.pop(0):
                raise RuntimeError("stub tool failure")
            if name in self._results:
                return copy.deepcopy(self._results[name])
            return [{"title": "t", "body": "b", "source_name": "s"}]
        return func

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)
