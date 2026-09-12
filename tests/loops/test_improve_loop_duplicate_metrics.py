"""approval-quality 設計書 §A (2026-09-12): [candidates-converge-to-
example-sma]。既存の承認済み candidate (`mission_outcome='approval'`) と
`(trades, pf, avg_r)` が一致する新規候補は approval を出さず observation
へ倒す — A4 12〜13 回目 (`tmp/a4-run12-codex-20260912.md` 等) で codex/
ornith/muse の 3 backend がコードは別物 (content_hash 3 種) なのに同じ
成績へ収束し、同じ却下判断を 3 回求めた欠陥の再現・pin。

E2E インフラは `tests/loops/test_improve_e2e.py` (FakeImproveWorkerRunner /
improve_env / _fake_in_sample_with_metrics 等) をそのまま再利用する —
strategy 採用ゲートの発火経路 (§4.2-4) を fake で回した上で、質検査
(§A) が gate 通過後・approval 提出前にどう作用するかを検証する。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops.improve_loop import ImproveLoop
from agentic_fx.runners.base import MissionResult
from agentic_fx.store.db import connect, connect_readonly

from tests.loops.test_improve_e2e import (
    NOW,
    FakeImproveWorkerRunner,
    _PASSING_STRATEGY_CONFIG,
    _PASSING_STRATEGY_TEST,
    _fake_record_kwargs,
    _plugin_artifact,
    _write_staging_plugin,
    improve_env,
)


def _fake_in_sample_full_metrics(metrics: dict):
    """`_fake_in_sample_with_metrics` (test_improve_e2e.py) は
    `record_fn` へ渡す行の `metrics` を `_fake_record_kwargs` の既定
    (`{"trades": trades}` のみ) のまま残す — その場合 `backtest_runs` に
    永続化される `metrics_json` には `pf`/`avg_r` が載らず、T-A の質検査
    (`find_matching_approved_metrics` は永続化済み `metrics_json` を読む)
    が比較できる材料にならない。本テスト専用に、行の `metrics` も
    `payload["in_sample"]` と同じ完全な dict に差し替える fake を使う。"""
    def _run(settings, *, history_conn, record_fn, **kwargs):
        row = _fake_record_kwargs(settings=settings, trades=metrics["trades"],
                                  scope="in_sample", **kwargs)
        row["metrics"] = dict(metrics)
        record_fn(row)
        return dict(metrics)
    return _run


def _fake_holdout_full_metrics(metrics: dict):
    def _run(settings, *, history_conn, record_fn, **kwargs):
        row = _fake_record_kwargs(settings=settings, trades=metrics["trades"],
                                  scope="holdout_gate", **kwargs)
        row["metrics"] = dict(metrics)
        record_fn(row)
        return dict(metrics)
    return _run

# 2 候補で content_hash を変えるため、plugin.py の docstring だけ変えた
# 2 本を用意する (run12/13 観測 = 「コードは別物なのに成績が完全一致」)。
_STRATEGY_PY_VARIANT_A = '''
"""variant A"""
def evaluate(df, indicators, signals, params):
    return {"action": "hold", "rationale": "variant a always holds"}
'''
_STRATEGY_PY_VARIANT_B = '''
"""variant B — 別コードだが成績は一致させる"""
def evaluate(df, indicators, signals, params):
    return {"action": "hold", "rationale": "variant b always holds"}
'''

_CONVERGED_METRICS = {
    "trades": 194, "pf": 1.4981, "win_rate": 0.487, "avg_r": 0.188,
    "max_drawdown": 0.0373, "total_pnl": 100.0}
_HOLDOUT_METRICS = {
    "trades": 53, "pf": 0.8047, "win_rate": 0.3, "avg_r": -0.037,
    "max_drawdown": 0.0392, "total_pnl": -10.0}
_DIFFERENT_METRICS = {
    "trades": 40, "pf": 1.2, "win_rate": 0.4, "avg_r": 0.05,
    "max_drawdown": 0.02, "total_pnl": 5.0}


def _make_loop(app, root):
    return ImproveLoop(
        root=root, settings=app.settings, clock=FixedClock(NOW),
        db_write_conn_factory=lambda: connect(root / "data" / "agentic.db"),
        db_readonly_conn_factory=lambda: connect_readonly(
            root / "data" / "agentic.db"),
        activity=app.activity, rag=app.rag)


def _run_strategy_mission(app, root, *, name, plugin_py, in_sample_metrics,
                          holdout_metrics):
    """1 mission ぶんの strategy 候補を通す共通ヘルパ。"""
    loop = _make_loop(app, root)
    output = _plugin_artifact(name, kind="strategy")
    result = MissionResult(status="completed", output=output, transcript=[])
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)), \
         patch("agentic_fx.loops.improve_loop.holdout.run_in_sample",
               _fake_in_sample_full_metrics(in_sample_metrics)), \
         patch("agentic_fx.loops.improve_loop.holdout.run_holdout_gate",
               _fake_holdout_full_metrics(holdout_metrics)):
        mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
        _write_staging_plugin(
            ctx.staging_dir, name, plugin_py, _PASSING_STRATEGY_CONFIG,
            _PASSING_STRATEGY_TEST)
        mission_result = worker.run(mission)
        loop.commit(mission=mission, ctx=ctx, result=mission_result, now=NOW)
    return ctx


def test_second_candidate_with_matching_metrics_becomes_observation(
        improve_env):
    """T-A の主 pin: 1 本目 (variant A) が approval に到達したあと、
    別コード (variant B, content_hash 別) だが同じ (trades, pf, avg_r) の
    2 本目は approval を出さず observation へ倒れる。"""
    app, root = improve_env
    conn = app.conn_core

    ctx_a = _run_strategy_mission(
        app, root, name="converge_a", plugin_py=_STRATEGY_PY_VARIANT_A,
        in_sample_metrics=_CONVERGED_METRICS, holdout_metrics=_HOLDOUT_METRICS)

    approval_a = conn.execute(
        "SELECT id FROM approval_requests WHERE kind='plugin'").fetchone()
    assert approval_a is not None

    content_hash_a = conn.execute(
        "SELECT content_hash FROM backtest_runs WHERE mission_id=? "
        "AND scope='in_sample' AND variant='candidate'",
        (ctx_a.mission_id,)).fetchone()[0]

    ctx_b = _run_strategy_mission(
        app, root, name="converge_b", plugin_py=_STRATEGY_PY_VARIANT_B,
        in_sample_metrics=_CONVERGED_METRICS, holdout_metrics=_HOLDOUT_METRICS)

    content_hash_b = conn.execute(
        "SELECT content_hash FROM backtest_runs WHERE mission_id=? "
        "AND scope='in_sample'", (ctx_b.mission_id,)).fetchone()
    # variant B の候補は質検査で観測降格するため in_sample gate 行自体が
    # 永続化されない (_persist_gate_rows は _finalize_success 経由のみ)。
    assert content_hash_b is None

    assert content_hash_a is not None

    approval_count = conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE kind='plugin'"
    ).fetchone()[0]
    assert approval_count == 1  # variant B は approval を増やさない

    backlog_row = conn.execute(
        "SELECT status, last_result FROM improvement_backlog "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert backlog_row[0] == "observation"
    assert f"duplicate_metrics_of:{content_hash_a}" in backlog_row[1]

    # T-A 検証節 pin: system note の文面に具体パラメータ (fast=/slow= 等)
    # を出さない — note に埋め込まれるのは content_hash のみで、候補の
    # plugin.py 由来のパラメータ文字列は現れない。
    assert "variant b" not in backlog_row[1]
    assert "params" not in backlog_row[1]


def test_candidate_with_different_metrics_still_reaches_approval(
        improve_env):
    """不一致 (3 値のうち 1 つでも異なる) なら approval を継続する。"""
    app, root = improve_env
    conn = app.conn_core

    _run_strategy_mission(
        app, root, name="converge_a2", plugin_py=_STRATEGY_PY_VARIANT_A,
        in_sample_metrics=_CONVERGED_METRICS, holdout_metrics=_HOLDOUT_METRICS)
    _run_strategy_mission(
        app, root, name="converge_c", plugin_py=_STRATEGY_PY_VARIANT_B,
        in_sample_metrics=_DIFFERENT_METRICS, holdout_metrics=_HOLDOUT_METRICS)

    approval_count = conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE kind='plugin'"
    ).fetchone()[0]
    assert approval_count == 2  # 両方とも approval に到達する


def test_first_candidate_alone_is_not_treated_as_duplicate(improve_env):
    """承認済み候補が存在しない (母集団が空) ときは質検査で観測降格しない
    — 1 本目自身が approval に到達すること自体が回帰確認になる。"""
    app, root = improve_env
    conn = app.conn_core

    _run_strategy_mission(
        app, root, name="converge_solo", plugin_py=_STRATEGY_PY_VARIANT_A,
        in_sample_metrics=_CONVERGED_METRICS, holdout_metrics=_HOLDOUT_METRICS)

    approval_count = conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE kind='plugin'"
    ).fetchone()[0]
    assert approval_count == 1
