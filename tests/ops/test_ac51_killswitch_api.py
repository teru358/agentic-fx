"""AC-51: kill switch reset の途中で落ちた後の status / reset / reconcile を API で。"""
from __future__ import annotations

import os
import resource
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_fx.ops.contracts import Principal

from ._api import api  # noqa: F401

CHILD = Path(__file__).with_name("_reset_crash_child.py")


def _crash_reset(env, generation: int, point: str) -> None:
    result = subprocess.run(
        [sys.executable, str(CHILD), str(env.state._path), str(generation), point],
        capture_output=True, text=True, timeout=30,
        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))
    assert result.returncode == 17, result.stderr


@pytest.mark.parametrize("point,file_latched", [("after_marker", True),
                                                ("after_replace", False)])
def test_ac51_marker_left_by_crash_is_reported_confirmed_then_reset(api, point,
                                                                    file_latched):
    env = api.env
    generation = env.state.update(kill_switch_latched=True).kill_switch_generation
    _crash_reset(env, generation, point)
    client = api.client(Principal.APPROVER)
    kill = client.get("/v1/status").json()["data"]["kill_switch"]
    assert kill["reconcile_required"] is True
    assert kill["requested_generation"] == generation
    assert kill["started_at"]
    assert kill["file_latched"] is file_latched
    assert kill["latched"] is True  # 取引は止まったまま
    same = client.post("/v1/killswitch/reset", json={"expected_generation": generation})
    assert same.status_code == 409 and same.json()["error"]["code"] == "invalid_state"
    # autopilot 中も reconcile は通り、reset は 403
    env.state.update(autopilot=True)
    assert client.post("/v1/killswitch/reset",
                       json={"expected_generation": generation}).status_code == 403
    assert api.client(Principal.OPERATOR).post("/v1/killswitch/reconcile",
                                               json={}).status_code == 403
    confirmed = client.post("/v1/killswitch/reconcile", json={})
    assert confirmed.status_code == 200
    assert confirmed.json()["data"]["latched"] is True
    assert env.state.load().kill_switch_latched is True
    after = client.get("/v1/status").json()["data"]["kill_switch"]
    assert after["reconcile_required"] is False and after["latched"] is True
    env.state.update(autopilot=False)
    released = client.post("/v1/killswitch/reset",
                           json={"expected_generation": after["generation"]})
    assert released.status_code == 200 and released.json()["data"]["latched"] is False
    assert env.state.load().kill_switch_latched is False


def test_ac51_crash_after_marker_removal_leaves_nothing_to_reconcile(api):
    env = api.env
    generation = env.state.update(kill_switch_latched=True).kill_switch_generation
    _crash_reset(env, generation, "after_unlink")
    client = api.client(Principal.APPROVER)
    kill = client.get("/v1/status").json()["data"]["kill_switch"]
    assert kill["reconcile_required"] is False and kill["latched"] is False
    before = Path(env.state._path).read_bytes()
    response = client.post("/v1/killswitch/reconcile", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"
    assert Path(env.state._path).read_bytes() == before
