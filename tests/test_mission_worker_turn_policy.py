"""trade worker が handshake の turn_policy を Mission へ復元し、終端要約を
result frame に載せること。"""
from __future__ import annotations

from agentic_fx.runners import turn_budget as tbm
from tests.test_mission_worker_protocol import (
    _FakeLocalRunner, _drive_main)


class _RecordingRunner(_FakeLocalRunner):
    missions: list = []

    def run(self, mission):
        from agentic_fx.runners.base import MissionResult
        _RecordingRunner.missions.append(mission)
        result = MissionResult(status="completed", output={"action": "hold"})
        result.turn_summary = tbm.TurnGuard("signal", 3).summary("completed", None)
        return result


class _PlainRunner(_FakeLocalRunner):
    def run(self, mission):
        from agentic_fx.runners.base import MissionResult
        return MissionResult(status="completed", output={"action": "hold"})


def _mission(**over):
    d = {"prompt": "p", "tools": ["get_quote"], "output_schema": {},
         "max_turns": 3, "timeout_sec": 30.0}
    d.update(over)
    return d


def test_worker_restores_the_policy_and_sends_the_summary(monkeypatch, tmp_path):
    _RecordingRunner.missions = []
    frames, _, _ = _drive_main(
        monkeypatch, tmp_path, runner_cls=_RecordingRunner,
        handshake_overrides={"mission": _mission(turn_policy="signal")})
    assert _RecordingRunner.missions[0].turn_policy == "signal"
    assert frames[-1]["turn_summary"] == tbm.TurnGuard(
        "signal", 3).summary("completed", None)


def test_worker_loads_a_handshake_without_the_policy_key(monkeypatch, tmp_path):
    _RecordingRunner.missions = []
    frames, _, _ = _drive_main(
        monkeypatch, tmp_path, runner_cls=_RecordingRunner,
        handshake_overrides={"mission": _mission()})
    assert _RecordingRunner.missions[0].turn_policy is None
    assert frames[-1]["type"] == "result"


def test_result_frame_has_no_summary_key_when_the_runner_made_none(
        monkeypatch, tmp_path):
    frames, _, _ = _drive_main(monkeypatch, tmp_path, runner_cls=_PlainRunner)
    assert "turn_summary" not in frames[-1]
