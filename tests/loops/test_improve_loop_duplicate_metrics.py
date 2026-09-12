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
import threading
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

    # /code-review 2 周目 CR3 是正 (2026-09-12): 観測降格でも gate が実際に
    # 計測した in_sample/holdout の成績行は失わない — mission_outcome=
    # 'observation' で残す (どの pair が質検査を引いたかを後から追跡できる
    # ようにするため)。従来 (CR3 是正前) は variant B の in_sample 行自体が
    # 永続化されなかったが、`_finalize_report_or_observation` が
    # `gate_rows` を受け取り `_persist_gate_rows` を呼ぶようになったため、
    # ここでは行が「有る」ことと `mission_outcome` を検証する。
    row_b = conn.execute(
        "SELECT content_hash, mission_outcome FROM backtest_runs "
        "WHERE mission_id=? AND scope='in_sample'",
        (ctx_b.mission_id,)).fetchone()
    assert row_b is not None
    assert row_b["mission_outcome"] == "observation"
    content_hash_b = row_b["content_hash"]

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


def test_concurrent_duplicate_candidates_only_one_reaches_approval(
        improve_env):
    """/code-review 2 周目 CR1 是正の pin: `settings.improve.parallel>1`
    (別 slot=別スレッド=別 sqlite 接続) で 2 本の候補が**同時に**同じ
    (trades, pf, avg_r) を持つとき、質検査の check-then-insert レースを
    防いだ実装は approval を 1 件だけ出し、もう 1 本は observation に
    倒れる。旧実装 (質検査が `_finalize_success` の `BEGIN IMMEDIATE` より
    前・bare SELECT で行われる) はこの pin で RED になる — 両スレッドが
    互いの未コミット行を見られないまま「一致なし」と判定し、2 件の
    approval が生まれる。

    `test_concurrent_duplicate_selection_loser_becomes_observation`
    (test_improve_e2e.py) と同じ骨格: 2 スレッド + 別 db 接続
    (`db_write_conn_factory` が呼び出しごとに新しい接続を作る)。

    barrier の置き場所が本 pin の要 — `loop.commit()` 呼び出し直前や
    `_check_duplicate_metrics_for_approval` の内側に置くと、修正後の実装
    (質検査が `BEGIN IMMEDIATE` の内側) では 1 本目が書き込みロックを
    保持したまま barrier で待ち、2 本目はロック待ちで barrier に到達
    できず**デッドロックする** (先に書き込みロックを取った側が
    `BEGIN IMMEDIATE` より前で足止めされる 2 本目を待ち続ける)。
    `ImproveLoop._finalize_success` の**入口** (どちらの版でも
    `BEGIN IMMEDIATE`/bare SELECT のどちらより前) を monkeypatch して
    barrier を置くことで、両実装のどちらでも「tx を一切開いていない
    状態」で両スレッドを揃えられる — 修正後は揃った直後に
    `BEGIN IMMEDIATE` で直列化され 2 本目が 1 本目の commit 後を読む。
    質検査が tx の外 (bare SELECT) に戻る変異では、揃った直後に両方が
    ロック無しで SELECT するため互いの未コミット行を見られず、2 件とも
    approval に達する — この pin が RED になる。"""
    app, root = improve_env
    conn = app.conn_core

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    results = {
        name: MissionResult(
            status="completed",
            output=_plugin_artifact(name, kind="strategy"), transcript=[])
        for name in ("race_a", "race_b")
    }

    def _worker_runner_factory(**kw):
        # test_improve_e2e.py の同名パターン (M-2 是正) をそのまま踏襲する:
        # `unittest.mock.patch` は同一ターゲットへの並行 `__enter__`/
        # `__exit__` に対して原子的でないため、各スレッドの内側で個別に
        # `with patch(...)` してはいけない (先に抜けた方が「元の値」を
        # 誤って書き戻し、以降の全テストで `WorkerRunner` がクラス→関数に
        # 化けて壊れる)。patch はメインスレッドで両スレッドの起動〜join を
        # 囲む 1 回だけに統一し、結果はスレッド名で振り分ける。
        name = threading.current_thread().name
        return FakeImproveWorkerRunner(result=results[name], **kw)

    orig_finalize_success = ImproveLoop._finalize_success

    def _synced_finalize_success(self, *a, **kw):
        # 両スレッドをここで完全に足並み揃える — この時点ではどちらの
        # スレッドもまだ tx を開いていない (呼び出し元 `commit()` は
        # `_finalize_success` を呼ぶだけで、tx は `_finalize_success` の
        # 内部 (質検査の前か後かは実装次第) で初めて開く)。
        barrier.wait(timeout=10)
        return orig_finalize_success(self, *a, **kw)

    def _run(name: str, plugin_py: str) -> None:
        try:
            loop = _make_loop(app, root)
            mission, ctx, worker = loop.prepare(slot_key=None, now=NOW)
            _write_staging_plugin(
                ctx.staging_dir, name, plugin_py,
                _PASSING_STRATEGY_CONFIG, _PASSING_STRATEGY_TEST)
            mission_result = worker.run(mission)
            loop.commit(mission=mission, ctx=ctx, result=mission_result,
                       now=NOW)
        except BaseException as exc:  # noqa: BLE001 — スレッド内例外を回収
            errors.append(exc)

    threads = [
        threading.Thread(target=_run, args=(name, plugin_py), name=name)
        for name, plugin_py in (
            ("race_a", _STRATEGY_PY_VARIANT_A),
            ("race_b", _STRATEGY_PY_VARIANT_B))]
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               _worker_runner_factory), \
         patch("agentic_fx.loops.improve_loop.holdout.run_in_sample",
               _fake_in_sample_full_metrics(_CONVERGED_METRICS)), \
         patch("agentic_fx.loops.improve_loop.holdout.run_holdout_gate",
               _fake_holdout_full_metrics(_HOLDOUT_METRICS)), \
         patch.object(ImproveLoop, "_finalize_success",
                     _synced_finalize_success):
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    assert errors == [], f"thread(s) raised: {errors!r}"

    approval_count = conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE kind='plugin'"
    ).fetchone()[0]
    assert approval_count == 1

    observation_rows = conn.execute(
        "SELECT status FROM improvement_backlog WHERE status='observation'"
    ).fetchall()
    assert len(observation_rows) == 1

