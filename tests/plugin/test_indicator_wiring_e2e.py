"""[indicator-consumption-wiring] T4: 依存 1 本の strategy が採用ゲートを
完走する E2E (A1 / A1-b / C1 / N1 / P3')。

**モックは使わない** — 実 sqlite (tmp_path) と実 worker サブプロセスで回す
([[test-fixtures-from-real-transcripts]]: モックが潰した次元は変異では取れない)。
実 DB・実 `plugins/` には触れない。
"""
from __future__ import annotations

import pytest

from agentic_fx.backtest import holdout
from agentic_fx.plugin import approval, strategy_adapter, strategy_gate
from agentic_fx.plugin.loader import content_hash, discover_one_with_reason
from agentic_fx.plugin.resolve import (
    ApprovedInventory, InventoryBuildResult, resolve_indicator_deps,
)
from tests.backtest.factories import SETTINGS, _conn
from tests.fixtures import indicator_wiring as fx

pytestmark = pytest.mark.slow


@pytest.fixture
def wired(tmp_path):
    """`rsi` 配備済 + `rsi_pullback` (pinned、staging 相当) + 履歴。"""
    conn = _conn(tmp_path)
    fx.seed_history(conn)
    plugins_root = tmp_path / "plugins"
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    cand = fx.write_rsi_pullback(tmp_path / "cand", pins={"rsi": hashes["rsi"]})
    meta, reason = discover_one_with_reason(cand, "rsi_pullback")
    assert reason is None, reason
    ind_meta, _ = discover_one_with_reason((plugins_root / "rsi").resolve(), "rsi")
    inventory = InventoryBuildResult(
        inventory=ApprovedInventory(root=plugins_root.resolve(),
                                    metas=(ind_meta,)),
        phase1_metas=(ind_meta,), resolved={}, rejected_strategies=())
    settings = SETTINGS.model_copy(deep=True)
    settings.backtest.holdout_months = fx.HOLDOUT_MONTHS
    settings.backtest.eval_source = fx.SOURCE
    settings.backtest.base_interval = fx.BASE_INTERVAL
    return conn, plugins_root, meta, inventory, settings


def test_a1_gate_completes_with_candidate_and_no_strategy_rows(wired):
    """A1: `evaluate_strategy_adoption_gate` 経路 (run_kind_gate) を完走し、
    candidate の in_sample 行 1 本と no_strategy 行 1 本が残る。"""
    conn, _root, meta, inventory, settings = wired
    # `floor_mode="warn"`: A1 は「配線が通って行が残るか」の受入であって
    # 収益性フロアの受入ではない。`enforce` だと in_sample フロア不合格で
    # `strategy_gate.py:316-319` が holdout 前に早期 return し、
    # `no_strategy` 行 (`:353-365`) が書かれずこのテストが偽陰性になる。
    outcome = approval.run_kind_gate(conn, meta, settings=settings, now=fx.NOW,
                                     inventory=inventory, floor_mode="warn")
    assert outcome.verdict_kind in ("ok", "floor"), outcome.verdict_kind
    for row in outcome.gate_rows:
        from agentic_fx.store import backtest_runs as store
        store.save_harness_run(conn, **row)
    rows = conn.execute(
        "SELECT scope, variant, plugin_ref, content_hash FROM backtest_runs "
        "WHERE scope='in_sample' ORDER BY id").fetchall()
    assert [(r["variant"], r["plugin_ref"]) for r in rows] == [
        ("candidate", "plugins/rsi_pullback"),
        ("no_strategy", "no_strategy:rsi_pullback")]
    assert rows[0]["content_hash"] == content_hash(meta.path)


def test_a1b_run_in_sample_direct_writes_only_the_candidate_row(wired):
    """A1-b: `holdout.run_in_sample` を直接叩くと candidate 行のみ。"""
    conn, root, meta, inventory, settings = wired
    resolved = resolve_indicator_deps(meta, inventory.inventory,
                                      settings=settings, pin_mode="require")
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair=fx.PAIR, dataset=settings.backtest.dataset(),
        settings=settings, resolved=resolved)
    try:
        metrics = holdout.run_in_sample(
            settings, history_conn=conn, symbol=fx.PAIR,
            dataset=settings.backtest.dataset(), intent_source=src,
            eval_timeframe=fx.EVAL_TIMEFRAME,
            plugin_ref=f"plugins/{meta.name}", content_hash=meta.content_hash,
            kind="strategy", now=fx.NOW)
    finally:
        src.close()
    assert metrics["trades"] >= 0
    rows = conn.execute("SELECT variant FROM backtest_runs").fetchall()
    assert [r["variant"] for r in rows] == ["candidate"]
    # C1: 実 worker を graceful close したので cpu_sec は float
    assert isinstance(src.cpu_sec, float)


def test_decision_sink_matches_the_independent_oracle(wired):
    """A1 の核: 全評価時点の (action, direction) と SL/TP が独立参照実装と
    一致する。plugin コードは oracle 側で一切 import しない。"""
    conn, root, meta, inventory, settings = wired
    resolved = resolve_indicator_deps(meta, inventory.inventory,
                                      settings=settings, pin_mode="require")
    recorded: list = []
    src = strategy_adapter.build_intent_source(
        meta, conn=conn, pair=fx.PAIR, dataset=settings.backtest.dataset(),
        settings=settings, resolved=resolved,
        decision_sink=lambda ts, d: recorded.append((ts, d)))
    try:
        holdout.run_in_sample(
            settings, history_conn=conn, symbol=fx.PAIR,
            dataset=settings.backtest.dataset(), intent_source=src,
            eval_timeframe=fx.EVAL_TIMEFRAME,
            plugin_ref=f"plugins/{meta.name}", content_hash=meta.content_hash,
            kind="strategy", now=fx.NOW)
    finally:
        src.close()
    fx.assert_decisions_match(recorded, conn)


def test_cpu_samples_reach_the_verdict(wired):
    """C1 (verdict 経路): in_sample / holdout × pair ごとに 1 件ずつ。

    **codex plan r1 I7 是正**: v1.2 は in_sample の 1 件しか assert して
    おらず、**holdout が 0 件でも緑**だった (holdout ループが
    `cpu_samples.append` を忘れる変異を検出できない)。さらに既定の
    `floor_mode="enforce"` では in_sample がフロア不合格になった時点で
    holdout に進まず早期 return しうるので、`floor_mode="warn"` で
    holdout まで必ず走らせる。**scope 列が厳密に
    `["in_sample", "holdout"]` (この順、各 1 件)** であることを pin する。"""
    conn, root, meta, inventory, settings = wired
    resolved = resolve_indicator_deps(meta, inventory.inventory,
                                      settings=settings, pin_mode="require")
    verdict = strategy_gate.evaluate_strategy_adoption_gate(
        conn, meta=meta, now=fx.NOW, settings=settings, resolved=resolved,
        inventory=inventory, record_fn=lambda row: None,
        floor_mode="warn")
    scopes = [s for s, _pair, _cpu in verdict.cpu_samples]
    assert scopes == ["in_sample", "holdout"]        # pair は 1 本 (USDJPY)
    assert all(pair == fx.PAIR for _s, pair, _c in verdict.cpu_samples)
    # C1: 正常終了 / plugin error 後はいずれも float。ここは正常終了経路
    # なので **float であること**まで踏む (`None` 許容にしない — 設計書
    # v1.4 §6 C1、`None` は SIGKILL fallback / worker 未起動の 2 経路のみ)。
    assert all(isinstance(cpu, float) for _s, _p, cpu in verdict.cpu_samples)


def test_resolved_object_identity_across_both_scopes(wired, monkeypatch):
    """P3' (`is` 同一性の全数、codex plan r1 I6): resolver は候補ごとに
    **1 回**、in_sample session / holdout session に渡った `resolved` が
    `GateOutcome.resolved` と**すべて同一オブジェクト**。

    T4a Step 4-1a の unit 版 (`test_run_kind_gate_resolves_once_and_carries_
    the_same_object`) は `evaluate_strategy_adoption_gate` を丸ごと double に
    するため **gate へ 1 回渡った object しか見えない**。ここは実 gate を
    走らせ、`strategy_gate` が scope ごとに呼ぶ `build_intent_source` を
    wrap して両 scope 分の `resolved` を集める。"""
    conn, root, meta, inventory, settings = wired

    calls: list = []
    real_resolve = approval.resolve_indicator_deps

    def _resolve_spy(*a, **k):
        calls.append(k.get("pin_mode"))
        return real_resolve(*a, **k)

    monkeypatch.setattr(approval, "resolve_indicator_deps", _resolve_spy)

    seen_resolved: list = []
    real_build = strategy_gate.strategy_adapter.build_intent_source

    def _build_spy(meta_arg, **kw):
        seen_resolved.append(kw["resolved"])
        return real_build(meta_arg, **kw)

    monkeypatch.setattr(strategy_gate.strategy_adapter,
                        "build_intent_source", _build_spy)

    outcome = approval.run_kind_gate(
        conn, meta, settings=settings, now=fx.NOW, inventory=inventory,
        floor_mode="warn")

    assert calls == ["require"]                 # 候補ごとに 1 回
    # pair 1 本 × (in_sample, holdout) = 2 回。両方とも同じ object。
    assert len(seen_resolved) == 2
    assert seen_resolved[0] is outcome.resolved
    assert seen_resolved[1] is outcome.resolved
    # payload の `indicator_deps` を作る元も同じ object (T4b Step 4-5c で
    # `outcome.resolved.pin_object()` を使う契約 — ここではその値が
    # 一致することまでを固定する)
    assert outcome.resolved.pin_object() == {
        "rsi": {"plugin": "rsi", "content_hash": inventory.inventory.metas[0]
                .content_hash, "params": {"period": fx.RSI_PERIOD}}}
