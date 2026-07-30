"""AgentRunner 抽象 — 設計書 §4 の Mission/MissionResult をそのまま固定。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class Mission:
    """Agent mission specification (設計書 §4).

    Attributes:
        prompt: Task description for the agent.
        tools: Names of tools available to the agent.
        output_schema: JSON schema for expected output (dict[str, Any]).
        max_turns: Maximum number of turns. Runner-neutral definition:
            1 turn = 1 LLM request + response pair.
            Tool execution within that request/response is part of the same turn.
            JSON repair, schema re-output retries, and other corrections
            are counted as 1 turn if they occur within a single request.
            timeout_sec always takes priority and may force termination before max_turns.

            Phase 2 ClaudeRunner (claude -p) does not have native turn limits.
            It must count turns by this definition, force-terminate when max_turns
            is exceeded, and return status="max_turns" to notify the executor.
        timeout_sec: Wall-clock timeout in seconds. Always takes priority over max_turns.
    """

    prompt: str
    tools: list[str]
    output_schema: dict[str, Any]
    max_turns: int
    timeout_sec: float


@dataclass
class MissionResult:
    status: Literal["completed", "failed", "timeout", "max_turns"]
    output: dict[str, Any] | None
    transcript: list[dict[str, Any]] = field(default_factory=list)


class AgentRunner(ABC):
    @abstractmethod
    def run(self, mission: Mission) -> MissionResult: ...
