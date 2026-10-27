"""失敗の固定分類 `failure_class` を runner protocol に通す。

CliRunner が検知したパターンの表から分類を決め、MissionResult -> worker の result
frame -> WorkerRunner の復元 を経て、親側の MissionResult に届く。

fixture の由来: `code-mode host` の stderr 先頭は実運用の activity ログ
(`cli_stderr_fatal` 行の tail) にあった文面。`usage limit` / `try again at` /
`429 Too Many Requests` / `401 Unauthorized` / `Unauthorized` は実 stderr を観測して
いない暫定の文言 (手書き)。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.runners import cli_runner as cli_mod
from agentic_fx.runners.cli_runner import (
    CLI_STDERR_FAILURE_CLASS, CLI_STDERR_FATAL_PATTERNS,
    _detect_stderr_failure_class, _detect_stderr_fatal)
from tests.runners.test_cli_runner import _mission, _new_runner

#: 実運用の activity ログ (mission 73、codex backend) の tail の先頭
REAL_CODEX_TAIL = (
    "WARNING: proceeding, even though we could not create PATH aliases: "
    "Permission denied (os error 13)\nReading additional input from stdin...\n"
    "2026-09-11T23:46:02.138255Z ERROR codex_core::tools::router: e")

UNOBSERVED = {
    "usage limit": "Error: usage limit reached, please upgrade your plan",
    "try again at": "You've hit your limit. Please try again at 3:00 PM.",
    "429 Too Many Requests": "HTTP 429 Too Many Requests",
    "401 Unauthorized": "HTTP 401 Unauthorized: token expired",
}
OBSERVED = {
    "code-mode host": REAL_CODEX_TAIL + "rror=code-mode host exited with status",
    "SIGTRAP": REAL_CODEX_TAIL + "\nprocess exited with status signal: 5 (SIGTRAP)",
    "closed its stdout": "mcp server closed its stdout",
    "Segmentation fault": "Segmentation fault (core dumped)",
}


def test_every_pattern_has_exactly_one_class_and_the_pattern_order_is_kept():
    assert CLI_STDERR_FATAL_PATTERNS == (
        "code-mode host", "SIGTRAP", "closed its stdout", "Segmentation fault",
        "usage limit", "try again at", "401 Unauthorized", "Unauthorized",
        "429 Too Many Requests")
    assert set(CLI_STDERR_FAILURE_CLASS) == (
        set(CLI_STDERR_FATAL_PATTERNS) - {"Unauthorized"})
    assert {p for p, c in CLI_STDERR_FAILURE_CLASS.items()
            if c == "resource_exhausted"} == {
        "usage limit", "try again at", "429 Too Many Requests",
        "401 Unauthorized"}
    assert {p for p, c in CLI_STDERR_FAILURE_CLASS.items()
            if c == "runner_fault"} == {
        "code-mode host", "SIGTRAP", "closed its stdout", "Segmentation fault"}


@pytest.mark.parametrize("text", [
    "request failed: Unauthorized",
    "the Unauthorized access check passed; schema mismatch in output",
    "error: unauthorized_client is not a known field",
    "got 4010 Unauthorizedx while parsing",
    "elapsed 14012 ms; usage limits section was skipped",
])
def test_ordinary_text_with_the_words_does_not_classify(text):
    assert _detect_stderr_failure_class(text) is None


def test_a_bare_unauthorized_still_reaches_the_diagnostic_fatal_detection():
    assert "pattern=Unauthorized" in _detect_stderr_fatal("request failed: Unauthorized")


def test_resource_exhausted_wins_over_an_earlier_runner_fault_pattern():
    text = ("code-mode host exited with status signal: 5 (SIGTRAP)\n"
            "You've hit the usage limit. Please try again at 3:00 PM.")
    assert _detect_stderr_failure_class(text) == "resource_exhausted"
    reordered = "\n".join(reversed(text.splitlines()))
    assert _detect_stderr_failure_class(reordered) == "resource_exhausted"


def test_runner_fault_alone_stays_runner_fault():
    assert _detect_stderr_failure_class("Segmentation fault (core dumped)") == (
        "runner_fault")


@pytest.mark.parametrize("pattern,text", {**OBSERVED, **UNOBSERVED}.items())
def test_each_pattern_classifies(pattern, text):
    assert _detect_stderr_failure_class(text) == CLI_STDERR_FAILURE_CLASS[pattern]
    assert f"pattern={pattern}" in _detect_stderr_fatal(text)


def test_no_match_gives_no_class():
    assert _detect_stderr_failure_class("ordinary log line") is None
    assert _detect_stderr_failure_class("elapsed 14012 ms, id abc-401-xyz") is None


def test_class_without_a_resource_pattern_follows_the_matching_pattern():
    text = "Unauthorized and also SIGTRAP"
    assert "pattern=SIGTRAP" in _detect_stderr_fatal(text)
    assert _detect_stderr_failure_class(text) == "runner_fault"


@pytest.mark.parametrize("pattern,text", {**OBSERVED, **UNOBSERVED}.items())
def test_cli_runner_result_carries_the_class_on_a_completed_mission(
        tmp_path, pattern, text):
    script = ("import json, sys\n"
              f"sys.stderr.write({text!r} + '\\n')\n"
              "print(json.dumps({'answer': 4}))\n")
    result = _new_runner(script, tmp_path).run(_mission())
    assert result.status == "completed"
    assert result.failure_class == CLI_STDERR_FAILURE_CLASS[pattern]


def test_cli_runner_failed_exit_carries_the_class(tmp_path):
    script = ("import sys\n"
              "sys.stderr.write('usage limit reached\\n')\n"
              "sys.exit(3)\n")
    result = _new_runner(script, tmp_path).run(_mission())
    assert result.status == "failed"
    assert result.failure_class == "resource_exhausted"


def test_cli_runner_failed_exit_with_ordinary_words_has_no_resource_class(tmp_path):
    script = ("import sys\n"
              "sys.stderr.write('Unauthorized access check passed; schema mismatch\\n')\n"
              "sys.exit(3)\n")
    result = _new_runner(script, tmp_path).run(_mission())
    assert result.status == "failed"
    assert result.failure_class is None


def test_cli_runner_without_a_pattern_has_no_class(tmp_path):
    result = _new_runner("import json\nprint(json.dumps({'answer': 4}))\n",
                         tmp_path).run(_mission())
    assert result.failure_class is None


def test_recovery_side_class_is_merged_when_the_primary_has_none(tmp_path):
    from tests.runners.test_cli_runner import _FakeCliRunner
    from agentic_fx.tools.registry import ToolRegistry
    import sys

    class Recovering(_FakeCliRunner):
        def _recover_output(self, mission, stdout_lines, recovery_timeout_sec):
            self._recovery_stderr_fatal = "pattern=usage limit tail=x"
            self._recovery_failure_class = "resource_exhausted"
            return {"answer": 4}

    runner = Recovering(script="print('no answer key')\n",
                        bin_path=Path(sys.executable), model="m",
                        workdir=tmp_path, cli_terminate_grace_sec=0.3,
                        registry=ToolRegistry())
    result = runner.run(_mission())
    assert result.failure_class == "resource_exhausted"


def test_primary_class_wins_over_the_recovery_class(tmp_path):
    from tests.runners.test_cli_runner import _FakeCliRunner
    from agentic_fx.tools.registry import ToolRegistry
    import sys

    class Recovering(_FakeCliRunner):
        def _recover_output(self, mission, stdout_lines, recovery_timeout_sec):
            self._recovery_failure_class = "resource_exhausted"
            return {"answer": 4}

    script = "import sys\nsys.stderr.write('Segmentation fault\\n')\nprint('x')\n"
    runner = Recovering(script=script, bin_path=Path(sys.executable), model="m",
                        workdir=tmp_path, cli_terminate_grace_sec=0.3,
                        registry=ToolRegistry())
    assert runner.run(_mission()).failure_class == "runner_fault"


def test_opencode_resume_sets_the_recovery_class():
    import inspect
    from agentic_fx.runners import opencode_runner
    src = inspect.getsource(opencode_runner)
    assert "self._recovery_failure_class = _detect_stderr_failure_class(" in src


def test_default_is_none():
    from agentic_fx.runners.base import MissionResult
    assert MissionResult("failed", None).failure_class is None


# ---- worker の result frame ----

def test_worker_result_frame_carries_failure_class(monkeypatch, tmp_path):
    from agentic_fx import mission_worker
    from agentic_fx.runners.base import MissionResult
    from agentic_fx.tools.mission_counters import MissionToolCounters
    from tests.test_mission_worker_protocol import (
        _drive_main, _improve_handshake_overrides)

    class _Runner:
        def __init__(self):
            self._afx_mission_counters = MissionToolCounters(budget=100)

        def run(self, mission):
            return MissionResult(status="failed", output=None,
                                 failure_class="resource_exhausted")

    monkeypatch.setattr(mission_worker, "_bootstrap_improve_profile",
                        lambda **kw: None)
    monkeypatch.setattr(mission_worker, "_run_improve_mission",
                        lambda **kw: _Runner())
    frames, _, _ = _drive_main(
        monkeypatch, tmp_path,
        handshake_overrides=_improve_handshake_overrides(
            tmp_path, mission_id="m-failure-class"),
        send_go=True)
    assert frames[-1]["type"] == "result"
    assert frames[-1]["failure_class"] == "resource_exhausted"


def test_worker_result_frame_without_a_class_carries_none(monkeypatch, tmp_path):
    from agentic_fx import mission_worker
    from agentic_fx.runners.base import MissionResult
    from agentic_fx.tools.mission_counters import MissionToolCounters
    from tests.test_mission_worker_protocol import (
        _drive_main, _improve_handshake_overrides)

    class _Runner:
        def __init__(self):
            self._afx_mission_counters = MissionToolCounters(budget=100)

        def run(self, mission):
            return MissionResult(status="completed", output={"a": 1})

    monkeypatch.setattr(mission_worker, "_bootstrap_improve_profile",
                        lambda **kw: None)
    monkeypatch.setattr(mission_worker, "_run_improve_mission",
                        lambda **kw: _Runner())
    frames, _, _ = _drive_main(
        monkeypatch, tmp_path,
        handshake_overrides=_improve_handshake_overrides(
            tmp_path, mission_id="m-failure-class-none"),
        send_go=True)
    assert frames[-1]["failure_class"] is None


# ---- WorkerRunner の復元 ----

def _run_with_frame(tmp_path, monkeypatch, extra):
    from tests.runners.test_worker_runner import (
        SETTINGS, FixedClock, WorkerRunner, _mission as wm, _rag, _root)
    from agentic_fx.runners.worker_runner import write_frame
    import agentic_fx.runners.worker_runner as wr_mod

    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        json.loads(child_in.readline())
        child_in.close()
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        if extra is not None:
            frame = {"type": "result", "seq": 2, "status": "failed", "output": None}
            frame.update(extra)
            write_frame(child_out, frame)
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

    proc = FakeProc()
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)
    runner = WorkerRunner(
        root=_root(tmp_path), settings=SETTINGS,
        clock=FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)),
        rag=_rag(tmp_path))
    t.start()
    result = runner.run(wm())
    t.join(timeout=2.0)
    return result


@pytest.mark.parametrize("value", ["resource_exhausted", "runner_fault"])
def test_worker_runner_restores_the_closed_set(tmp_path, monkeypatch, value):
    assert _run_with_frame(tmp_path, monkeypatch,
                           {"failure_class": value}).failure_class == value


@pytest.mark.parametrize("value", [
    "other", "", "RESOURCE_EXHAUSTED", 1, True, ["resource_exhausted"],
    {"x": 1}, None])
def test_worker_runner_drops_values_outside_the_closed_set(
        tmp_path, monkeypatch, value):
    assert _run_with_frame(tmp_path, monkeypatch,
                           {"failure_class": value}).failure_class is None


def test_worker_runner_old_frame_without_the_key_has_none(tmp_path, monkeypatch):
    assert _run_with_frame(tmp_path, monkeypatch, {}).failure_class is None


def test_worker_runner_eof_has_no_class(tmp_path, monkeypatch):
    result = _run_with_frame(tmp_path, monkeypatch, None)
    assert result.status == "failed"
    assert result.failure_class is None
