"""evaluate_strategy_adoption_gate — 戦略採用ゲート (設計書 §4.2-4、
プラン §8.1-40)。`ImproveLoop.commit` 手順4 と Task 11 の
`bless --from _human` (§8.1-41) の両方から import される共有モジュール
(統合裁定 R-i3 — plugin/ 配下の独立ファイルに置く)。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from agentic_fx.backtest import holdout
from agentic_fx.backtest.metrics import EVALUABLE_MIN_TRADES
from agentic_fx.plugin import strategy_adapter
from agentic_fx.plugin.loader import PluginMeta

# `plugin/approval.py` と同じ timeframe 正規化。
_EVAL_TIMEFRAME_OVERRIDE = {"1d": "24h"}


def _eval_timeframe(meta_timeframe: str) -> str:
    return _EVAL_TIMEFRAME_OVERRIDE.get(meta_timeframe, meta_timeframe)


@dataclass(frozen=True)
class StrategyGateVerdict:  # 新規命名 (元 _StrategyGateVerdict — 独立
                            # モジュールへ切り出すため公開名にする)
    evaluable: bool
    observation_reason: str = ""
    baseline_variant: str = "no_strategy"
    baseline_row: dict | None = None
    candidate_metrics: dict | None = None


def evaluate_strategy_adoption_gate(
    conn, *, meta: "PluginMeta | None", now: datetime, settings: "Settings",
    name: str | None = None, pairs: "list[str] | None" = None,
    timeframe: str | None = None, content_hash: str | None = None,
    kind: str = "strategy",
    history_conn=None,
    run_in_sample_fn=None, run_holdout_gate_fn=None,
    record_fn: "Callable[[dict], None] | None" = None,
) -> "StrategyGateVerdict | None":
    """candidate/baseline/no_strategy の identity と評価可能性。
    indicator/signal はこのゲートを課さない (None を返す)。

    **直前修正の申し送り④ (`run_in_sample`/`run_holdout_gate` の欠落引数)**:
    現物シグネチャ `run_in_sample(settings, *, history_conn, symbol, source,
    intent_source, eval_timeframe, plugin_ref, content_hash, kind, now,
    record_fn=None)` (7-D) は `settings`/`history_conn`/`intent_source` を
    必須で要求する。`history_conn` は明示指定が無ければ `conn` を再利用する
    (既存 `plugin/approval.py:_validate_strategy` と同じパターン — この
    改善ループの DB は単一ファイルであり、`backtest_runs`/`approval_requests`
    は同じ接続で読み書きできる)。`intent_source` は候補ごとに
    `strategy_adapter.build_intent_source(meta, conn=conn, pair=pair,
    source=settings.backtest.eval_source, settings=settings)` で組み立て、
    `_validate_strategy`
    と同じ try/finally で `close()` する (サンドボックスプロセスのリーク防止) —
    in-sample ループと holdout ループはそれぞれ独立に構築・close する
    (閉じた intent_source は使い回せない)。

    **`meta` は `intent_source` 構築のためだけに使う — identity/persist に
    使う `content_hash`/`timeframe` は常に明示引数 (10.6 節が再計算した値)
    が正**。`meta.content_hash`/`meta.timeframe` が引数と食い違っていても
    引数側が勝つ (10.6 節のハッシュ再照合の意図を保つ)。`kind != "strategy"`
    のときは `meta` を一切参照せず早期 return するため `meta=None` で呼べる。
    `eval_timeframe` は `plugin/approval.py:_eval_timeframe` と同じ写像
    (`"1d"` → `"24h"`) を明示引数 `timeframe` へ適用する — 適用しないと
    `bless`/`_validate_strategy` が書く行と `timeframe` 列が食い違い、
    §4.2-4 の baseline/candidate identity が崩れる。

    `record_fn` は `holdout.run_in_sample`/`run_holdout_gate` の
    non-committing sink (Task 7-D、`_run_scope` の `record_fn(save_kwargs)`
    契約 — 単一の dict を位置引数で渡す形。`ImproveLoop.commit` 手順4は
    ここへ `list.append` を渡し、蓄積した行を Tx-2 (`_persist_gate_rows`)
    へ渡す (設計書 §4.1「long-running work is outside tx」、3 周目レビュー
    Important-2)。`None` (既定) のときは `holdout` 側が即時 commit する
    従来経路のまま (Task 11 の `bless --from _human` など Tx-2 の外から
    呼ぶ経路はこちらを使う)。"""
    if kind != "strategy":
        return None
    # R-i3 追随 (プラン10 Task 11): `plugin/approval.py::run_kind_gate` は
    # 検証済みの `meta` (PluginMeta) だけを持って呼ぶ (switch.py の候補は
    # 直前に discover した鮮度の高い meta なので name/pairs/timeframe/
    # content_hash は meta 由来で正しい — 10.6 節の「明示引数が正」の懸念は
    # ImproveLoop.commit のように meta が使い回されて古くなり得る経路の話
    # であり、switch.py の一発ゲートには当てはまらない)。明示引数が渡され
    # ればそちらを常に優先する (10.6 節の契約は維持)。
    name = meta.name if name is None else name
    pairs = list(meta.pairs) if pairs is None else pairs
    timeframe = meta.timeframe if timeframe is None else timeframe
    content_hash = meta.content_hash if content_hash is None else content_hash
    history_conn = conn if history_conn is None else history_conn
    run_in_sample = run_in_sample_fn or holdout.run_in_sample
    run_holdout = run_holdout_gate_fn or holdout.run_holdout_gate
    plugin_ref = f"plugins/{name}"
    eval_timeframe = _eval_timeframe(timeframe)

    # baseline の要否 (approved 同名 strategy の有無) は per_pair の結果に
    # 依存しない純粋な参照なので、in-sample ループより前に確定できる。
    # ここで確定させておくことで、no_strategy になる場合だけ record_fn を
    # 捕捉ラッパーへ差し替える (approved-baseline のときは呼び出し元の
    # record_fn をそのまま転送する既存契約を保つ — 3 周目レビュー
    # Important-2 の pin `test_record_fn_is_forwarded_to_run_in_sample_
    # and_run_holdout` が「record_fn はそのまま転送される」ことを要求する
    # ため)。
    approved_row = conn.execute(
        "SELECT payload_json FROM approval_requests WHERE kind='plugin' "
        "AND status='approved' AND json_extract(payload_json,'$.name')=? "
        "AND json_extract(payload_json,'$.kind')='strategy' "
        "ORDER BY id DESC LIMIT 1", (name,)).fetchone()

    from agentic_fx.store import backtest_runs as backtest_runs_store

    # no_strategy baseline (§4.2-4): 候補の in-sample 行 (pair ごと) の
    # identity (period/settings_hash/core_commit/initial_balance/source/
    # timeframe) をそのまま複製し、plugin_ref/variant/metrics だけ
    # no_strategy 用に差し替えて record_fn/Tx-2 の既存経路 (record_fn is
    # None のときは即時 save、`holdout._run_scope` と同じ形) へ流す。
    # `baseline_row` (approval payload 添付用の JSON-safe 4 key dict) とは
    # 別物 — `period` に datetime を含む行を payload に混ぜると
    # `approvals_store.create` の json.dumps で TypeError になる。
    in_sample_rows: list[dict] = []
    if approved_row is None:
        def _in_sample_record_fn(row: dict) -> None:
            in_sample_rows.append(row)
            if record_fn is not None:
                record_fn(row)
            else:
                backtest_runs_store.save_harness_run(history_conn, **row)
    else:
        _in_sample_record_fn = record_fn

    dataset = settings.backtest.dataset()
    per_pair = {}
    for pair in pairs:
        intent_source = strategy_adapter.build_intent_source(
            meta, conn=conn, pair=pair, dataset=dataset,
            settings=settings)
        try:
            per_pair[pair] = run_in_sample(
                settings, history_conn=history_conn, symbol=pair,
                dataset=dataset, intent_source=intent_source,
                eval_timeframe=eval_timeframe, plugin_ref=plugin_ref,
                content_hash=content_hash, kind="strategy", now=now,
                record_fn=_in_sample_record_fn)
        finally:
            intent_source.close()
    total_trades = sum(m["trades"] for m in per_pair.values())
    evaluable = total_trades >= EVALUABLE_MIN_TRADES
    if not evaluable:
        return StrategyGateVerdict(
            evaluable=False,
            observation_reason=f"insufficient_trades:{total_trades}")

    for pair in pairs:
        intent_source = strategy_adapter.build_intent_source(
            meta, conn=conn, pair=pair, dataset=dataset,
            settings=settings)
        try:
            run_holdout(
                settings, history_conn=history_conn, symbol=pair,
                dataset=dataset, intent_source=intent_source,
                eval_timeframe=eval_timeframe, plugin_ref=plugin_ref,
                content_hash=content_hash, kind="strategy", now=now,
                record_fn=record_fn)
        finally:
            intent_source.close()

    if approved_row is not None:
        baseline_row = {"ref_plugin_ref": plugin_ref, "variant": "baseline"}
        return StrategyGateVerdict(
            evaluable=True, baseline_variant="baseline",
            baseline_row=baseline_row, candidate_metrics=per_pair)

    for row in in_sample_rows:
        no_strategy_row = dict(row)
        no_strategy_row["plugin_ref"] = f"no_strategy:{name}"
        no_strategy_row["variant"] = "no_strategy"
        no_strategy_row["metrics"] = {
            "trades": 0, "pf": None, "win_rate": None, "avg_r": None,
            "max_drawdown": 0.0, "total_pnl": 0.0, "evaluable": False,
            "fallback_spread_used": False}
        if record_fn is not None:
            record_fn(no_strategy_row)
        else:
            backtest_runs_store.save_harness_run(
                history_conn, **no_strategy_row)

    baseline_row = {"plugin_ref": f"no_strategy:{name}",
                    "content_hash": content_hash, "kind": "strategy",
                    "variant": "no_strategy"}
    return StrategyGateVerdict(
        evaluable=True, baseline_variant="no_strategy",
        baseline_row=baseline_row, candidate_metrics=per_pair)
