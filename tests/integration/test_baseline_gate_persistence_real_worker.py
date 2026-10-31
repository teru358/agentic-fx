"""比較 gate の永続化の規則を、実 worker・実 DB で確かめる。

床が先 / 契約が最優先 / 対象外の 3 経路で baseline 行が増えないこと、
配備中の版の再生が失敗したときの `SandboxError` の全 code の振り分け、
再生失敗と標本不足の 2 種類の `unavailable` の区別。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from agentic_fx.backtest import holdout
from agentic_fx.plugin.sandbox import SandboxError
from tests.fixtures import baseline_world as bw
from tests.fixtures import indicator_wiring as fx

pytestmark = pytest.mark.slow

SAME_NAME = "same_name_contract_changed"


def _baseline_rows(snap):
    return [r for r in snap.rows if r["variant"] == "baseline"]


def _candidate_rows(snap):
    return [r for r in snap.rows if r["variant"] == "candidate"]


def _compare_lines(snap):
    return [l for l in snap.activity if "\tbaseline_compare\t" in l]


# --- 床が先 ----------------------------------------------------------------------

def test_a_candidate_below_the_floor_never_replays_the_deployed_version(
        tmp_path):
    snap, _deployed, _ = bw.run_scenario(
        tmp_path / "w", bw.candidate_files(py=bw.strategy_py(min_close=151.6)))
    assert snap.status == "completed"
    assert snap.notice == ("gate_failed", "unprofitable")
    assert _baseline_rows(snap) == []
    assert [(r["scope"], r["mission_outcome"]) for r in _candidate_rows(snap)] \
        == [("in_sample", "unprofitable")]
    assert not any("scope=baseline_" in l for l in snap.activity)
    assert _compare_lines(snap) == []
    assert snap.approvals == []


def test_a_candidate_failing_only_the_holdout_floor_never_replays_either(
        tmp_path):
    """in_sample を通り holdout の床で落ちる候補 (holdout の標本不足を床に
    している設定) も、配備中の版を回さず baseline 行を残さない。"""
    snap, _deployed, _ = bw.run_scenario(
        tmp_path / "w",
        bw.candidate_files(py=bw.strategy_py(max_close=148.4)),
        gate_updates={"require_holdout_evaluable": True})
    assert snap.status == "completed"
    assert snap.notice == ("gate_failed", "unprofitable")
    assert [(r["scope"], r["mission_outcome"]) for r in _candidate_rows(snap)] \
        == [("in_sample", "unprofitable"), ("holdout_gate", "unprofitable")]
    assert _baseline_rows(snap) == []
    assert not any("scope=baseline_" in l for l in snap.activity)
    assert _compare_lines(snap) == []


# --- 契約が床より優先 ------------------------------------------------------------

@pytest.mark.parametrize("mode", ["shadow", "enforce"])
@pytest.mark.parametrize("change", ["timeframe", "pair_superset"])
def test_a_contract_change_wins_over_a_floor_failure_and_runs_no_backtest(
        tmp_path, mode, change):
    config = (bw.CONFIG.replace("timeframe: 1h", "timeframe: 4h")
              if change == "timeframe"
              else bw.CONFIG.replace("[USDJPY]", "[USDJPY, EURUSD]"))
    snap, _deployed, _ = bw.run_scenario(
        tmp_path / "w",
        bw.candidate_files(py=bw.strategy_py(min_close=151.6), config=config),
        mode=mode)
    assert snap.status == "completed"
    assert snap.notice == ("gate_failed", SAME_NAME)
    assert snap.rows == []                      # 候補の backtest も 0 行
    assert snap.approvals == []
    assert snap.backlog == [{"last_result": "observation:not_submitted",
                             "origin_outcome": "not_submitted"}]
    assert any(l.endswith(f"mission={snap.mission_id} reason={SAME_NAME}\t-")
               for l in snap.activity)
    assert "意味が変わる変更" in snap.report
    assert _compare_lines(snap) == []
    assert not any("\tbacktest_cpu\t" in l for l in snap.activity)


# --- 対象外: 新名 / 人間回廊 / 再ロック -----------------------------------------------

def test_a_new_name_is_floor_only_and_adds_no_baseline_row(tmp_path):
    snap, _deployed, _ = bw.run_scenario(
        tmp_path / "w",
        bw.candidate_files(name="fresh", py=bw.strategy_py(note="fresh")))
    assert snap.status == "completed"
    assert len(snap.approvals) == 1
    assert "comparison" not in (snap.approvals[0]["baseline"] or {})
    assert _baseline_rows(snap) == []
    assert sorted({r["variant"] for r in snap.rows}) == [
        "candidate", "no_strategy"]
    assert _compare_lines(snap) == []


def test_the_human_corridor_never_compares(tmp_path):
    """人間の `submit` は同名の配備中の版があっても、再生せず baseline 行も作らない。"""
    from agentic_fx.plugin import switch as plugin_switch
    with bw.open_world(tmp_path / "w", mode="enforce") as world:
        deployed = bw.deploy(world)
        human = world.root / "plugins" / "_human" / bw.NAME
        human.mkdir(parents=True)
        files = bw.candidate_files(
            py=bw.strategy_py(take_profit=0.8, stop_loss=0.3))
        (human / "plugin.py").write_text(files[1], encoding="utf-8")
        (human / "config.yaml").write_text(files[2], encoding="utf-8")
        (human / "test_plugin.py").write_text(files[3], encoding="utf-8")
        calls: list[dict] = []
        with bw.timed_backtests(calls):
            approval_id = plugin_switch.submit_candidate(
                world.conn, name=bw.NAME,
                staging_dir=world.root / "plugins" / "_human",
                candidate_origin="human", mission_id=None, backlog_id=None,
                settings=world.settings, now=bw.NOW,
                activity=world.app.activity)
        # 候補だけが回り、配備中の版の backtest は 1 回も起きない
        assert calls and all(c["content_hash"] != deployed for c in calls)
        rows = world.conn.execute(
            "SELECT variant, count(*) FROM backtest_runs GROUP BY variant"
        ).fetchall()
        assert all(r[0] != "baseline" for r in rows), [tuple(r) for r in rows]
        payload = json.loads(world.conn.execute(
            "SELECT payload_json FROM approval_requests WHERE id=?",
            (approval_id,)).fetchone()[0])
        assert "comparison" not in (payload.get("baseline") or {})
        assert not any("\tbaseline_compare\t" in l
                       for l in world.app.activity.tail(10_000))
        assert not any("scope=baseline_" in l
                       for l in world.app.activity.tail(10_000))


def test_a_relock_never_compares(tmp_path):
    """依存 indicator の pin だけを新しい版に張り替える候補 (再ロック) は、
    比較を受けない。"""
    import yaml
    from tests.fixtures.wiring_envs import bump_indicator_version
    with bw.open_world(tmp_path / "w", mode="enforce") as world:
        plugins = world.root / "plugins"
        fx.write_indicator(plugins, "rsi")
        pins = fx.deploy_approved(world.conn, plugins, ["rsi"], now=bw.NOW)

        def config(pin):
            return bw.CONFIG + yaml.safe_dump({"indicators": {"rsi": {
                "plugin": "rsi", "params": {"period": 14}, "pin": pin}}})

        deployed = bw.deploy(world, config=config(pins["rsi"]))
        new_pin = bump_indicator_version(world.conn, plugins, "rsi",
                                         now=bw.NOW)
        assert new_pin != pins["rsi"]
        mission = bw.run_manual(world, bw.candidate_files(config=config(new_pin)))
        snap = bw.snapshot(world, mission)
    assert snap.notice == ("submitted", None), snap.notice
    assert snap.status == "completed"
    assert _baseline_rows(snap) == []
    assert _compare_lines(snap) == []
    assert not any("scope=baseline_" in l for l in snap.activity)
    assert deployed not in {r["content_hash"] for r in snap.rows}


# --- SandboxError の全 code ------------------------------------------------------

_HEAD = "import pandas as pd\n\n\ndef evaluate(df, indicators, signals, params):\n"
HOSTILE = {
    "plugin_error": _HEAD + "    raise ValueError('plugin says no')\n",
    # sandbox の import 制限では os を直接 import できない。pandas が
    # 読み込み済みの os を経由して worker を異常終了させる。
    "crashed": _HEAD + "    pd.io.common.os._exit(3)\n",
    "backtest_failed": "import os\n\n" + _HEAD
    + "    return {'action': 'hold', 'rationale': 'x'}\n",
    "timeout": _HEAD + "    n = 0\n    while True:\n        n += 1\n",
    "cpu_limit": _HEAD + "    n = 0\n    while True:\n        n += 1\n",
}
SETTINGS = {
    "timeout": {"sandbox_session_cpu_sec": 120, "sandbox_timeout_sec": 2.0},
    "cpu_limit": {"sandbox_session_cpu_sec": 3, "sandbox_timeout_sec": 30.0},
}


def _assert_unavailable_floor_only(snap, reason="baseline_unhealthy"):
    """再生失敗: 床だけで通り、baseline 行は 0、候補は完全な pair x 段。"""
    assert snap.status == "completed"
    assert snap.notice == ("submitted", None)
    assert _baseline_rows(snap) == []
    assert [(r["scope"], r["mission_outcome"]) for r in _candidate_rows(snap)] \
        == [("in_sample", "approval"), ("holdout_gate", "approval")]
    (payload,) = snap.approvals
    comparison = payload["baseline"]["comparison"]
    assert (comparison["verdict"], comparison["reason"]) == (
        "unavailable", reason)
    assert comparison["stages"] == {"holdout": {}, "in_sample": {}}
    (line,) = _compare_lines(snap)
    assert f"result=unavailable detail={reason}" in line


@pytest.mark.parametrize("code,expected_result", [
    ("plugin_error", "plugin_error"),
    ("crashed", "crashed"),
    ("backtest_failed", None),
    ("timeout", "timeout"),
    ("cpu_limit", "cpu_limit"),
])
def test_a_real_worker_failure_of_the_deployed_version_falls_to_unavailable(
        tmp_path, code, expected_result):
    snap, _deployed, _ = bw.run_scenario(
        tmp_path / "w", bw.candidate_files(),
        deployed_py=HOSTILE[code], plugin_updates=SETTINGS.get(code),
        mode="shadow")
    _assert_unavailable_floor_only(snap)
    (sink,) = [l for l in snap.activity
               if "\tbacktest_cpu\t" in l and "scope=baseline_in_sample" in l]
    print(code, sink.split("\t")[3])
    if expected_result is not None:
        assert f"result={expected_result} " in sink
    if code == "cpu_limit":
        assert "signal=9" in sink


@contextmanager
def _inject(code, deployed_hash, reason=None, stage="in_sample"):
    """配備中の版の `stage` の backtest だけが `code` で落ちる。
    候補と他の段は本物の worker のまま通す。"""
    name = "run_in_sample" if stage == "in_sample" else "run_holdout_gate"
    real = getattr(holdout, name)

    def wrapped(*args, **kwargs):
        if kwargs.get("content_hash") == deployed_hash:
            raise SandboxError("injected", code=code, sandbox_reason=reason)
        return real(*args, **kwargs)

    with patch(f"agentic_fx.backtest.holdout.{name}", wrapped):
        yield


def test_protocol_error_of_the_deployed_version_falls_to_unavailable(tmp_path):
    snap, _deployed, _ = bw.run_scenario(
        tmp_path / "w", bw.candidate_files(py=bw.strategy_py(note="c")),
        mode="enforce", wrap=lambda h: _inject("protocol_error", h))
    _assert_unavailable_floor_only(snap)


def test_a_failure_at_the_holdout_stage_drops_the_baseline_in_sample_row_too(
        tmp_path):
    """再生が in_sample を終えた後の holdout で落ちたとき、先に得た baseline の
    in_sample 行も残さない (片側だけの行は読み手に誤った比較を見せる)。"""
    snap, _deployed, _ = bw.run_scenario(
        tmp_path / "w", bw.candidate_files(py=bw.strategy_py(note="c")),
        mode="enforce",
        wrap=lambda h: _inject("protocol_error", h, stage="holdout"))
    _assert_unavailable_floor_only(snap)
    assert any("scope=baseline_in_sample" in l and "result=ok" in l
               for l in snap.activity)         # in_sample は実際に回っていた


def test_sandbox_unavailable_during_the_replay_fails_the_mission_retryably(
        tmp_path):
    snap, _deployed, _ = bw.run_scenario(
        tmp_path / "w", bw.candidate_files(py=bw.strategy_py(note="c")),
        mode="enforce",
        wrap=lambda h: _inject("sandbox_unavailable", h, "host_preflight"))
    assert snap.status == "failed"
    assert snap.notice == ("failed", "sandbox_unavailable")
    assert snap.approvals == []
    assert _baseline_rows(snap) == []
    assert _compare_lines(snap) == []


def test_an_unknown_code_fails_closed_without_inventing_a_comparison(tmp_path):
    snap, _deployed, error = bw.run_scenario(
        tmp_path / "w", bw.candidate_files(py=bw.strategy_py(note="c")),
        mode="enforce", wrap=lambda h: _inject("mystery_code", h),
        expect_error=SandboxError)
    assert error.code == "mystery_code"
    assert snap.status == "failed"
    assert snap.approvals == []
    assert snap.rows == []
    assert _compare_lines(snap) == []


# --- 標本不足は完全な baseline 行つきの unavailable ---------------------------------

def test_a_deployed_holdout_below_30_trades_keeps_complete_baseline_rows(
        tmp_path):
    thin = bw.strategy_py(max_close=148.4)       # holdout 12 取引 (評価不能)
    snap, deployed, _ = bw.run_scenario(
        tmp_path / "w", bw.candidate_files(), deployed_py=thin, mode="enforce")
    assert snap.status == "completed"
    assert snap.notice == ("submitted", None)
    (payload,) = snap.approvals
    comparison = payload["baseline"]["comparison"]
    assert comparison["verdict"] == "unavailable"
    assert comparison["reason"] == "baseline_not_evaluable"
    base = _baseline_rows(snap)
    assert [(r["scope"], r["mission_outcome"], r["content_hash"]) for r in base] \
        == [("in_sample", "approval", deployed),
            ("holdout_gate", "approval", deployed)]
    holdout_row = [r for r in base if r["scope"] == "holdout_gate"][0]
    assert json.loads(holdout_row["metrics_json"])["evaluable"] is False
    assert [r["scope"] for r in _candidate_rows(snap)] == [
        "in_sample", "holdout_gate"]
    (line,) = _compare_lines(snap)
    assert "result=unavailable detail=baseline_not_evaluable" in line
