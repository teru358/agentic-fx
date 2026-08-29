"""evaluate_strategy_adoption_gate — 戦略採用ゲート (設計書 §4.2-4、
プラン §8.1-40)。ImproveLoop から独立した plugin/strategy_gate.py の
モジュール関数として直接テストする (Task 11 の bless --from _human も
同じ関数を import する — §8.1-41)。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.strategy_gate import evaluate_strategy_adoption_gate

_SETTINGS = MagicMock()  # run_in_sample/run_holdout_gate は monkeypatch で
                         # 差し替えるため settings の中身は本節のテストでは
                         # 参照されない (build_intent_source も同様に
                         # monkeypatch する — 下記 `_meta`/`_fake_intent_source`)


def _meta(name="myst", pairs=("USDJPY",), timeframe="1h",
         content_hash="h1") -> PluginMeta:
    """`build_intent_source` に渡す最小の `PluginMeta` (直前修正の申し送り④)。
    `path`/`params`/`max_bars` はこの節のテストでは使われない (`build_intent_source`
    自体を monkeypatch するため) — real 値は不要。"""
    from pathlib import Path
    return PluginMeta(name=name, kind="strategy", path=Path("/tmp/x"),
                      params={}, timeframe=timeframe, pairs=tuple(pairs),
                      max_bars=1000, content_hash=content_hash)


def _fake_intent_source(monkeypatch):
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.strategy_adapter.build_intent_source",
        lambda meta, **kw: MagicMock(close=lambda: None))


def test_below_evaluable_min_trades_is_observation_not_rejected(monkeypatch):
    """合計取引数 < 30 → 承認申請を出さず observation (悪いとは記録しない)。"""
    _fake_intent_source(monkeypatch)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        lambda *a, **kw: {"trades": 10, "pf": 1.0})
    verdict = evaluate_strategy_adoption_gate(
        MagicMock(), name="myst", pairs=["USDJPY"], timeframe="1h",
        content_hash="h1", now=datetime(2026, 8, 22), settings=_SETTINGS,
        meta=_meta())
    assert verdict.evaluable is False
    assert verdict.observation_reason.startswith("insufficient_trades")


def test_evaluable_min_trades_is_sum_across_pairs(monkeypatch):
    """`EVALUABLE_MIN_TRADES` の集計単位は meta.pairs 合計 (単一 pair
    ではない)。2 pair × 16 trades = 32 >= 30 で evaluable。"""
    _fake_intent_source(monkeypatch)
    calls = []
    def _fake_run_in_sample(*a, **kw):
        calls.append(kw.get("symbol"))
        return {"trades": 16, "pf": 1.2}
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        _fake_run_in_sample)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_holdout_gate",
        lambda *a, **kw: {"trades": 16, "pf": 1.1})
    verdict = evaluate_strategy_adoption_gate(
        MagicMock(), name="myst", pairs=["USDJPY", "EURUSD"], timeframe="1h",
        content_hash="h1", now=datetime(2026, 8, 22), settings=_SETTINGS,
        meta=_meta(pairs=("USDJPY", "EURUSD")))
    assert verdict.evaluable is True
    assert calls == ["USDJPY", "EURUSD"]


def test_baseline_uses_live_d4_approved_same_name_strategy(
        monkeypatch, conn_with_approved_strategy):
    _fake_intent_source(monkeypatch)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        lambda *a, **kw: {"trades": 30, "pf": 1.2})
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_holdout_gate",
        lambda *a, **kw: {"trades": 30, "pf": 1.1})
    verdict = evaluate_strategy_adoption_gate(
        conn_with_approved_strategy, name="myst", pairs=["USDJPY"],
        timeframe="1h", content_hash="h2", now=datetime(2026, 8, 22),
        settings=_SETTINGS, meta=_meta(content_hash="h2"))
    assert verdict.baseline_variant == "baseline"
    assert verdict.baseline_row["ref_plugin_ref"] == "plugins/myst"


def test_baseline_falls_back_to_no_strategy_when_no_approved_same_name(
        monkeypatch, conn):
    _fake_intent_source(monkeypatch)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        lambda *a, **kw: {"trades": 30, "pf": 1.2})
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_holdout_gate",
        lambda *a, **kw: {"trades": 30, "pf": 1.1})
    verdict = evaluate_strategy_adoption_gate(
        conn, name="brand_new_strategy", pairs=["USDJPY"], timeframe="1h",
        content_hash="h3", now=datetime(2026, 8, 22), settings=_SETTINGS,
        meta=_meta(name="brand_new_strategy", content_hash="h3"))
    assert verdict.baseline_variant == "no_strategy"
    assert verdict.baseline_row is not None  # 決して null にしない
    assert verdict.baseline_row["plugin_ref"] == "no_strategy:brand_new_strategy"


def test_indicator_and_signal_kinds_skip_this_gate_entirely():
    verdict = evaluate_strategy_adoption_gate(
        None, name="myind", pairs=[], timeframe="1h", content_hash="h4",
        now=datetime(2026, 8, 22), kind="indicator", settings=_SETTINGS,
        meta=None)   # kind!="strategy" は meta を使う前に早期 return する
    assert verdict is None


def test_record_fn_is_forwarded_to_run_in_sample_and_run_holdout(
        monkeypatch, conn_with_approved_strategy):
    """3 周目レビュー Important-2: `record_fn` を渡すと `run_in_sample`/
    `run_holdout_gate` の両方へそのまま転送される — 転送されないと Tx-2 の
    外で即時 commit されてしまい、`ImproveLoop.commit` 手順4〜7-9 で組み立てる
    `gate_rows` が常に空になる。"""
    _fake_intent_source(monkeypatch)
    seen_record_fns = []

    def _fake_run_in_sample(*a, record_fn=None, **kw):
        seen_record_fns.append(("run_in_sample", record_fn))
        return {"trades": 30, "pf": 1.2}  # >= EVALUABLE_MIN_TRADES (30) で
                                          # run_holdout まで到達させる

    def _fake_run_holdout(*a, record_fn=None, **kw):
        seen_record_fns.append(("run_holdout", record_fn))
        return {"trades": 30, "pf": 1.1}

    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        _fake_run_in_sample)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_holdout_gate",
        _fake_run_holdout)
    sentinel = object()
    evaluate_strategy_adoption_gate(
        conn_with_approved_strategy, name="myst", pairs=["USDJPY"],
        timeframe="1h", content_hash="h5", now=datetime(2026, 8, 22),
        settings=_SETTINGS, meta=_meta(content_hash="h5"), record_fn=sentinel)
    assert seen_record_fns == [("run_in_sample", sentinel),
                               ("run_holdout", sentinel)]


def test_content_hash_argument_wins_over_meta_content_hash(
        monkeypatch, conn_with_approved_strategy):
    """直前修正の申し送り④: `meta` は `intent_source` 構築のためだけに使う —
    identity/persist に使う `content_hash` は明示引数 (10.6 節の再計算値) が
    常に正であり、`meta.content_hash` が食い違っていても引数側が勝つ。"""
    _fake_intent_source(monkeypatch)
    seen_content_hashes = []
    def _fake_run_in_sample(*a, **kw):
        seen_content_hashes.append(kw.get("content_hash"))
        return {"trades": 30, "pf": 1.2}
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        _fake_run_in_sample)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_holdout_gate",
        lambda *a, **kw: {"trades": 30, "pf": 1.1})
    evaluate_strategy_adoption_gate(
        conn_with_approved_strategy, name="myst", pairs=["USDJPY"],
        timeframe="1h", content_hash="recomputed-hash",
        now=datetime(2026, 8, 22), settings=_SETTINGS,
        meta=_meta(content_hash="stale-meta-hash"))
    assert seen_content_hashes == ["recomputed-hash"]


def test_pending_only_approval_does_not_count_as_baseline(
        monkeypatch, conn_with_pending_strategy):
    """D-6 是正 (プラン申し送り B.6、10.7 M5 の killer pin): 同名 strategy の
    承認 *申請* が存在するだけ (`status='pending'`、未承認) では baseline
    として扱わない — `AND status='approved'` を落とす退行を殺す。行が
    0 件のケース (`test_baseline_falls_back_to_no_strategy_when_no_approved_
    same_name`) だけでは `WHERE` 句ごと消えても結果が変わらず退行を
    検出できない。"""
    _fake_intent_source(monkeypatch)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        lambda *a, **kw: {"trades": 30, "pf": 1.2})
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_holdout_gate",
        lambda *a, **kw: {"trades": 30, "pf": 1.1})
    verdict = evaluate_strategy_adoption_gate(
        conn_with_pending_strategy, name="myst", pairs=["USDJPY"],
        timeframe="1h", content_hash="h2", now=datetime(2026, 8, 22),
        settings=_SETTINGS, meta=_meta(content_hash="h2"))
    assert verdict.baseline_variant == "no_strategy"
    assert verdict.baseline_row["plugin_ref"] == "no_strategy:myst"


def test_eval_timeframe_normalizes_1d_to_24h_for_run_in_sample_and_holdout(
        monkeypatch, conn_with_approved_strategy):
    """D-6 是正 (プラン申し送り B.6、10.7 M8 の killer pin):
    `meta.timeframe="1d"` は `_eval_timeframe` により `eval_timeframe="24h"`
    へ正規化されて `run_in_sample`/`run_holdout_gate` の両方に渡る —
    正規化を削る退行を検出する。"""
    _fake_intent_source(monkeypatch)
    seen_eval_timeframes = []

    def _fake_run_in_sample(*a, **kw):
        seen_eval_timeframes.append(("run_in_sample", kw.get("eval_timeframe")))
        return {"trades": 30, "pf": 1.2}

    def _fake_run_holdout(*a, **kw):
        seen_eval_timeframes.append(("run_holdout", kw.get("eval_timeframe")))
        return {"trades": 30, "pf": 1.1}

    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        _fake_run_in_sample)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_holdout_gate",
        _fake_run_holdout)
    evaluate_strategy_adoption_gate(
        conn_with_approved_strategy, name="myst", pairs=["USDJPY"],
        timeframe="1d", content_hash="h6", now=datetime(2026, 8, 22),
        settings=_SETTINGS, meta=_meta(timeframe="1d", content_hash="h6"))
    assert seen_eval_timeframes == [("run_in_sample", "24h"),
                                    ("run_holdout", "24h")]


# round2 O1/O2/O3 是正 (2026-08-29、verified-round2.md、pin のみ — 実装は
# 触らない): `record_fn=None` の即時 save 経路 (`switch._run_full_gate`
# = P1 submit / P3 bless の本番経路。改善ループ側は `record_fn=gate_rows.
# append` を渡すため到達しない) の観測点が無かった。

def _no_strategy_row_kwargs(*, pair="USDJPY"):
    """`holdout.run_in_sample` が `record_fn` へ渡す実 kwargs 形
    (`backtest_runs_store.save_harness_run` の必須引数一式) を模す。"""
    from datetime import timezone
    return {
        "scope": "in_sample", "plugin_ref": "plugins/brand_new_strategy",
        "content_hash": "h3", "kind": "strategy", "pair": pair,
        "timeframe": "1h", "source": "dukascopy",
        "period": (datetime(2026, 1, 1, tzinfo=timezone.utc),
                  datetime(2026, 2, 1, tzinfo=timezone.utc)),
        "metrics": {"trades": 30, "pf": 1.2, "win_rate": 0.5, "avg_r": 0.1,
                    "max_drawdown": -0.1, "total_pnl": 100.0,
                    "evaluable": True, "fallback_spread_used": False},
        "settings_hash": "sh1", "core_commit": "cc1",
        "initial_balance": 10000.0,
        "now": datetime(2026, 8, 22, tzinfo=timezone.utc),
        "variant": "candidate"}


def test_no_strategy_row_is_saved_immediately_when_record_fn_is_none(
        monkeypatch, conn):
    """O1 pin: `record_fn` 省略 (=None、`switch._run_full_gate` の本番経路)
    でも no_strategy 行が `backtest_runs` へ即時 save されること。
    `fake run_in_sample` は `record_fn(row)` を実際に呼ぶ形にする
    (呼ばないと `in_sample_rows` が空で no_strategy 行が 0 件になり、
    テストが「実装が動いた」ことを見誤る)。"""
    _fake_intent_source(monkeypatch)

    def _fake_run_in_sample(*a, record_fn=None, **kw):
        row = _no_strategy_row_kwargs(pair=kw.get("symbol", "USDJPY"))
        if record_fn is not None:
            record_fn(row)
        return {"trades": 30, "pf": 1.2}

    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        _fake_run_in_sample)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_holdout_gate",
        lambda *a, **kw: {"trades": 30, "pf": 1.1})

    verdict = evaluate_strategy_adoption_gate(
        conn, name="brand_new_strategy", pairs=["USDJPY"], timeframe="1h",
        content_hash="h3", now=datetime(2026, 8, 22), settings=_SETTINGS,
        meta=_meta(name="brand_new_strategy", content_hash="h3"))
    assert verdict.baseline_variant == "no_strategy"

    rows = conn.execute(
        "SELECT plugin_ref, variant FROM backtest_runs "
        "WHERE plugin_ref='no_strategy:brand_new_strategy' "
        "AND variant='no_strategy'").fetchall()
    assert len(rows) == 1


def test_no_strategy_path_wraps_record_fn_for_run_in_sample_only(
        monkeypatch, conn):
    """O2 pin: no_strategy 経路 (承認行なし) では `run_in_sample` へ渡る
    record_fn は捕捉用のラッパー (sentinel そのものではない) だが、
    `run_holdout_gate` へは sentinel がそのまま転送される
    (`evaluate_strategy_adoption_gate:165` 付近 — 素通し)。この非対称を
    固定する。"""
    _fake_intent_source(monkeypatch)
    seen = []

    def _fake_run_in_sample(*a, record_fn=None, **kw):
        seen.append(("run_in_sample", record_fn))
        return {"trades": 30, "pf": 1.2}

    def _fake_run_holdout(*a, record_fn=None, **kw):
        seen.append(("run_holdout", record_fn))
        return {"trades": 30, "pf": 1.1}

    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        _fake_run_in_sample)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_holdout_gate",
        _fake_run_holdout)
    sentinel = object()

    evaluate_strategy_adoption_gate(
        conn, name="brand_new_strategy", pairs=["USDJPY"], timeframe="1h",
        content_hash="h3", now=datetime(2026, 8, 22), settings=_SETTINGS,
        meta=_meta(name="brand_new_strategy", content_hash="h3"),
        record_fn=sentinel)

    assert len(seen) == 2
    in_sample_call, holdout_call = seen
    assert in_sample_call[0] == "run_in_sample"
    assert in_sample_call[1] is not sentinel  # ラッパーに差し替えられる
    assert holdout_call == ("run_holdout", sentinel)  # sentinel がそのまま転送

    # そのラッパーを直接呼ぶと (a) sentinel (呼び出し可能な fake) へ転送
    # され (b) in_sample_rows に積まれる、の両立を確認する。
    wrapper = in_sample_call[1]
    forwarded_to_sentinel = []

    def _callable_sentinel(row):
        forwarded_to_sentinel.append(row)

    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        lambda *a, record_fn=None, **kw: (
            record_fn(_no_strategy_row_kwargs()),
            {"trades": 30, "pf": 1.2})[1])
    # wrapper は `evaluate_strategy_adoption_gate` の閉じたクロージャ内で
    # `in_sample_rows.append(row)` と `record_fn(row)` (呼び出し元の
    # record_fn=None のときは即時 save) の両方を行う。ここでは呼び出し元
    # の record_fn を実際に呼び出し可能な fake に差し替えて再実行し、
    # wrapper が (a) sentinel へ転送する (b) in_sample_rows へ積む の
    # 両方を行うことを確認する。
    evaluate_strategy_adoption_gate(
        conn, name="brand_new_strategy2", pairs=["USDJPY"], timeframe="1h",
        content_hash="h3b", now=datetime(2026, 8, 22), settings=_SETTINGS,
        meta=_meta(name="brand_new_strategy2", content_hash="h3b"),
        record_fn=_callable_sentinel)
    # (a) wrapper が転送する: in-sample の生 row (1回目) と、末尾ループが
    # 組み立てる no_strategy 行 (2回目、plugin_ref が no_strategy: 接頭辞)
    # の 2 回、record_fn へ届くこと。
    assert len(forwarded_to_sentinel) == 2
    assert forwarded_to_sentinel[0]["plugin_ref"] == "plugins/brand_new_strategy"
    assert forwarded_to_sentinel[1]["plugin_ref"] == "no_strategy:brand_new_strategy2"
    rows = conn.execute(
        "SELECT plugin_ref FROM backtest_runs WHERE "
        "plugin_ref='no_strategy:brand_new_strategy2'").fetchall()
    assert len(rows) == 0  # (b) record_fn 指定時は即時 save しない (呼び出し
                           # 元の record_fn 経由でのみ蓄積される — Tx-2 側の
                           # 責務であって wrapper 自身は DB に書かない)


def test_no_strategy_row_copies_identity_and_replaces_metrics_only(
        monkeypatch, conn):
    """O3 pin: no_strategy 行が candidate 行の identity 列
    (period/settings_hash/core_commit/initial_balance/source/timeframe/
    content_hash/kind/scope) をそのまま複製し、plugin_ref/variant/metrics
    だけ差し替えることを固定する。`dict(row)` の shallow copy を消す変異
    は candidate 行の dict も書き換えてしまう (nested な metrics 自体は
    丸ごと置換なので nested 破壊は起きないが、plugin_ref の破壊は起きる)。"""
    _fake_intent_source(monkeypatch)
    captured_rows: list[dict] = []

    def _fake_run_in_sample(*a, record_fn=None, **kw):
        row = _no_strategy_row_kwargs(pair=kw.get("symbol", "USDJPY"))
        if record_fn is not None:
            record_fn(row)
        captured_rows.append(row)
        return {"trades": 30, "pf": 1.2}

    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        _fake_run_in_sample)
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_holdout_gate",
        lambda *a, **kw: {"trades": 30, "pf": 1.1})

    evaluate_strategy_adoption_gate(
        conn, name="brand_new_strategy", pairs=["USDJPY"], timeframe="1h",
        content_hash="h3", now=datetime(2026, 8, 22), settings=_SETTINGS,
        meta=_meta(name="brand_new_strategy", content_hash="h3"))

    candidate_row = captured_rows[0]
    row = conn.execute(
        "SELECT * FROM backtest_runs WHERE plugin_ref="
        "'no_strategy:brand_new_strategy' AND variant='no_strategy'"
    ).fetchone()
    assert row is not None
    assert row["settings_hash"] == candidate_row["settings_hash"]
    assert row["core_commit"] == candidate_row["core_commit"]
    assert row["initial_balance"] == candidate_row["initial_balance"]
    assert row["source"] == candidate_row["source"]
    assert row["timeframe"] == candidate_row["timeframe"]
    assert row["content_hash"] == candidate_row["content_hash"]
    assert row["kind"] == candidate_row["kind"]
    assert row["scope"] == candidate_row["scope"]
    assert row["plugin_ref"] == "no_strategy:brand_new_strategy"
    assert row["variant"] == "no_strategy"
    # candidate 行の dict 自体は破壊されていないこと (shallow copy pin)
    assert candidate_row["plugin_ref"] == "plugins/brand_new_strategy"
