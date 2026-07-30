"""AgentRunner 抽象 — 設計書 §4 の Mission/MissionResult をそのまま固定。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Mission:
    prompt: str
    tools: list[str]
    output_schema: dict
    max_turns: int
    timeout_sec: float


@dataclass
class MissionResult:
    status: Literal["completed", "failed", "timeout", "max_turns"]
    output: dict | None
    transcript: list[dict] = field(default_factory=list)


class AgentRunner(ABC):
    @abstractmethod
    def run(self, mission: Mission) -> MissionResult: ...
