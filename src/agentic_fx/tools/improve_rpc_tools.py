"""improve registry の RPC ツール — run_backtest / analyze_corr
(設計書 §3.4)。親 RPC 経由 (WorkerRunner の tool_rpc フレーム) で実行される
handler をラップし、台帳への record と遮断 7 のキー剥がしだけを行う。"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.plugin import loader as plugin_loader
from agentic_fx.tools.improve_staging_tools import _safe_join
from agentic_fx.tools.registry import ToolDef

if TYPE_CHECKING:
    from agentic_fx.config import ImproveToolBudgetSettings
    from agentic_fx.tools.mission_counters import MissionToolCounters

_RUN_BACKTEST_KIND_HINT = (
    "indicator/signal plugin はバックテストできません "
    "(決済規則を持たないため、設計 §6)。self-test (run_plugin_tests) で検証し、"
    "そのまま最終出力へ進んでください。strategy を作る場合は config.yaml に "
    "kind: strategy / timeframe / pairs / exit_mode / max_bars を書きます "
    '(read_example_plugin(name="sma_cross") 参照)')

_FORBIDDEN_KEYS = frozenset({
    "period_start", "period_end", "start", "end", "window", "timestamps",
    # `period`/`now` (10.9 節 Step 11 追記): `_build_rpc_handlers` の
    # run_backtest handler は `_run_scope` の save_kwargs (`period=
    # (period_start, period_end)` タプル、`now` datetime) をそのまま
    # 戻り値へ混ぜて ledger.record の result_summary に載せる (10.10 節
    # `_persist_ledger_rows` が読むための契約)。この 2 キーを剥がさないと
    # `period_start`/`period_end` という平坦キーは無くても `period` タプル
    # 経由・`now` 経由で期間端点/日時が agent へ漏れ、遮断7 の趣旨
    # (「返却 schema に日時・期間端点…が無い」) に反する。
    "period", "now",
    # `in_sample_until` (Task 7 検収 B2 是正): `analyze_for_agent(persist=
    # False)` (`backtest/analysis.py`) は `save_params = {"params": {
    # "request": ..., "in_sample_until": <ISO 文字列>}, "trial_count": ...,
    # "source": ...}` をそのまま `payload_body` にマージして返す
    # (`persist=False` の分岐)。`params` はトップレベルの禁止キーではなく、
    # `in_sample_until` はその**入れ子**にしか現れないため、剥がすキーの
    # 集合を平坦に列挙するだけでは足りない — `_strip_forbidden` 自体を
    # 再帰化し、`in_sample_until` を禁止キーに加える。
    "in_sample_until"})


def _strip_forbidden(value: object) -> object:
    """§遮断7: 禁止キーを dict/list の任意の深さから剥がして投影する
    (Task 7 検収 B2 是正 — 元は浅い (トップレベルのみ) 実装で、
    `analyze_corr` (persist=False 経路) の `params.in_sample_until` を
    見落としていた)。

    **台帳は痩せない**: `build_improve_rpc_tooldefs` 内の `ledger.record`
    呼び出しは strip **前**の `result` をそのまま渡す (10.10 節
    `_persist_ledger_rows` の契約) — ここでの再帰化は agent への戻り値の
    投影にのみ影響し、台帳の記録内容は変えない。"""
    if isinstance(value, dict):
        return {k: _strip_forbidden(v) for k, v in value.items()
                if k not in _FORBIDDEN_KEYS}
    if isinstance(value, (list, tuple)):
        # L21: tuple 要素も list と同じく再帰する (型は保存する)。
        stripped = [_strip_forbidden(v) for v in value]
        return tuple(stripped) if isinstance(value, tuple) else stripped
    return value


def _is_successful_backtest(result: object) -> bool:
    """設計 v4 Tier B の「成功した run_backtest」= error が無く、metrics が
    **evaluable** (trades ≥ 1 を含意) な応答。空 dict / metrics 欠落 (codex 1 周目
    Important 1) に加え、trades=0 / evaluable=false の空振り backtest も成功に
    数えない — run7 #63 (2026-09-08) で空振り 1 本により Tier B が恒久解除され、
    モデルが self-test 修正ループへ復帰した ([tier-b-release-requires-evaluable-
    backtest])。directive「実データで trade が出れば plugin は正しい」と一致させる。
    正しい plugin でも期間次第で trades=0 になりうるが、その場合の次の一手は
    パラメータ/期間を変えた backtest (予算 6) であってテストの修正ではない。"""
    if not (isinstance(result, dict) and "error" not in result):
        return False
    metrics = result.get("metrics")
    return isinstance(metrics, dict) and metrics.get("evaluable") is True


def build_improve_rpc_tooldefs(
        *, ledger: ImproveRpcLedger,
        run_backtest_handler: Callable[[dict], dict],
        analyze_corr_handler: Callable[[dict], dict],
        staging_dir: Path | None = None,
        counters: "MissionToolCounters | None" = None,
        budget: "ImproveToolBudgetSettings | None" = None) -> list[ToolDef]:
    if (counters is None) != (budget is None):
        raise ValueError("counters と budget は両方渡すか両方省く")

    def run_backtest(name: str, pair: str) -> dict:
        if staging_dir is not None:
            candidate_dir = _safe_join(staging_dir, name)
            if candidate_dir is None:
                return {"error": "invalid candidate name", "candidate_kind": None,
                        "hint": _RUN_BACKTEST_KIND_HINT}
            meta, reason = plugin_loader.discover_one_with_reason(candidate_dir, name)
            if meta is None:
                return {"error": f"loader_rejected: {reason}", "candidate_kind": None,
                        "hint": _RUN_BACKTEST_KIND_HINT}
            if meta.kind != "strategy":
                return {"error": "run_backtest is only for kind=strategy candidates",
                        "candidate_kind": meta.kind,
                        "hint": _RUN_BACKTEST_KIND_HINT}
        # codex 1 周目 Important 2 (2026-09-07): 予算の消費は候補名・loader・kind
        # 検証の **後**。誤呼び出し (名前不正 / loader 拒否 / indicator) で候補
        # あたり枠を減らさない — 設計 v4 Tier C の目的は「実 backtest の修正
        # ループ」の上限であって、検証エラーは対象外。
        if counters is not None and not counters.reserve_backtest(
                    name, budget.max_backtests_per_candidate):
            from agentic_fx.tools.improve_staging_tools import BUDGET_EXHAUSTED_DIRECTIVE
            counters.record_terminal_refusal()
            return {"error": "budget exhausted",
                    "budget": "max_backtests_per_candidate",
                    "directive": BUDGET_EXHAUSTED_DIRECTIVE}
        result = run_backtest_handler({"name": name, "pair": pair})
        if counters is not None:
            backtest_ok = _is_successful_backtest(result)
            counters.record_backtest_result(name, ok=backtest_ok)
            if backtest_ok:
                counters.record_progress(name, "backtest_ok")
        # 台帳は永続化用 save_kwargs (period/now 込み) を読む契約 —
        # agent 向け応答 (JSON-safe、期間端点なし) とは別物として受け取る。
        ledger_result = getattr(result, "save_kwargs", result)
        ledger.record(opaque_ref=f"run_backtest:{name}:{pair}",
                      kind="run_backtest", params={"name": name, "pair": pair},
                      result_summary=ledger_result,
                      trial_count=result.get("trial_count", 1))
        response = _strip_forbidden(result)
        if counters is not None:
            response["remaining_budget"] = {
                "backtests_for_candidate": max(
                    budget.max_backtests_per_candidate
                    - counters.backtest_calls[name], 0)}
        return response

    def analyze_corr(request: dict) -> dict:
        result = analyze_corr_handler(request)
        ledger.record(opaque_ref=f"analyze_corr:{id(request)}",
                      kind="analyze_corr", params=request,
                      result_summary=result,
                      trial_count=result.get("trial_count", 1))
        return _strip_forbidden(result)

    return [
        ToolDef(name="run_backtest", description=(
                    "kind=strategy の候補専用。親が in-sample バックテストを回す。"),
                parameters={"type": "object",
                            "properties": {"name": {"type": "string"},
                                          "pair": {"type": "string"}},
                            "required": ["name", "pair"]},
                func=run_backtest),
        ToolDef(name="analyze_corr", description="親が相関分析を回す。",
                parameters={"type": "object",
                            "properties": {"request": {"type": "object"}},
                            "required": ["request"]},
                func=analyze_corr),
    ]


def build_ledger_wrapped_rpc_handlers(
        *, ledger: ImproveRpcLedger, rpc_handlers: dict[str, Callable],
        staging_dir: Path | None = None) -> dict[str, Callable[[dict], dict]]:
    """親 (WorkerRunner) が子からの tool_rpc を受ける側の handler 表。

    生 handler (`ImproveLoop._build_rpc_handlers`) を tooldef 層で包み、
    台帳記録 (`ledger.record`) と遮断 7 (`_strip_forbidden`) を親側でも
    通す — 生 handler を直接 `WorkerRunner(rpc_handlers=...)` に渡すと
    親 ledger は永遠に空で `_persist_ledger_rows` が何も書かない
    ([ledger-never-populated-in-production]、/code-review 2026-09-04)。
    子側の wrapper (mission_worker、使い捨て ledger) は二重防御として残す。"""
    tools = build_improve_rpc_tooldefs(
        ledger=ledger,
        run_backtest_handler=rpc_handlers["run_backtest"],
        analyze_corr_handler=rpc_handlers["analyze_corr"],
        staging_dir=staging_dir)
    return {tool.name: (lambda args, func=tool.func: func(**args))
            for tool in tools}
