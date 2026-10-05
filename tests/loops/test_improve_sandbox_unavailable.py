"""隔離できない失敗 (`sandbox_unavailable`) と load 後の SIGSYS が、改善 agent と
人間の activity にどう届くかを実 worker で確かめる。

- 隔離できない失敗は `worker_sandbox_unavailable`・固定 hint・`started:false`。候補枠と
  CPU 観測には数えず、tool error の streak には入れる。
- load 後の SIGSYS は通常の crash と同じ `worker_crashed`・`started:true`。
- activity の `backtest_cpu` だけが `sandbox_reason` を持つ。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from agentic_fx.config import ImproveToolBudgetSettings
from agentic_fx.core import runtime_fingerprint
from agentic_fx.plugin import sandbox
from agentic_fx.tools.improve_rpc_tools import build_improve_rpc_tooldefs
from agentic_fx.tools.mission_counters import MissionToolCounters
from agentic_fx.tools.registry import ToolRegistry

from tests.fixtures import indicator_wiring as fx
from tests.fixtures.wiring_envs import (
    activity_text as _activity_text,
    improve_env_with_activity as _improve_env_with_activity,
    prepare_ctx as _prepare_ctx,
)

pytestmark = pytest.mark.skipif(sandbox.host_preflight()[0] is not None,
                                reason="this host cannot isolate plugin workers")

_CONFIG = (
    "kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nexit_mode: levels\n"
    "max_bars: 50\nparams: {}\n")
_TEST_PY = "def test_placeholder():\n    pass\n"
_MIN3_TEST_PY = (
    "def test_placeholder_a():\n    pass\n\n\n"
    "def test_placeholder_b():\n    pass\n\n\n"
    "def test_placeholder_c():\n    pass\n")
_HOLD = ("import pandas as pd\n\n\n"
         "def evaluate(df, indicators, signals, params):\n"
         "    return {'action': 'hold', 'rationale': 'flat'}\n")
# load 後に表の外の syscall (getcwd) を呼んで SIGSYS で死ぬ
_SIGSYS = ("import pandas as pd\n\n\n"
           "def evaluate(df, indicators, signals, params):\n"
           "    pd.io.common.os.getcwd()\n"
           "    return {'action': 'hold', 'rationale': 'flat'}\n")
CONTROL = Path(__file__).resolve().parents[1] / "plugin" / "_control_worker.py"


@pytest.fixture(autouse=True)
def _fresh_orphans(monkeypatch):
    monkeypatch.setattr(sandbox, "_ORPHANS", [])
    monkeypatch.setattr(sandbox, "_ORPHAN_OVERFLOW_LOGGED", False)


def _fail_admission(monkeypatch):
    monkeypatch.setattr(sandbox, "_RUNTIME_ADMISSION", runtime_fingerprint.RuntimeAdmission(
        selftest=lambda: runtime_fingerprint.SelftestOutcome(False, "workload_failed"),
        supported=()))
    monkeypatch.setattr(sandbox, "_ADMISSION_RESULT", None)


def _route_to_control(monkeypatch, mode: str):
    real = subprocess.Popen

    def popen(argv, **kwargs):
        if "agentic_fx.plugin.worker" in argv:
            argv = [argv[0], "-B", str(CONTROL), argv[-1], mode]
        return real(argv, **kwargs)

    monkeypatch.setattr(sandbox.subprocess, "Popen", popen)


def _arrange(tmp_path, source: str, test_py: str = _TEST_PY):
    loop, conn, root, activity = _improve_env_with_activity(tmp_path)
    fx.seed_history(conn)
    ctx = _prepare_ctx(loop, now=fx.NOW)
    cand = ctx.staging_dir / "probe"
    cand.mkdir(parents=True)
    (cand / "plugin.py").write_text(source, encoding="utf-8")
    (cand / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (cand / "test_plugin.py").write_text(test_py, encoding="utf-8")
    return loop, conn, ctx, activity


def _registry(ctx, *, max_refusal_streak: int = 10):
    budget = ImproveToolBudgetSettings(max_refusal_streak=max_refusal_streak)
    counters = MissionToolCounters(budget=budget)
    defs = build_improve_rpc_tooldefs(
        ledger=ctx.ledger, run_backtest_handler=ctx.rpc_handlers["run_backtest"],
        analyze_corr_handler=ctx.rpc_handlers["analyze_corr"],
        staging_dir=ctx.staging_dir, counters=counters, budget=budget)
    registry = ToolRegistry(on_execute=counters.record_call,
                            on_result=counters.record_tool_result)
    registry.register_all(defs)
    return registry, counters


def _run(registry):
    return json.loads(registry.execute("run_backtest", {"name": "probe", "pair": "USDJPY"},
                                       allowed=registry.names()))


def _cpu_lines(activity):
    """`backtest_cpu` 行の本文 (activity の 4 列目)。"""
    return [l.split("\t")[3] for l in _activity_text(activity).splitlines()
            if "\tbacktest_cpu\t" in l]


def test_admission_failure_is_started_false_and_only_counts_in_the_error_streak(
        monkeypatch, tmp_path):
    _fail_admission(monkeypatch)
    loop, conn, ctx, activity = _arrange(tmp_path, _HOLD)
    registry, counters = _registry(ctx)

    response = _run(registry)

    assert set(response) <= {"started", "error", "hint", "remaining_budget"}
    assert response["started"] is False
    assert response["error"] == "worker_sandbox_unavailable"
    assert response["hint"].startswith("この環境では候補を安全に実行できません")
    assert sum(counters.backtest_calls.values()) == 0
    assert dict(counters.cpu_limit_observations) == {}
    assert counters.recoverable_refusal_streak[
        ("run_backtest", "tool_error:worker_sandbox_unavailable")] == 1
    ctx.ledger.freeze()
    sinks = (json.dumps(response), json.dumps(ctx.ledger.entries(), default=str))
    for sink in sinks:
        assert "runtime_fingerprint_selftest_failed" not in sink
        assert "sandbox_reason" not in sink
    (line,) = _cpu_lines(activity)
    assert ("cpu_sec=null cpu_source=parent_wait4 result=sandbox_unavailable "
            "returncode=null signal=null "
            "sandbox_reason=runtime_fingerprint_selftest_failed") in line


def test_repeated_sandbox_unavailable_reaches_the_refusal_streak_limit(monkeypatch,
                                                                       tmp_path):
    _fail_admission(monkeypatch)
    loop, conn, ctx, activity = _arrange(tmp_path, _HOLD)
    registry, counters = _registry(ctx, max_refusal_streak=2)

    _run(registry)
    assert counters.abort_pending is False
    _run(registry)
    assert counters.abort_pending is True
    assert counters.abort_trigger == "tool_errors:run_backtest"


def test_startup_timeout_before_load_is_started_false_with_its_reason(monkeypatch,
                                                                      tmp_path):
    monkeypatch.setattr(sandbox, "_STARTUP_TIMEOUT_SEC", 1.5)
    _route_to_control(monkeypatch, "sleep_before_ready")
    loop, conn, ctx, activity = _arrange(tmp_path, _HOLD)
    registry, counters = _registry(ctx)

    response = _run(registry)

    assert (response["started"], response["error"]) == (False, "worker_sandbox_unavailable")
    assert sum(counters.backtest_calls.values()) == 0
    (line,) = _cpu_lines(activity)
    assert "result=sandbox_unavailable" in line
    assert line.endswith("sandbox_reason=sandbox_startup_timeout")


def test_deadline_after_load_is_a_worker_timeout_that_consumes_the_candidate_slot(
        monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "_STARTUP_TIMEOUT_SEC", 3.0)
    _route_to_control(monkeypatch, "sleep_after_load")
    loop, conn, ctx, activity = _arrange(tmp_path, _HOLD)
    registry, counters = _registry(ctx)

    response = _run(registry)

    assert (response["started"], response["error"]) == (True, "worker_timeout")
    assert sum(counters.backtest_calls.values()) == 1
    (line,) = _cpu_lines(activity)
    assert "result=timeout" in line and "sandbox_reason" not in line


def test_sigsys_after_load_is_an_ordinary_crash_with_an_unattributed_reason(tmp_path):
    loop, conn, ctx, activity = _arrange(tmp_path, _SIGSYS)
    registry, counters = _registry(ctx)

    response = _run(registry)

    assert (response["started"], response["error"]) == (True, "worker_crashed")
    assert response["hint"].startswith("worker が異常終了しました")
    assert sum(counters.backtest_calls.values()) == 1
    assert counters.recoverable_refusal_streak[
        ("run_backtest", "tool_error:worker_crashed")] == 1
    assert "sigsys" not in json.dumps(response).lower()
    (line,) = _cpu_lines(activity)
    assert re.search(r"result=crashed returncode=-31 signal=31 "
                     r"sandbox_reason=sigsys_unattributed$", line), line


def test_commit_gate_without_isolation_fails_the_mission_and_keeps_the_candidate(
        monkeypatch, tmp_path):
    """commit gate で隔離不能 (環境側の失敗) になったら、候補の不合格にはしない。
    staging を削除せず、backlog を observation にせず、mission を failed で終端し、
    backlog は再選択可能な open へ戻す (環境が直れば候補を作り直せる)。"""
    from tests.fixtures.wiring_envs import (
        completed_result as _completed_result, mission_for as _mission)

    _fail_admission(monkeypatch)
    loop, conn, ctx, activity = _arrange(tmp_path, _HOLD, test_py=_MIN3_TEST_PY)
    artifact = {
        "discoveries": [{"idea": "idea for probe", "source": "agent",
                         "evidence": "test evidence", "kind": "task"}],
        "selected": {"backlog_id": None, "idea": "idea for probe"},
        "artifact": {"type": "plugin", "name": "probe", "kind": "strategy",
                     "self_test": "passed", "summary": "probe candidate"},
        "selection_rationale": "test rationale"}

    loop.commit(mission=_mission(ctx), ctx=ctx, result=_completed_result(artifact),
                now=fx.NOW)

    text = _activity_text(activity)
    # 候補の不合格 (gate_failed) にはしない。mission を failed で終端する
    assert "gate_failed" not in text
    assert ("mission_failed" in text
            and "reason=gate_worker_sandbox_unavailable" in text)
    # sandbox_reason は activity (技術ログ) にだけ出る
    assert "sandbox_reason=runtime_fingerprint_selftest_failed" in text
    # backtest_cpu 行は従来どおり sink が残す
    lines = [l for l in _cpu_lines(activity) if "plugin=probe" in l]
    assert len(lines) == 1
    assert lines[0].endswith(
        "result=sandbox_unavailable returncode=null signal=null "
        "sandbox_reason=runtime_fingerprint_selftest_failed")
    # mission は failed
    assert conn.execute(
        "SELECT status FROM missions WHERE id=?", (ctx.mission_id,)
    ).fetchone()[0] == "failed"
    # backlog は observation にせず、再選択可能な open へ戻す (sandbox_reason は漏らさない)
    statuses = conn.execute("SELECT status, last_result FROM improvement_backlog").fetchall()
    assert statuses, "discovery backlog row must exist"
    assert all(s[0] != "observation" for s in statuses)
    assert any(s[0] == "open" for s in statuses)
    assert all(s[1] is None or "sandbox" not in s[1] for s in statuses)
    # 候補の staging は残す
    assert ctx.staging_dir.exists()
