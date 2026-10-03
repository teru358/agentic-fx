"""実 worker を通した run_backtest の失敗が、公開分類まで届くことを確かめる。

fake の intent source と差し替えた run_in_sample では、worker の実際の死因
(CPU 上限の SIGKILL・親 kill・例外・異常終了) が公開分類へ写像されるところまで
は踏めない。ここでは一時ディレクトリに小さな strategy plugin を置き、
本物の worker プロセスを起こす。
"""
from __future__ import annotations

import json
import logging
import re

import pytest

from agentic_fx.config import ImproveToolBudgetSettings
from agentic_fx.tools.improve_rpc_tools import build_improve_rpc_tooldefs
from agentic_fx.tools.mission_counters import MissionToolCounters
from agentic_fx.tools.registry import ToolRegistry

from tests.fixtures import indicator_wiring as fx
from tests.fixtures.wiring_envs import (
    activity_text as _activity_text,
    improve_env_with_activity as _improve_env_with_activity,
    prepare_ctx as _prepare_ctx,
)

_MARKER = "PLUGIN-STDERR-MARKER-9c1e4b"

_CONFIG = (
    "kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nexit_mode: levels\n"
    "max_bars: 50\nparams: {}\n")
_TEST_PY = "def test_placeholder():\n    pass\n"

_HEAD = f"""
import pandas as pd


def evaluate(df, indicators, signals, params):
    print({_MARKER!r}, flush=True)
"""

_PLUGINS = {
    "cpu_limit": _HEAD + "    n = 0\n    while True:\n        n += 1\n",
    "timeout": _HEAD + "    n = 0\n    while True:\n        n += 1\n",
    "plugin_error": _HEAD + "    raise ValueError('plugin says no')\n",
    # sandbox の import 制限では os を直接 import できない。pandas が既に
    # 読み込んでいる os を経由して、worker を異常終了させる。
    "crashed": _HEAD + "    pd.io.common.os._exit(3)\n",
}

# (plugin の種類, 設定の上書き, 公開 error, activity の result)
_CASES = [
    ("cpu_limit", {"sandbox_session_cpu_sec": 1, "sandbox_timeout_sec": 8.0},
     "worker_cpu_limit", "cpu_limit"),
    ("timeout", {"sandbox_session_cpu_sec": 30, "sandbox_timeout_sec": 1.0},
     "worker_timeout", "timeout"),
    ("plugin_error", {}, "backtest_failed", "plugin_error"),
    ("crashed", {}, "worker_crashed", "crashed"),
]


def _arrange(tmp_path, kind, updates):
    loop, conn, root, activity = _improve_env_with_activity(tmp_path)
    fx.seed_history(conn)
    loop._settings = loop._settings.model_copy(update={
        "plugin": loop._settings.plugin.model_copy(update=updates)})
    ctx = _prepare_ctx(loop, now=fx.NOW)
    cand = ctx.staging_dir / "probe"
    cand.mkdir(parents=True)
    (cand / "plugin.py").write_text(_PLUGINS[kind], encoding="utf-8")
    (cand / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (cand / "test_plugin.py").write_text(_TEST_PY, encoding="utf-8")
    return loop, ctx, activity


def _call_through_registry(ctx):
    budget = ImproveToolBudgetSettings()
    counters = MissionToolCounters(budget=budget)
    defs = build_improve_rpc_tooldefs(
        ledger=ctx.ledger, run_backtest_handler=ctx.rpc_handlers["run_backtest"],
        analyze_corr_handler=ctx.rpc_handlers["analyze_corr"],
        staging_dir=None, counters=counters, budget=budget)
    registry = ToolRegistry(on_result=counters.record_tool_result)
    registry.register_all(defs)
    raw = registry.execute("run_backtest", {"name": "probe", "pair": "USDJPY"},
                           allowed=registry.names())
    ctx.ledger.freeze()
    return raw, ctx.ledger.entries()


@pytest.mark.parametrize("kind,updates,public,result", _CASES)
def test_real_worker_death_reaches_the_agent_only_as_a_fixed_public_error(
        tmp_path, caplog, kind, updates, public, result):
    caplog.set_level(logging.INFO, logger="agentic_fx.plugin.sandbox")
    loop, ctx, activity = _arrange(tmp_path, kind, updates)

    raw, entries = _call_through_registry(ctx)

    response = json.loads(raw)
    assert response["started"] is True
    assert response["error"] == public
    assert set(response) <= {"started", "error", "hint", "remaining_budget"}
    assert isinstance(response["hint"], str) and response["hint"]
    assert entries[0]["result_summary"]["error"] == public

    (line,) = [l for l in _activity_text(activity).splitlines()
               if "backtest_cpu" in l]
    assert f"result={result}" in line
    assert "cpu_source=parent_wait4" in line
    assert re.search(r"cpu_sec=\d+(\.\d+)?(\s|$)", line), line

    # plugin が stderr に書いた文字列は、agent に届く面・activity のどこにも
    # 出ない。出てよいのは人間向けの技術ログだけ。
    for sink in (raw, json.dumps(entries, default=str), _activity_text(activity)):
        assert _MARKER not in sink
    # 例外文字列・returncode・signal・CPU 値は、agent に届く応答と台帳に出ない。
    for sink in (raw, json.dumps(entries, default=str)):
        for text in ("plugin says no", "timed out", "exited unexpectedly",
                     "returncode", "signal", "cpu_sec"):
            assert text not in sink
        assert not re.search(r"\d+\.\d{3,}", sink)
    assert any(_MARKER in r.getMessage() for r in caplog.records
               if r.name.endswith("sandbox"))


def test_cpu_limit_death_reports_the_parent_observed_classification(
        tmp_path):
    loop, ctx, activity = _arrange(
        tmp_path, "cpu_limit",
        {"sandbox_session_cpu_sec": 1, "sandbox_timeout_sec": 8.0})

    out = ctx.rpc_handlers["run_backtest"](
        {"name": "probe", "pair": "USDJPY"})

    assert out["started"] is True and out["error"] == "worker_cpu_limit"
    (line,) = [l for l in _activity_text(activity).splitlines()
               if "backtest_cpu" in l]
    assert "result=cpu_limit" in line and "cpu_source=parent_wait4" in line
    assert "signal=9" in line


# --- commit gate 経路 ------------------------------------------------------

_MIN3_TEST_PY = (
    "def test_placeholder_a():\n    pass\n\n\n"
    "def test_placeholder_b():\n    pass\n\n\n"
    "def test_placeholder_c():\n    pass\n")

_GATE_PLUGINS = dict(_PLUGINS)
# 評価が最後まで走る plugin (hold を返し続ける)。
_GATE_PLUGINS["ok"] = (
    "import pandas as pd\n\n\n"
    "def evaluate(df, indicators, signals, params):\n"
    "    return {'action': 'hold', 'rationale': 'flat'}\n")


def _commit_with(tmp_path, kind, updates):
    from tests.fixtures.wiring_envs import (
        completed_result as _completed_result, mission_for as _mission)
    loop, conn, root, activity = _improve_env_with_activity(tmp_path)
    fx.seed_history(conn)
    loop._settings = loop._settings.model_copy(update={
        "plugin": loop._settings.plugin.model_copy(update=updates)})
    ctx = _prepare_ctx(loop, now=fx.NOW)
    cand = ctx.staging_dir / "probe"
    cand.mkdir(parents=True)
    (cand / "plugin.py").write_text(_GATE_PLUGINS[kind], encoding="utf-8")
    (cand / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (cand / "test_plugin.py").write_text(_MIN3_TEST_PY, encoding="utf-8")
    artifact = {
        "discoveries": [{"idea": "idea for probe", "source": "agent",
                         "evidence": "test evidence", "kind": "task"}],
        "selected": {"backlog_id": None, "idea": "idea for probe"},
        "artifact": {"type": "plugin", "name": "probe", "kind": "strategy",
                     "self_test": "passed", "summary": "probe candidate"},
        "selection_rationale": "test rationale"}
    error = None
    try:
        loop.commit(mission=_mission(ctx), ctx=ctx,
                    result=_completed_result(artifact), now=fx.NOW)
    except Exception as exc:  # noqa: BLE001 — 例外の有無も観測対象
        error = exc
    return loop, conn, root, activity, error, ctx


def _gate_lines(activity):
    return [l for l in _activity_text(activity).splitlines()
            if "backtest_cpu" in l and "plugin=probe" in l]


@pytest.mark.parametrize("kind,updates,result", [
    (k, u, r) for k, u, _p, r in _CASES])
def test_commit_gate_writes_one_backtest_cpu_line_when_the_worker_fails(
        tmp_path, kind, updates, result):
    loop, conn, root, activity, error, _ctx = _commit_with(tmp_path, kind, updates)

    lines = _gate_lines(activity)
    assert len(lines) == 1, (lines, error)
    line = lines[0]
    assert "scope=in_sample pair=USDJPY deps=0 cpu_sec=" in line
    assert f"cpu_source=parent_wait4 result={result} " in line
    assert re.search(r"cpu_sec=\d+(\.\d+)?\s", line), line
    assert re.search(r"returncode=-?\d+ signal=(null|\d+)", line), line
    assert _MARKER not in _activity_text(activity)


def test_commit_gate_line_for_a_completed_evaluation_has_the_same_format(
        tmp_path):
    loop, conn, root, activity, error, _ctx = _commit_with(tmp_path, "ok", {})

    lines = _gate_lines(activity)
    assert lines, error
    assert all(re.search(
        r"scope=(in_sample|holdout) pair=USDJPY deps=0 cpu_sec=\d+(\.\d+)? "
        r"cpu_source=parent_wait4 result=ok returncode=0 signal=null", l)
        for l in lines), lines


# --- 実測 CPU 上限から 3 回目の拒否まで ----------------------------------------

def test_real_cpu_limit_twice_then_the_third_identical_run_is_refused(tmp_path):
    """本物の worker が CPU 上限で 2 回死ぬと、各回が activity に親観測の
    `backtest_cpu result=cpu_limit` を残し、agent には `worker_cpu_limit` だけが
    届く。同じ内容・pair の 3 回目は worker を起こさずに拒否される。"""
    loop, ctx, activity = _arrange(
        tmp_path, "cpu_limit",
        {"sandbox_session_cpu_sec": 1, "sandbox_timeout_sec": 8.0})
    budget = ImproveToolBudgetSettings()
    counters = MissionToolCounters(budget=budget)
    defs = build_improve_rpc_tooldefs(
        ledger=ctx.ledger, run_backtest_handler=ctx.rpc_handlers["run_backtest"],
        analyze_corr_handler=ctx.rpc_handlers["analyze_corr"],
        staging_dir=ctx.staging_dir, counters=counters, budget=budget)
    registry = ToolRegistry(on_execute=counters.record_call,
                            on_result=counters.record_tool_result)
    registry.register_all(defs)

    raws = []
    lines_after = []
    for _ in range(3):
        raws.append(registry.execute(
            "run_backtest", {"name": "probe", "pair": "USDJPY"},
            allowed=registry.names()))
        lines_after.append([l for l in _activity_text(activity).splitlines()
                            if "backtest_cpu" in l])
    ctx.ledger.freeze()
    entries = ctx.ledger.entries()
    responses = [json.loads(r) for r in raws]

    assert [r["error"] for r in responses] == [
        "worker_cpu_limit", "worker_cpu_limit", "repeated_worker_cpu_limit"]
    assert [r["started"] for r in responses] == [True, True, False]
    # 1 回目・2 回目はそれぞれ親観測の CPU 上限として 1 行ずつ残る。
    assert [len(x) for x in lines_after] == [1, 2, 2]
    for line in lines_after[1]:
        assert "result=cpu_limit" in line
        assert "cpu_source=parent_wait4" in line
        assert "signal=9" in line
        assert re.search(r"cpu_sec=\d+(\.\d+)?\s", line), line
    # 3 回目は worker を起こさず、候補枠も消費しない。
    assert counters.backtest_calls["probe"] == 2
    assert counters.total_calls == 3 and counters.errors == 3
    # activity は人間向けだが、stderr とその有無は技術ログにしか出ない。
    assert _MARKER not in _activity_text(activity)
    assert "stderr" not in _activity_text(activity)
    assert [e["result_summary"]["error"] for e in entries] == [
        r["error"] for r in responses]
    for sink in raws + [json.dumps(entries, default=str)]:
        assert _MARKER not in sink
        for text in ("returncode", "signal", "cpu_sec", "parent_wait4",
                     "stderr", "pid"):
            assert text not in sink
        assert not re.search(r"\d+\.\d{2,}", sink)
