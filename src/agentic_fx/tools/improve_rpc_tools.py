"""improve registry の RPC ツール — run_backtest / analyze_corr
(設計書 §3.4)。親 RPC 経由 (WorkerRunner の tool_rpc フレーム) で実行される
handler をラップし、台帳への record と遮断 7 のキー剥がしだけを行う。"""
from __future__ import annotations

from typing import Callable

from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.tools.registry import ToolDef

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
    "period", "now"})


def _strip_forbidden(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in _FORBIDDEN_KEYS}


def build_improve_rpc_tooldefs(
        *, ledger: ImproveRpcLedger,
        run_backtest_handler: Callable[[dict], dict],
        analyze_corr_handler: Callable[[dict], dict]) -> list[ToolDef]:
    def run_backtest(name: str, pair: str) -> dict:
        result = run_backtest_handler({"name": name, "pair": pair})
        ledger.record(opaque_ref=f"run_backtest:{name}:{pair}",
                      kind="run_backtest", params={"name": name, "pair": pair},
                      result_summary=result,
                      trial_count=result.get("trial_count", 1))
        return _strip_forbidden(result)

    def analyze_corr(request: dict) -> dict:
        result = analyze_corr_handler(request)
        ledger.record(opaque_ref=f"analyze_corr:{id(request)}",
                      kind="analyze_corr", params=request,
                      result_summary=result,
                      trial_count=result.get("trial_count", 1))
        return _strip_forbidden(result)

    return [
        ToolDef(name="run_backtest", description="親が in-sample バックテストを回す。",
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
