"""improve registry の RPC ツール (run_backtest/analyze_corr、設計書 §3.4)。

**この層は台帳への record と handler 呼び出しの配線だけを検証する** —
RPC の実 timeout/直列化は WorkerRunner の tool_rpc フレーム層 (Task 4/9) の
責務であり、ここでは handler が同期関数として渡された前提でテストする。
"""
from __future__ import annotations

import json

from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.tools.improve_rpc_tools import build_improve_rpc_tooldefs


def test_run_backtest_records_to_ledger_and_returns_handler_result():
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    calls = []

    def handler(args):
        calls.append(args)
        return {"metrics": {"pf": 1.2}, "trial_count": 1}

    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger, run_backtest_handler=handler,
        analyze_corr_handler=lambda a: {})}
    out = tools["run_backtest"].func(name="rsi_v2", pair="USDJPY")
    assert out["metrics"] == {"pf": 1.2}
    assert "period_start" not in json.dumps(out)   # 遮断 8: 期間端点を返さない
    ledger.freeze()
    assert len(ledger.entries()) == 1
    assert ledger.entries()[0]["kind"] == "run_backtest"


def test_analyze_corr_records_trial_count_from_handler():
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0})
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger, run_backtest_handler=lambda a: {},
        analyze_corr_handler=lambda a: {"trial_count": 25, "summary": {}})}
    tools["analyze_corr"].func(request={"kind": "corr_matrix"})
    ledger.freeze()
    assert ledger.entries()[0]["trial_count"] == 25


def test_run_backtest_does_not_expose_period_or_datetime_keys():
    """遮断 7: analyze_corr/run_backtest の返却 schema に日時・期間端点が無い。"""
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger,
        run_backtest_handler=lambda a: {
            "metrics": {"pf": 1.0}, "trial_count": 1,
            "period_start": "2020-01-01"},  # handler が誤って混入させたケース
        analyze_corr_handler=lambda a: {})}
    out = tools["run_backtest"].func(name="x", pair="USDJPY")
    assert "period_start" not in out and "period_end" not in out
