"""evaluate_strategy_adoption_gate — 戦略採用ゲート (設計書 §4.2-4、
プラン §8.1-40)。`ImproveLoop.commit` 手順4 と Task 11 の
`bless --from _human` (§8.1-41) の両方から import される共有モジュール
(統合裁定 R-i3 — plugin/ 配下の独立ファイルに置く)。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal

from agentic_fx.backtest import holdout
from agentic_fx.backtest.metrics import EVALUABLE_MIN_TRADES
from agentic_fx.plugin import strategy_adapter
from agentic_fx.plugin.loader import PluginMeta

# timeframe 正規化の単一所有者 (codex 段階2/3 是正 1周目): `plugin/
# approval.py` はこの辞書を再実装せず `strategy_gate._eval_timeframe` を
# 直接参照する。switch.py (submit/bless) と improve_loop.py の payload
# 組み立ても同じ関数を経由すること (4 系統の eval_timeframe 写像を 1 箇所
# に集約する)。
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
    # [profitability-floor] T1 Step 1-2 (2026-09-12、設計書 §3 T1-a):
    # `evaluable` は R8 の「標本不足」意味論を流用しない (別フィールド)。
    floor_reason: str = ""
    floor_detail: str = ""


def _check_profitability_floor(
        per_pair: dict, *, settings: "Settings", scope: str) -> tuple[str, str]:
    """[profitability-floor] T1 Step 1-1 (設計書 §3「判定規則 (逐語)」)。

    戻り値 `(label, detail)`。`label` は `""` (合格) か `"unprofitable"`
    (固定文言 — この文字列は `improvement_backlog.last_result` に逐語で
    現れるため変えない)。`detail` は人間向けレポート専用の全数値文字列
    — `label` と混ぜない (遮断 8 の 1 bit 例外を守るため)。

    pair ごとに判定し、**1 pair でも不合格なら候補全体を落とす**
    (pf は pair 間で算術合成できないため合計ではなく pair ごとに見る)。

    順序 ①→②→③ は契約 (codex I9): ① (strict holdout 判定) を ② の
    後に置くと `require_holdout_evaluable=True` でも `trades=0` の pair
    が通ってしまう。
    """
    g = settings.improve.gate
    fail_details: list[str] = []
    for pair, m in per_pair.items():
        # ① strict holdout 判定は zero-trade shortcut より前 (trades==0
        # でも FAIL する)。
        if scope == "holdout" and g.require_holdout_evaluable \
                and not m.get("evaluable"):
            fail_details.append(f"{pair}: holdout_not_evaluable")
            continue
        # ② 成績が無い pair は通常モードでは判定から除外する。
        if m["trades"] == 0:
            continue
        # ③ 既定: holdout の標本不足は「悪いとは言わない」(R8)。
        if scope == "holdout" and not m.get("evaluable"):
            continue
        pf = m["pf"]
        if pf is not None and pf < g.min_pf:  # pf is None (gross_loss==0) は PASS
            fail_details.append(f"{pair}: pf={pf}")
            continue
        avg_r = m["avg_r"]
        if g.require_positive_avg_r and avg_r is not None and avg_r <= 0.0:
            fail_details.append(f"{pair}: avg_r={avg_r}")
    if fail_details:
        return "unprofitable", f"{scope}: " + ", ".join(fail_details)
    return "", ""


def evaluate_strategy_adoption_gate(
    conn, *, meta: "PluginMeta | None", now: datetime, settings: "Settings",
    name: str | None = None, pairs: "list[str] | None" = None,
    timeframe: str | None = None, content_hash: str | None = None,
    kind: str = "strategy",
    history_conn=None,
    run_in_sample_fn=None, run_holdout_gate_fn=None,
    record_fn: "Callable[[dict], None] | None" = None,
    floor_mode: Literal["enforce", "warn"] = "enforce",
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

    # [profitability-floor] T1 Step 1-2 (2026-09-12、設計書 §3):
    # in_sample 段の判定は total_trades 判定の直後 (holdout ループより前)。
    # `floor_mode="enforce"` で不合格なら holdout を回さずに即 return する
    # (落ちる候補に holdout の計算コストを払わない/holdout 情報の発生
    # 自体を無くす — 遮断 8 の 1 bit 例外の前提)。`floor_mode="warn"` の
    # 不合格は `floor_reason`/`floor_detail` を保持したまま完走する
    # (codex C2 — bless で強行する候補にも通常 submit と同じ全ゲート
    # 証跡を残すため)。
    in_sample_floor_reason, in_sample_floor_detail = _check_profitability_floor(
        per_pair, settings=settings, scope="in_sample")
    if in_sample_floor_reason and floor_mode == "enforce":
        return StrategyGateVerdict(
            evaluable=True, floor_reason=in_sample_floor_reason,
            floor_detail=in_sample_floor_detail, candidate_metrics=per_pair)

    holdout_per_pair: dict = {}
    for pair in pairs:
        intent_source = strategy_adapter.build_intent_source(
            meta, conn=conn, pair=pair, dataset=dataset,
            settings=settings)
        try:
            # 現行は戻り値を捨てていた (§0「前提を疑う」) — フロア判定の
            # holdout 段はこの戻り値が要る。
            holdout_per_pair[pair] = run_holdout(
                settings, history_conn=history_conn, symbol=pair,
                dataset=dataset, intent_source=intent_source,
                eval_timeframe=eval_timeframe, plugin_ref=plugin_ref,
                content_hash=content_hash, kind="strategy", now=now,
                record_fn=record_fn)
        finally:
            intent_source.close()

    holdout_floor_reason, holdout_floor_detail = _check_profitability_floor(
        holdout_per_pair, settings=settings, scope="holdout")
    # warn で in_sample も落ちていた場合は floor_detail に両段を含める。
    floor_reason = in_sample_floor_reason or holdout_floor_reason
    floor_detail = " | ".join(
        d for d in (in_sample_floor_detail, holdout_floor_detail) if d)

    if approved_row is not None:
        baseline_row = {"ref_plugin_ref": plugin_ref, "variant": "baseline"}
        return StrategyGateVerdict(
            evaluable=True, baseline_variant="baseline",
            baseline_row=baseline_row, candidate_metrics=per_pair,
            floor_reason=floor_reason, floor_detail=floor_detail)

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
        baseline_row=baseline_row, candidate_metrics=per_pair,
        floor_reason=floor_reason, floor_detail=floor_detail)
