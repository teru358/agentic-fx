"""commit() 手順5: 承認申請 payload の台帳由来フィールド
(設計書 §4.2-5、プラン §8.1-16)。"""
from __future__ import annotations

from datetime import datetime

import pytest

from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger


def test_payload_analysis_fields_come_from_ledger_not_agent_output(
        loop_min, conn):
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0,
                                                        "run_backtest": 600.0})
    ledger.record(opaque_ref="r1", kind="analyze_corr", params={},
                  result_summary={}, trial_count=25)
    ledger.record(opaque_ref="r2", kind="analyze_corr", params={},
                  result_summary={}, trial_count=10)
    ledger.record(opaque_ref="r3", kind="run_backtest", params={},
                  result_summary={}, trial_count=0)
    ledger.freeze()

    output = {"selection_rationale": "agent claims analysis_run_ids=[1,2,3,4,5,6,7,8,9,10]"}
    payload = loop_min._build_approval_payload(
        conn, name="myind", kind="indicator", content_hash="h",
        artifact_hash="a", ctx_ledger=ledger, mission_id=1, backlog_id=2,
        candidate_origin="staging", candidate_path="plugins/_staging/1/myind",
        gate_metrics={}, output=output, now=datetime(2026, 8, 22))

    assert payload["trial_count"] == 35  # sum(25, 10, 0)
    assert payload["analysis_call_count"] == 2  # analyze_corr の呼出数
    assert payload["backtest_call_count"] == 1
    assert "agent claims" not in str(payload.get("analysis_run_ids", ""))


def test_payload_trial_count_is_sum_not_call_count(loop_min, conn):
    """`trial_count` は呼出回数ではなく計算した相関値の個数の総和
    (lead-lag 1 呼出しで 25 になり得る、という設計書の規範を数値で pin)。"""
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0})
    ledger.record(opaque_ref="r1", kind="analyze_corr", params={},
                  result_summary={}, trial_count=25)
    ledger.freeze()
    payload = loop_min._build_approval_payload(
        conn, name="myind", kind="indicator", content_hash="h",
        artifact_hash="a", ctx_ledger=ledger, mission_id=1, backlog_id=2,
        candidate_origin="staging", candidate_path="plugins/_staging/1/myind",
        gate_metrics={}, output={"selection_rationale": ""},
        now=datetime(2026, 8, 22))
    assert payload["trial_count"] == 25
    assert payload["analysis_call_count"] == 1


def test_frozen_ledger_rejects_late_record_calls(loop_min):
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0})
    ledger.freeze()
    ledger.record(opaque_ref="late", kind="analyze_corr", params={},
                  result_summary={}, trial_count=999)  # 例外にしない — 無視
    assert ledger.entries() == []  # 遅延結果は捨てる
