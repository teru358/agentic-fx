"""shadow / enforce の同形、mission 開始時の mode 固定、壁時計の上限、予算を、
実 worker・実 DB で確かめる。"""
from __future__ import annotations

import json
import re

import pytest

from tests.fixtures import baseline_world as bw

pytestmark = pytest.mark.slow

SAME = dict(take_profit=0.6, stop_loss=0.3)


def _same(note):
    return bw.candidate_files(py=bw.strategy_py(**SAME, note=note))


def _baseline_rows(snap):
    return [r for r in snap.rows if r["variant"] == "baseline"]


def _compare_line(snap):
    (line,) = [l for l in snap.activity if "\tbaseline_compare\t" in l]
    return line


def _shape(snap):
    """行の同形を比べるための射影 (mission_outcome と id 以外の全列の写し)。"""
    return [(r["variant"], r["scope"], r["pair"], r["plugin_ref"],
             r["content_hash"], r["ref_content_hash"], r["metrics_json"])
            for r in snap.rows]


# --- shadow は enforce と同形で、却下しない ----------------------------------------

def test_shadow_records_the_same_rows_as_enforce_but_does_not_reject(tmp_path):
    enforce, deployed, _ = bw.run_scenario(
        tmp_path / "enforce", _same("a"), mode="enforce")
    shadow, deployed2, _ = bw.run_scenario(
        tmp_path / "shadow", _same("a"), mode="shadow")
    assert deployed == deployed2
    # enforce は却下、shadow は同じ値で承認へ進む
    assert enforce.notice == ("gate_failed", "not_improved")
    assert enforce.approvals == []
    assert shadow.notice == ("submitted", None)
    assert len(shadow.approvals) == 1
    # 行・比較値・activity は同形 (mode と outcome の語だけが違う)
    assert _shape(shadow) == _shape(enforce)
    assert {r["mission_outcome"] for r in _baseline_rows(shadow)} == {"approval"}
    assert {r["mission_outcome"] for r in _baseline_rows(enforce)} \
        == {"not_improved"}
    comparison = shadow.approvals[0]["baseline"]["comparison"]
    assert comparison["mode"] == "shadow"
    assert (comparison["verdict"], comparison["reason"]) == (
        "not_improved", "holdout_not_exceeds")
    line = _compare_line(shadow)
    assert " mode=shadow result=not_improved detail=holdout_not_exceeds " in line
    assert re.sub(r"mode=\w+", "mode=X", _compare_line(shadow).split("\t", 3)[3]
                  ).replace(str(shadow.mission_id), "N") == re.sub(
        r"mode=\w+", "mode=X", _compare_line(enforce).split("\t", 3)[3]
    ).replace(str(enforce.mission_id), "N")


# --- mode は mission 開始時に固定 ------------------------------------------------------

def test_the_mode_is_fixed_when_the_mission_starts(tmp_path):
    """mission の途中で settings が変わっても、始めた mission の mode は変わらない。"""
    with bw.open_world(tmp_path / "w", mode="shadow") as world:
        bw.deploy(world)

        def flip_to(mode):
            def hook(ctx):
                world.settings.improve.baseline_gate = mode
            return hook

        # shadow で始めた mission は、途中で enforce に変わっても却下されない
        first = bw.run_manual(world, _same("a"), after_stage=flip_to("enforce"))
        a = bw.snapshot(world, first)
        assert world.settings.improve.baseline_gate == "enforce"
        assert a.notice == ("submitted", None), a.notice
        assert "mode=shadow result=not_improved" in _compare_line(a)
        # 次の mission は enforce で始まり、途中で shadow に戻っても却下される
        second = bw.run_manual(world, _same("b"), after_stage=flip_to("shadow"))
        b = bw.snapshot(world, second)
        assert world.settings.improve.baseline_gate == "shadow"
        assert b.notice == ("gate_failed", "not_improved"), b.notice
        assert [l for l in b.activity if "\tbaseline_compare\t" in l
                and f"mission={second} " in l and "mode=enforce" in l]


# --- 壁時計 (実 worker で実測) -------------------------------------------------------

def test_a_replay_over_the_wall_clock_limit_is_cancelled_and_falls_to_unavailable(
        tmp_path):
    """上限は床を通った後から数える。候補の床 (十数秒) は上限より長くても
    打ち切られず、再生だけが取り消される。"""
    limit = 5.0
    calls: list[dict] = []
    with bw.open_world(tmp_path / "w", mode="enforce",
                       wall_clock_max_sec=limit) as world:
        deployed = bw.deploy(world)
        with bw.timed_backtests(calls):
            mission = bw.run_manual(world, _same("a"))
        snap = bw.snapshot(world, mission)
    candidate_calls = [c for c in calls if c["content_hash"] != deployed]
    replay_calls = [c for c in calls if c["content_hash"] == deployed]
    floor_seconds = sum(c["seconds"] for c in candidate_calls)
    print("measured:", [(c["scope"], c["content_hash"] == deployed,
                         round(c["seconds"], 2), c["raised"]) for c in calls])
    # 候補の床は上限を超えても完走する (in_sample と holdout が両方残る)
    assert floor_seconds > limit
    assert [c["raised"] for c in candidate_calls] == [None, None]
    assert [r["scope"] for r in snap.rows if r["variant"] == "candidate"] == [
        "in_sample", "holdout_gate"]
    # 再生は 1 回だけ始まり、上限の近くで取り消される (床の前に数えていれば 0 秒)
    assert len(replay_calls) == 1 and replay_calls[0]["scope"] == "in_sample"
    assert replay_calls[0]["raised"] is not None
    assert limit * 0.8 <= replay_calls[0]["seconds"] < limit * 3
    # 結果は床のみで通り、baseline 行は 0、比較は gate_timeout の unavailable
    assert snap.notice == ("submitted", None)
    assert _baseline_rows(snap) == []
    (payload,) = snap.approvals
    comparison = payload["baseline"]["comparison"]
    assert (comparison["verdict"], comparison["reason"]) == (
        "unavailable", "gate_timeout")
    line = _compare_line(snap)
    assert "result=unavailable detail=gate_timeout" in line
    # 取り消しは失敗ではないので、sink にも CPU の行にも出ない
    assert not [l for l in snap.activity if "scope=baseline_" in l]


def test_a_replay_inside_the_limit_is_not_cut_and_measures_the_real_cost(
        tmp_path):
    calls: list[dict] = []
    with bw.open_world(tmp_path / "w", mode="enforce") as world:
        deployed = bw.deploy(world)
        with bw.timed_backtests(calls):
            mission = bw.run_manual(world, _same("a"))
        snap = bw.snapshot(world, mission)
    replay = [c for c in calls if c["content_hash"] == deployed]
    print("measured replay seconds:", [(c["scope"], round(c["seconds"], 2))
                                       for c in replay])
    assert [c["scope"] for c in replay] == ["in_sample", "holdout"]
    assert all(c["raised"] is None for c in replay)
    assert sum(c["seconds"] for c in replay) < 300 / 2     # 既定の上限の半分以内
    assert snap.approvals == [] and snap.notice == ("gate_failed", "not_improved")


# --- 予算 ----------------------------------------------------------------------------

def test_the_backtest_budget_does_not_change_with_a_comparison(tmp_path):
    """agent の run_backtest を 1 回呼んだ mission で、比較のある同名更新と
    比較のない新名の提出は、同じ消費を承認 payload に残す。"""
    returns = {}

    def agent_calls_run_backtest(label):
        def hook(ctx):
            returns[label] = bw.agent_run_backtest(
                ctx, bw.NAME if label == "compared" else "fresh")
        return hook

    with bw.open_world(tmp_path / "w", mode="enforce") as world:
        bw.deploy(world)
        compared = bw.run_manual(
            world, bw.candidate_files(
                py=bw.strategy_py(take_profit=0.8, stop_loss=0.3)),
            after_stage=agent_calls_run_backtest("compared"))
        fresh = bw.run_manual(
            world, bw.candidate_files(
                name="fresh", py=bw.strategy_py(min_close=151.0)),
            after_stage=agent_calls_run_backtest("fresh"))
        a, b = bw.snapshot(world, compared), bw.snapshot(world, fresh)
    assert len(a.approvals) == 1 and len(b.approvals) == 1
    pa, pb = a.approvals[0], b.approvals[0]
    assert "comparison" in pa["baseline"]
    assert "comparison" not in (pb["baseline"] or {})
    for key in ("backtest_call_count", "trial_count", "analysis_call_count"):
        assert pa[key] == pb[key], key
    assert pa["backtest_call_count"] == 1
    # 比較の再生は agent の RPC の消費に入らず、行は baseline の分だけ増える
    assert len(_baseline_rows(a)) == 2 and _baseline_rows(b) == []
    # agent が受け取る run_backtest の返却に、比較の語も配備中の版の数値も無い
    deployed_metrics = [json.loads(r["metrics_json"]) for r in a.rows
                        if r["variant"] == "baseline"]
    text = json.dumps(returns, ensure_ascii=False, default=str)
    for word in ("baseline", "not_improved", "same_name_contract_changed",
                 "holdout", "holdout_gate", "deployed", "improved"):
        assert word not in text, word
    for metrics in deployed_metrics:
        assert repr(metrics["pf"]) not in text
        assert repr(metrics["avg_r"]) not in text
