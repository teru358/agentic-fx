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


def _empty_inventory(plugins_root):
    """[indicator-consumption-wiring] T4b: 依存なし strategy のテストが
    使う空 inventory (`_check_duplicate_metrics_for_approval` の
    `inventory=`/`staging_dir=` 必須化に伴う移行)。"""
    from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult
    inv = ApprovedInventory(root=plugins_root.resolve(), metas=())
    return InventoryBuildResult(inventory=inv, phase1_metas=(), resolved={},
                                rejected_strategies=())


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



def test_duplicate_metrics_check_skips_non_strategy_kind(improve_env):
    """ローカル approval-quality 2 周目 #L3 (2026-09-12): 質検査
    (`_check_duplicate_metrics_for_approval`) は `kind != 'strategy'` の
    payload を対象外にする — indicator は成績 (trades/pf/avg_r) を持たない
    ので、仮に payload に `in_sample` が載っていても既承認 strategy 候補と
    の一致で降格してはいけない。

    ガードを削った変異は現行の payload 形状では観測されない
    (`gate_metrics["in_sample"]` は `kind == "strategy"` 分岐でのみ代入され、
    非 strategy では `eval_source`/`base_interval` も `None` になるため
    実質的に等価変異 — 広域 1422 件で SURVIVED を実測)。ここでは
    `_check_duplicate_metrics_for_approval` を直接呼び、ガードの有無を
    観測可能にする。対照 (`kind='strategy'` で降格が返る) を同じ payload
    で取ることで、fixture が退化していない (母集団に一致行が実在する)
    ことを同時に示す。"""
    app, root = improve_env
    conn = app.conn_core

    ctx_a = _run_strategy_mission(
        app, root, name="kindguard_a", plugin_py=_STRATEGY_PY_VARIANT_A,
        in_sample_metrics=_CONVERGED_METRICS, holdout_metrics=_HOLDOUT_METRICS)

    row = conn.execute(
        "SELECT content_hash, pair, source, base_interval FROM backtest_runs "
        "WHERE mission_id=? AND scope='in_sample' AND variant='candidate'",
        (ctx_a.mission_id,)).fetchone()
    assert row is not None

    loop = _make_loop(app, root)
    payload = {
        "name": "kindguard_b",
        "kind": "strategy",
        "content_hash": "kindguard-b-hash",
        "eval_source": row["source"],
        "base_interval": row["base_interval"],
        "in_sample": {row["pair"]: dict(_CONVERGED_METRICS)},
    }

    # 対照: strategy なら同じ成績の既承認候補を検出して降格する。
    inventory = _empty_inventory(root / "plugins")
    demotion = loop._check_duplicate_metrics_for_approval(
        conn, payload, inventory=inventory, staging_dir=root / "plugins")
    assert demotion is not None
    assert demotion.content_hash == row["content_hash"]
    assert demotion.pair == row["pair"]

    # pin: kind が strategy 以外なら質検査そのものを行わない。
    assert loop._check_duplicate_metrics_for_approval(
        conn, dict(payload, kind="indicator"), inventory=inventory,
        staging_dir=root / "plugins") is None


# --- [indicator-consumption-wiring] T4b Step 4-6: P5 (再ロック除外) --------

from tests.fixtures import indicator_wiring as fx
from tests.fixtures.wiring_envs import SETTINGS_FIXTURE, loop_env as _loop_env


def loop_staging(loop):
    """候補置き場 (このテストでは mission を回さないので直接作る)。"""
    d = loop._root / "plugins" / "_staging" / "1"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _approve_plain_strategy(conn, plugins_root, name):
    """[indicator-consumption-wiring] T4b 逸脱: `_deployed_dir_for` は
    `inventory.phase1_metas` の `meta.path` を返す — plain directory の
    場合は `meta.path == plugins_root/name` だが、`fx.deploy_approved`
    (symlink 化して `.versions/` へ移す) を通すと `meta.path` は
    `.versions/<name>/<hash>` になってしまい、`test_relock_detection_
    uses_the_real_dir_helpers` の「配備位置そのもの」を pin するアサート
    と食い違う。plain directory のまま `approval_requests` にだけ承認済み
    行を作る (着手前検証の取りこぼし — plan 本文はこの承認登録手段を
    明示していなかった)。"""
    from agentic_fx.plugin.loader import content_hash as _content_hash
    from agentic_fx.store import approvals
    chash = _content_hash(plugins_root / name)
    aid = approvals.create(conn, "plugin",
                           {"name": name, "kind": "strategy",
                            "content_hash": chash}, fx.NOW)
    approvals.apply_decision(conn, aid, status="approved", decided_by="fixture",
                             now=fx.NOW)
    return chash


def _seed_approved_candidate_metrics(conn, *, pair, trades, pf, avg_r):
    """既承認候補の in_sample 行を 1 本入れる
    (`find_matching_approved_metrics` が引き当てる母集団)。"""
    from datetime import timedelta

    from agentic_fx.store import approvals, backtest_runs as br
    aid = approvals.create(conn, "plugin",
                           {"name": "rsi_pullback", "kind": "strategy",
                            "content_hash": "d" * 64}, NOW)
    approvals.apply_decision(conn, aid, status="approved", decided_by="t", now=NOW)
    br.save_harness_run(
        conn, scope="in_sample", plugin_ref="plugins/rsi_pullback",
        content_hash="d" * 64, kind="strategy", pair=pair, timeframe="1h",
        source="dukascopy", base_interval="5m", params={},
        period=(NOW - timedelta(days=90), NOW),
        metrics={"trades": trades, "pf": pf, "win_rate": 0.5, "avg_r": avg_r,
                 "max_drawdown": 0.05, "total_pnl": 100.0, "evaluable": True,
                 "fallback_spread_used": False},
        settings_hash="h", core_commit="c", initial_balance=1_000_000.0,
        now=NOW, variant="candidate",
        # [indicator-consumption-wiring] T4b 逸脱: プラン本文のこの
        # ヘルパは `mission_outcome` を渡していなかったが、
        # `find_matching_approved_metrics` の母集団条件は
        # `mission_outcome='approval'` を要求する (store/backtest_runs.py)。
        # 明示的に付けないと既定 `None` のまま母集団に入らず、質検査が
        # 常に「一致無し」になる (実測で確認、着手前検証の取りこぼし)。
        mission_outcome="approval")


def monkeypatch_dirs(loop, *, deployed, candidate):
    """`_deployed_dir_for` / `_candidate_dir_for` を固定する。

    **これは注入シームではなく短絡である** (codex plan r1 C3): これを使う
    テストは 2 つの helper の中身を 1 行も実行しない。`is_relock_transition`
    の判定そのものを見るために使い、**helper の配線は下の
    `test_relock_detection_uses_the_real_dir_helpers` が実物で検証する**。"""
    loop._deployed_dir_for = lambda name, *, inventory=None: deployed
    loop._candidate_dir_for = lambda payload, *, staging_dir=None: candidate


def _inventory_with_phase1(conn, plugins_root):
    from agentic_fx.tools import plugin_loader as tools_plugin_loader
    return tools_plugin_loader.approved_plugins(
        conn, plugins_root, settings=SETTINGS_FIXTURE)


def test_relock_only_resubmission_skips_the_duplicate_metrics_check(tmp_path):
    """P5: indicator を出力不変の変更で更新 → 依存 strategy を再ロック →
    再提出の成績が既承認版と一致しても `duplicate_metrics_of` で降格されない。"""
    loop, conn, plugins_root = _loop_env(tmp_path)
    fx.write_indicator(plugins_root, "rsi")            # = I2 (現在 inventory)
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    deployed = fx.write_rsi_pullback(plugins_root, pins={"rsi": "a" * 64})
    candidate = fx.write_rsi_pullback(loop_staging(loop), pins={"rsi": hashes["rsi"]})
    inventory = _inventory_with_phase1(conn, plugins_root)
    _seed_approved_candidate_metrics(conn, pair="USDJPY", trades=40, pf=1.5,
                                     avg_r=0.2)
    payload = {"kind": "strategy", "name": "rsi_pullback",
               "content_hash": "c" * 64, "eval_source": "dukascopy",
               "base_interval": "5m",
               "in_sample": {"USDJPY": {"trades": 40, "pf": 1.5, "avg_r": 0.2}}}
    monkeypatch_dirs(loop, deployed=deployed, candidate=candidate)
    demotion = loop._check_duplicate_metrics_for_approval(
        conn, payload, inventory=inventory, staging_dir=loop_staging(loop))
    assert demotion is None


def test_unrelated_duplicate_metrics_still_demote(tmp_path):
    """P5 の裏: 再ロックでない一致は従来どおり降格する。"""
    loop, conn, plugins_root = _loop_env(tmp_path)
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    deployed = fx.write_rsi_pullback(plugins_root, pins={"rsi": hashes["rsi"]})
    candidate = fx.write_rsi_pullback(loop_staging(loop),
                                      pins={"rsi": hashes["rsi"]})
    inventory = _inventory_with_phase1(conn, plugins_root)
    _seed_approved_candidate_metrics(conn, pair="USDJPY", trades=40, pf=1.5,
                                     avg_r=0.2)
    payload = {"kind": "strategy", "name": "rsi_pullback",
               "content_hash": "c" * 64, "eval_source": "dukascopy",
               "base_interval": "5m",
               "in_sample": {"USDJPY": {"trades": 40, "pf": 1.5, "avg_r": 0.2}}}
    monkeypatch_dirs(loop, deployed=deployed, candidate=candidate)
    assert loop._check_duplicate_metrics_for_approval(
        conn, payload, inventory=inventory, staging_dir=loop_staging(loop)
    ) is not None


def test_relock_detection_uses_the_real_dir_helpers(tmp_path):
    """codex plan r1 C3: `monkeypatch_dirs` を使わず、`_deployed_dir_for` /
    `_candidate_dir_for` の**実装そのもの**を通す。"""
    loop, conn, plugins_root = _loop_env(tmp_path)
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    fx.write_rsi_pullback(plugins_root, pins={"rsi": "a" * 64})
    _approve_plain_strategy(conn, plugins_root, "rsi_pullback")
    staging = loop_staging(loop)
    fx.write_rsi_pullback(staging, pins={"rsi": hashes["rsi"]})
    inventory = _inventory_with_phase1(conn, plugins_root)
    _seed_approved_candidate_metrics(conn, pair="USDJPY", trades=40, pf=1.5,
                                     avg_r=0.2)
    payload = {"kind": "strategy", "name": "rsi_pullback",
               "content_hash": "c" * 64, "eval_source": "dukascopy",
               "base_interval": "5m",
               "in_sample": {"USDJPY": {"trades": 40, "pf": 1.5, "avg_r": 0.2}}}

    assert loop._deployed_dir_for("rsi_pullback", inventory=inventory) == \
        (plugins_root / "rsi_pullback")
    assert loop._candidate_dir_for(payload, staging_dir=staging) == \
        (staging / "rsi_pullback")
    assert loop._check_duplicate_metrics_for_approval(
        conn, payload, inventory=inventory, staging_dir=staging) is None


# [indicator-consumption-wiring] T5b Step 5-5c: T4b の
# `test_finalize_success_falls_back_to_inventory_for_gate_when_ctx_
# inventory_is_none` はここに存在したが、`_inventory_for_gate` フォール
# バック分岐ごと削除した (プラン本文どおり — T5a 完了以降は `prepare()` が
# 常に非空の `ctx.inventory` を書き込むため、フォールバック分岐は恒久的に
# 到達不能な死にコードになっていた)。


# --- /code-review 2 周目 CR3 (2026-09-18): tx 内の is_relock_transition ---


@pytest.mark.parametrize("exc", [OSError("boom"), UnicodeError("boom"),
                                 SyntaxError("boom"),
                                 __import__("yaml").YAMLError("boom")])
def test_relock_detection_failure_falls_back_to_the_quality_check(tmp_path, exc):
    """/code-review 2 周目 CR3: `_check_duplicate_metrics_for_approval` は
    `_finalize_success` の `BEGIN IMMEDIATE` の**内側**から呼ばれる。
    `is_relock_transition` は file I/O + YAML/AST parse をするので
    `OSError`/`UnicodeError`/`SyntaxError`/`yaml.YAMLError` を投げうるが、
    従来は無防備で、`_finalize_success` の外側 `except Exception` が
    `_compensate_tx2_failure` を走らせて approval 全体を捨てていた。

    `plugin/noop_gate.py:71` の同種呼び出しと**同じ except 集合**で
    「再ロックではない」に倒し、通常の質検査へ落ちる (fail closed) こと
    を pin する。"""
    import agentic_fx.loops.improve_loop as il
    loop, conn, plugins_root = _loop_env(tmp_path)
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    deployed = fx.write_rsi_pullback(plugins_root, pins={"rsi": "a" * 64})
    candidate = fx.write_rsi_pullback(loop_staging(loop),
                                      pins={"rsi": hashes["rsi"]})
    inventory = _inventory_with_phase1(conn, plugins_root)
    _seed_approved_candidate_metrics(conn, pair="USDJPY", trades=40, pf=1.5,
                                     avg_r=0.2)
    payload = {"kind": "strategy", "name": "rsi_pullback",
               "content_hash": "c" * 64, "eval_source": "dukascopy",
               "base_interval": "5m",
               "in_sample": {"USDJPY": {"trades": 40, "pf": 1.5, "avg_r": 0.2}}}
    monkeypatch_dirs(loop, deployed=deployed, candidate=candidate)

    def _boom(*a, **k):
        raise exc

    with patch.object(il, "is_relock_transition", _boom):
        demotion = loop._check_duplicate_metrics_for_approval(
            conn, payload, inventory=inventory, staging_dir=loop_staging(loop))
    # 例外は伝播せず、「再ロックではない」として通常の質検査に落ちる
    assert demotion is not None
    assert demotion.content_hash == "d" * 64
