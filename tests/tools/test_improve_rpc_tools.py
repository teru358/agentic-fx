"""improve registry の RPC ツール (run_backtest/analyze_corr、設計書 §3.4)。

**この層は台帳への record と handler 呼び出しの配線だけを検証する** —
RPC の実 timeout/直列化は WorkerRunner の tool_rpc フレーム層 (Task 4/9) の
責務であり、ここでは handler が同期関数として渡された前提でテストする。
"""
from __future__ import annotations

import json
import re

from agentic_fx.backtest.analysis import analyze_for_agent
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.tools.improve_rpc_tools import _FORBIDDEN_KEYS, build_improve_rpc_tooldefs

from tests.backtest.factories import _conn
from tests.backtest.test_analysis import (
    BEFORE_BOUNDARY, NOW, _seed_two_series, _settings_watch_eurusd,
)

_ISO_DATETIME_LEAF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _walk_leaves(value):
    """dict/list を再帰展開して (キー列, 葉値列) を返す
    (`tests/backtest/test_analysis.py::_leaves` と同型 — B2 pin 専用に
    ここでも保持する)。"""
    keys, leaves = [], []
    if isinstance(value, dict):
        for k, v in value.items():
            keys.append(k)
            k2, l2 = _walk_leaves(v)
            keys += k2; leaves += l2
    elif isinstance(value, (list, tuple)):
        for v in value:
            k2, l2 = _walk_leaves(v)
            keys += k2; leaves += l2
    else:
        leaves.append(value)
    return keys, leaves


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


def test_analyze_corr_recursively_strips_nested_period_endpoints(tmp_path):
    """B2 (検収 Blocking): `analyze_for_agent(persist=False)` は
    `{"params": {"request": ..., "in_sample_until": <ISO>}, ...}` を
    `payload_body` にマージして返す — `in_sample_until` は入れ子
    (`params.in_sample_until`) にしか現れないため、`_strip_forbidden` が
    トップレベルのキー名照合しかしないと agent へ漏れる (検収実測で再現)。

    実 `analyze_for_agent(persist=False)` を `analyze_corr_handler` に
    据え、agent が受け取る戻り値を再帰的に走査して `_FORBIDDEN_KEYS` の
    いずれのキーも・ISO 日時らしき文字列 (leaf) も残っていないことを見る。
    """
    conn = _conn(tmp_path)
    _seed_two_series(conn, start=BEFORE_BOUNDARY)

    def analyze_corr_handler(request: dict) -> dict:
        return analyze_for_agent(conn, _settings_watch_eurusd(), request,
                                 now=NOW, persist=False)

    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0})
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger, run_backtest_handler=lambda a: {},
        analyze_corr_handler=analyze_corr_handler)}

    out = tools["analyze_corr"].func(
        request={"kind": "lead_lag", "a": "USDJPY", "b": "EURUSD",
                 "timeframe": "1h"})

    # 前提: 漏洩元の経路そのものが実際に呼ばれていること (handler が
    # エラーコードを返すだけの空振りだと pin が意味を持たない)。
    assert "error" not in out, out

    keys, leaves = _walk_leaves(out)
    assert _FORBIDDEN_KEYS.isdisjoint(set(keys)), (
        f"agent 戻り値に禁止キーが残っている: {set(keys) & _FORBIDDEN_KEYS}")
    for leaf in leaves:
        assert not (isinstance(leaf, str) and _ISO_DATETIME_LEAF_RE.match(leaf)), (
            f"agent 戻り値に ISO 日時らしき文字列が残っている: {leaf!r}")

    # 台帳側は strip 前の dict を保持する (Task 8 の _persist_ledger_rows が
    # 読むための契約) — B2 是正が台帳を痩せさせていないことも確認する。
    ledger.freeze()
    assert "in_sample_until" in ledger.entries()[0]["result_summary"]["params"]
