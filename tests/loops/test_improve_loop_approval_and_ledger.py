"""commit() 手順5: 承認申請 payload の台帳由来フィールド
(設計書 §4.2-5、プラン §8.1-16)。"""
from __future__ import annotations

import threading
from datetime import datetime

import pytest

from agentic_fx.loops.improve_loop import accepted_entries
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.tools.improve_rpc_tools import RpcOutcome


def test_accepted_entries_excludes_error_results():
    good = {"kind": "analyze_corr", "result_summary": {"trial_count": 2}}
    failed = {"kind": "run_backtest", "result_summary": {"error": "boom"}}
    assert accepted_entries([good, failed]) == [good]


def test_approval_payload_counts_only_accepted_entries(loop_min, conn):
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ledger.record(opaque_ref="ok", kind="analyze_corr", params={},
                  result_summary={"trial_count": 4}, trial_count=4)
    ledger.record(opaque_ref="bad", kind="analyze_corr", params={},
                  result_summary={"error": "boom"}, trial_count=99)
    ledger.freeze()
    payload = loop_min._build_approval_payload(
        conn, name="x", kind="indicator", content_hash="c",
        artifact_hash="a", ctx_ledger=ledger, mission_id=1,
        backlog_id=1, candidate_origin="staging", candidate_path="p",
        gate_metrics={}, output={}, now=loop_min._clock.now())
    assert payload["trial_count"] == 4
    assert payload["analysis_call_count"] == 1
    assert payload["backtest_call_count"] == 0


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


def test_build_worker_runner_records_private_outcome_and_releases(loop_and_ctx):
    loop, ctx, _conn = loop_and_ctx
    ctx.rpc_handlers.update({
        "run_backtest": lambda args: {}, "analyze_corr": lambda args: {}})
    runner = loop._build_worker_runner(ctx)
    assert runner._on_rpc_begin("run_backtest") is True
    private = {"trial_count": 4, "archive_tmp": "/tmp/archive",
               "artifact_hash": "abc"}
    runner._on_rpc_accepted(
        "run_backtest", {"name": "myst", "pair": "USDJPY"},
        RpcOutcome(public={"trial_count": 1}, private=private))
    ctx.ledger.freeze()
    assert ctx.ledger.entries() == [{
        "opaque_ref": "run_backtest:myst:USDJPY", "kind": "run_backtest",
        "params": {"name": "myst", "pair": "USDJPY"},
        "result_summary": private, "trial_count": 4}]


def test_build_worker_runner_accepted_preprocessing_failure_still_releases(
        loop_and_ctx):
    """codex 1 周目 Important (2026-09-10): summary / opaque_ref の前処理で
    例外になっても予約は解放される (freeze が drain を待たない)。"""
    loop, ctx, _conn = loop_and_ctx
    ctx.rpc_handlers.update({
        "run_backtest": lambda args: {}, "analyze_corr": lambda args: {}})
    runner = loop._build_worker_runner(ctx)
    assert runner._on_rpc_begin("run_backtest") is True
    with pytest.raises(KeyError):
        runner._on_rpc_accepted(  # args に pair が無い → 前処理で KeyError
            "run_backtest", {"name": "myst"},
            RpcOutcome(public={"trial_count": 1}, private=None))
    dropped = []
    ctx.ledger.freeze(drain_timeout_sec=0.0, on_timeout=dropped.append)
    assert dropped == []
    assert ctx.ledger.entries() == []


def test_payload_base_interval_follows_backtest_settings_for_strategy(
        loop_min, conn):
    """A6 (v3 設計): `_build_approval_payload` の base_interval/eval_timeframe
    は strategy kind のとき `settings.backtest.dataset().base_interval` /
    gate_metrics の meta.timeframe を反映する。既定 "1m" のままだと "1m"
    固定リテラルへの変異が生き残るため "5m" に設定して区別する (段階 2
    レビュー是正)。indicator kind は両方 null。"""
    loop_min._settings = loop_min._settings.model_copy(update={
        "backtest": loop_min._settings.backtest.model_copy(
            update={"base_interval": "5m"})})
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ledger.freeze()

    class _Meta:
        timeframe = "1h"

    payload = loop_min._build_approval_payload(
        conn, name="myst", kind="strategy", content_hash="h",
        artifact_hash="a", ctx_ledger=ledger, mission_id=1, backlog_id=2,
        candidate_origin="staging", candidate_path="plugins/_staging/1/myst",
        gate_metrics={"meta": _Meta()}, output={"selection_rationale": ""},
        now=datetime(2026, 8, 22))
    assert payload["base_interval"] == "5m"
    assert payload["eval_timeframe"] == "1h"

    payload_indicator = loop_min._build_approval_payload(
        conn, name="myind", kind="indicator", content_hash="h",
        artifact_hash="a", ctx_ledger=ledger, mission_id=1, backlog_id=2,
        candidate_origin="staging", candidate_path="plugins/_staging/1/myind",
        gate_metrics={}, output={"selection_rationale": ""},
        now=datetime(2026, 8, 22))
    assert payload_indicator["base_interval"] is None
    assert payload_indicator["eval_timeframe"] is None


def test_payload_eval_timeframe_maps_1d_to_24h_for_strategy(loop_min, conn):
    """写像非対称是正 (codex 段階2/3 是正 1周目): `_build_approval_payload`
    (improve_loop 系統) は `meta.timeframe` を生のまま payload の
    `eval_timeframe` に載せていた — switch.py の submit/bless と同じ欠陥
    (approval.py だけが "1d"→"24h" 写像を適用していた)。写像は
    `strategy_gate._eval_timeframe` に一元化し、4 系統すべてがこれを
    経由すること。
    """
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={})
    ledger.freeze()

    class _Meta:
        timeframe = "1d"

    payload = loop_min._build_approval_payload(
        conn, name="myst_1d", kind="strategy", content_hash="h",
        artifact_hash="a", ctx_ledger=ledger, mission_id=1, backlog_id=2,
        candidate_origin="staging", candidate_path="plugins/_staging/1/myst_1d",
        gate_metrics={"meta": _Meta()}, output={"selection_rationale": ""},
        now=datetime(2026, 8, 22))
    assert payload["eval_timeframe"] == "24h"


def test_accepted_records_public_when_private_is_absent_or_empty(loop_and_ctx):
    """ローカル 1 周目 #12 (2026-09-10): `summary = outcome.private or
    outcome.public` の分岐。private=None (analyze_corr の本番形) と
    private={} の両方で public に落ちる。既存テストは非空 private しか
    通さないため、`is not None` 判定に置き換えても緑のままだった。
    (private={} は現行の本番経路では到達しないが、summary 選択の契約を
    一意に固定するために合わせて pin する。)"""
    loop, ctx, _conn = loop_and_ctx
    ctx.rpc_handlers.update({
        "run_backtest": lambda args: {}, "analyze_corr": lambda args: {}})
    runner = loop._build_worker_runner(ctx)
    for private in (None, {}):
        assert runner._on_rpc_begin("run_backtest") is True
        runner._on_rpc_accepted(
            "run_backtest", {"name": "myst", "pair": "USDJPY"},
            RpcOutcome(public={"trial_count": 2}, private=private))
    ctx.ledger.freeze()
    entries = ctx.ledger.entries()
    assert [e["result_summary"] for e in entries] == [
        {"trial_count": 2}, {"trial_count": 2}]
    assert [e["trial_count"] for e in entries] == [2, 2]


def test_accepted_analyze_corr_opaque_ref_is_per_request(loop_and_ctx):
    """ローカル 1 周目 #13 (2026-09-10): analyze_corr の else 側
    (`opaque_ref = f"analyze_corr:{id(request)}"` と `params = request`) が
    一度も踏まれていない。callback を叩くテストは run_backtest しか通さず、
    opaque_ref を固定文字列にしても緑のままだった。"""
    loop, ctx, _conn = loop_and_ctx
    ctx.rpc_handlers.update({
        "run_backtest": lambda args: {}, "analyze_corr": lambda args: {}})
    runner = loop._build_worker_runner(ctx)
    for request in ({"pairs": ["USDJPY"]}, {"pairs": ["EURUSD"]}):
        assert runner._on_rpc_begin("analyze_corr") is True
        runner._on_rpc_accepted(
            "analyze_corr", {"request": request},
            RpcOutcome(public={"trial_count": 1}, private=None))
    ctx.ledger.freeze()
    entries = ctx.ledger.entries()
    assert [e["kind"] for e in entries] == ["analyze_corr", "analyze_corr"]
    assert [e["params"] for e in entries] == [
        {"pairs": ["USDJPY"]}, {"pairs": ["EURUSD"]}]
    assert entries[0]["opaque_ref"] != entries[1]["opaque_ref"]
    assert all(e["opaque_ref"].startswith("analyze_corr:") for e in entries)


def test_freeze_ledger_logs_timeout_activity_when_drain_expires(
        loop_and_ctx, monkeypatch):
    """ローカル 1 周目 #14 (2026-09-10): `_freeze_ledger` が drain timeout で
    activity へ `ledger_freeze_timeout` を書くこと。この文字列は tests/ の
    どこにも現れず、on_timeout ごと消しても activity キーを書き換えても
    緑のままだった。commit 相の drain timeout は運用の唯一の signal。"""
    loop, ctx, _conn = loop_and_ctx
    monkeypatch.setattr(loop._settings.improve, "accept_drain_sec", 0.0)
    ctx.ledger.begin_accept()          # 解放されない予約を 1 件残す
    loop._freeze_ledger(ctx)
    log = (loop._root / "activity.log").read_text()
    assert "ledger_freeze_timeout" in log
    assert f"mission={ctx.mission_id} dropped=1" in log


def test_freeze_ledger_drain_waits_accept_drain_sec_before_dropping(
        loop_and_ctx, monkeypatch):
    """ローカル 1 周目 #15 (2026-09-10): drain 秒数が `accept_drain_sec` から
    来ていること。`drain_timeout_sec=0.0` に潰す変異は、timeout 側だけを見る
    テストでは検出できない。期限内に end_accept が来れば timeout activity は
    書かれない。"""
    loop, ctx, _conn = loop_and_ctx
    monkeypatch.setattr(loop._settings.improve, "accept_drain_sec", 2.0)
    reservation = ctx.ledger.begin_accept()
    timer = threading.Timer(0.2, ctx.ledger.end_accept, args=(reservation,))
    timer.start()
    try:
        loop._freeze_ledger(ctx)
    finally:
        timer.cancel()
    log_path = loop._root / "activity.log"
    log = log_path.read_text() if log_path.exists() else ""
    assert "ledger_freeze_timeout" not in log
    assert ctx.ledger.entries() == []
