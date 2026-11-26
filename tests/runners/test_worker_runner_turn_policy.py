"""Mission の turn_policy が handshake で子へ渡り、終端要約が親へ戻ること。"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.mission_protocol import write_frame
from agentic_fx.runners import turn_budget as tbm
from agentic_fx.runners.base import Mission
from agentic_fx.runners.worker_runner import WorkerRunner
from tests.runners.test_worker_runner import SETTINGS, _rag, _root


def _mission(policy):
    return Mission(prompt="p", tools=[], output_schema={"type": "object"},
                   max_turns=3, timeout_sec=5.0, turn_policy=policy)


def _drive(monkeypatch, tmp_path, policy, result_extra=None, *, send_result=True):
    r, w = os.pipe()
    r2, w2 = os.pipe()
    seen: dict = {}

    def child():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        seen["handshake"] = json.loads(child_in.readline())
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        if send_result:
            write_frame(child_out, {"type": "result", "seq": 2,
                                    "status": "completed", "output": {"x": 1},
                                    **(result_extra or {})})
        child_out.close()

    t = threading.Thread(target=child, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    import agentic_fx.runners.worker_runner as wr_mod
    fake = FakeProc()
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)
    runner = WorkerRunner(
        root=_root(tmp_path), settings=SETTINGS,
        clock=FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)),
        rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission(policy))
    t.join(timeout=2.0)
    return seen["handshake"], result


def _summary():
    return tbm.TurnGuard("signal", 6).summary("completed", None)


@pytest.mark.parametrize("policy", [None, "signal", "cron"])
def test_handshake_carries_the_policy(monkeypatch, tmp_path, policy):
    handshake, _ = _drive(monkeypatch, tmp_path, policy)
    assert handshake["mission"].get("turn_policy") == policy
    assert ("turn_policy" in handshake["mission"]) is (policy is not None)


def test_result_frame_summary_reaches_the_mission_result(monkeypatch, tmp_path):
    summary = _summary()
    _, result = _drive(monkeypatch, tmp_path, "signal",
                       {"turn_summary": summary})
    assert result.turn_summary == summary


@pytest.mark.parametrize("bad", [{"requests": 1}, "x", [1], 5])
def test_malformed_summary_counts_as_missing_not_zero(monkeypatch, tmp_path, bad):
    _, result = _drive(monkeypatch, tmp_path, "signal", {"turn_summary": bad})
    assert result.turn_summary == {"missing": True}


def test_absent_summary_with_a_policy_counts_as_missing(monkeypatch, tmp_path):
    _, result = _drive(monkeypatch, tmp_path, "signal")
    assert result.turn_summary == {"missing": True}


def test_worker_death_before_the_result_counts_as_missing(monkeypatch, tmp_path):
    _, result = _drive(monkeypatch, tmp_path, "cron", send_result=False)
    assert result.status == "failed"
    assert result.turn_summary == {"missing": True}


def test_no_policy_never_records_a_summary(monkeypatch, tmp_path):
    _, result = _drive(monkeypatch, tmp_path, None)
    assert result.turn_summary is None
    (tmp_path / "second").mkdir()
    _, result = _drive(monkeypatch, tmp_path / "second", None,
                       {"turn_summary": _summary()})
    assert result.turn_summary is None


def _runner_with_child(monkeypatch, tmp_path, child_fn):
    from tests.runners.test_worker_runner import _spawn_runner, _tiny_worker_settings

    r, w = os.pipe()
    r2, w2 = os.pipe()
    kept: list = []

    def child():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        kept.extend([child_in, child_out])
        child_in.readline()
        child_fn(child_out)

    thread = threading.Thread(target=child, daemon=True)
    thread.start()

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    runner, _, _ = _spawn_runner(
        monkeypatch, tmp_path, FakeProc(), _tiny_worker_settings(), close_on_kill=w)
    return runner, w, thread


@pytest.mark.parametrize("policy,expected", [
    ("signal", {"missing": True}), (None, None)])
def test_startup_failure_records_missing_only_with_a_policy(
        monkeypatch, tmp_path, policy, expected):
    runner, w, thread = _runner_with_child(monkeypatch, tmp_path, lambda out: None)
    try:
        result = runner.run(Mission(prompt="p", tools=[], output_schema={},
                                    max_turns=3, timeout_sec=5.0,
                                    turn_policy=policy))
    finally:
        try:
            os.close(w)
        except OSError:
            pass
    assert result.status == "failed"
    assert result.turn_summary == expected


@pytest.mark.parametrize("policy,expected", [
    ("cron", {"missing": True}), (None, None)])
def test_parent_deadline_records_missing_only_with_a_policy(
        monkeypatch, tmp_path, policy, expected):
    release = threading.Event()

    def child_fn(out):
        write_frame(out, {"type": "ready", "seq": 1, "ok": True})
        release.wait()

    runner, w, thread = _runner_with_child(monkeypatch, tmp_path, child_fn)
    try:
        result = runner.run(Mission(prompt="p", tools=[], output_schema={},
                                    max_turns=3, timeout_sec=0.3,
                                    turn_policy=policy))
    finally:
        release.set()
        thread.join(timeout=5)
        try:
            os.close(w)
        except OSError:
            pass
    assert not thread.is_alive()
    assert result.status == "timeout"
    assert result.turn_summary == expected


@pytest.mark.parametrize("status,reason", [
    ("failed", "tool_budget_abort: x"), ("timeout", None)])
def test_summary_follows_the_final_status_when_the_parent_overrides_it(
        monkeypatch, tmp_path, status, reason):
    runner, w, _thread = _runner_with_child(monkeypatch, tmp_path, lambda out: None)
    os.close(w)
    summary = _summary()
    assert summary["terminal"] == "completed"

    def fake(mission):
        from agentic_fx.runners.base import MissionResult
        return MissionResult(status, None, [], reason=reason,
                             turn_summary=dict(summary))

    monkeypatch.setattr(runner, "_run_mission", fake)
    result = runner.run(_mission("signal"))
    assert result.turn_summary["terminal"] == status
    assert result.turn_summary["reason"] == reason
