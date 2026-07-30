"""テスト用 Runner — 定型 MissionResult を返し、受信 Mission を記録する。"""
from __future__ import annotations

from agentic_fx.runners.base import AgentRunner, Mission, MissionResult


class FakeRunner(AgentRunner):
    def __init__(self, results: list[MissionResult]) -> None:
        self._results = list(results)
        self._i = 0
        self.missions: list[Mission] = []

    def run(self, mission: Mission) -> MissionResult:
        self.missions.append(mission)
        if not self._results:
            return MissionResult(status="failed", output=None, transcript=[])
        result = self._results[min(self._i, len(self._results) - 1)]
        self._i += 1
        return result
