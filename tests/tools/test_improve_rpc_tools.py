"""improve registry の RPC ツール (run_backtest/analyze_corr、設計書 §3.4)。

**この層は台帳への record と handler 呼び出しの配線だけを検証する** —
RPC の実 timeout/直列化は WorkerRunner の tool_rpc フレーム層 (Task 4/9) の
責務であり、ここでは handler が同期関数として渡された前提でテストする。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from agentic_fx.backtest.analysis import analyze_for_agent
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.tools.improve_rpc_tools import (
    _FORBIDDEN_KEYS, _strip_forbidden, build_improve_rpc_tooldefs,
)

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


def test_run_backtest_guides_indicator_without_calling_handler(tmp_path):
    candidate = tmp_path / "rsi_v2"
    candidate.mkdir()
    (candidate / "config.yaml").write_text(
        "kind: indicator\n", encoding="utf-8")
    calls = []
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger, staging_dir=tmp_path,
        run_backtest_handler=lambda args: calls.append(args) or {},
        analyze_corr_handler=lambda args: {})}

    out = tools["run_backtest"].func(name="rsi_v2", pair="USDJPY")

    assert out["error"] == "run_backtest is only for kind=strategy candidates"
    assert out["candidate_kind"] == "indicator"
    assert "run_plugin_tests" in out["hint"]
    assert "設計 §6" in out["hint"]
    assert calls == []


def test_run_backtest_calls_handler_for_strategy_candidate(tmp_path):
    candidate = tmp_path / "sma_cross_v2"
    candidate.mkdir()
    (candidate / "config.yaml").write_text(
        "kind: strategy\n", encoding="utf-8")
    calls = []
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger, staging_dir=tmp_path,
        run_backtest_handler=lambda args: calls.append(args) or {"ok": True},
        analyze_corr_handler=lambda args: {})}

    assert tools["run_backtest"].func(
        name="sma_cross_v2", pair="USDJPY") == {"ok": True}
    assert calls == [{"name": "sma_cross_v2", "pair": "USDJPY"}]


@pytest.mark.parametrize(("name", "config", "expected_error", "expected_kind"), [
    ("missing", None, "config.yaml not found", None),
    ("bad_yaml", "kind: [\n", "config.yaml unreadable", None),
    ("missing_kind", "pairs: []\n", "kind missing", None),
    ("non_str", "kind: 3\n", "kind must be str", 3),
    ("indicator", "kind: indicator\n",
     "run_backtest is only for kind=strategy candidates", "indicator"),
])
def test_run_backtest_fails_closed_for_invalid_candidate_config(
        tmp_path, name, config, expected_error, expected_kind):
    if config is not None:
        candidate = tmp_path / name
        candidate.mkdir()
        (candidate / "config.yaml").write_text(config)
    calls = []
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger, staging_dir=tmp_path,
        run_backtest_handler=lambda args: calls.append(args) or {"ok": True},
        analyze_corr_handler=lambda args: {})}

    out = tools["run_backtest"].func(name=name, pair="USDJPY")
    assert out["error"] == expected_error
    assert out["candidate_kind"] == expected_kind
    assert out["hint"]
    assert calls == []


def test_run_backtest_fails_closed_for_unsafe_candidate_name(tmp_path):
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    calls = []
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger, staging_dir=tmp_path,
        run_backtest_handler=lambda args: calls.append(args) or {},
        analyze_corr_handler=lambda args: {})}
    out = tools["run_backtest"].func(name="../escape", pair="USDJPY")
    assert out["error"] == "config.yaml not found"
    assert out["candidate_kind"] is None and out["hint"]
    assert calls == []


def test_analyze_corr_records_trial_count_from_handler():
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"analyze_corr": 60.0})
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger, run_backtest_handler=lambda a: {},
        analyze_corr_handler=lambda a: {"trial_count": 25, "summary": {}})}
    tools["analyze_corr"].func(request={"kind": "corr_matrix"})
    ledger.freeze()
    assert ledger.entries()[0]["trial_count"] == 25


def test_run_backtest_defaults_trial_count_to_one_when_handler_omits_it():
    """L22: `result.get("trial_count", 1)` の既定値 1 を見るテストが無い
    — trial_count を返さない handler を据え、台帳の trial_count==1 を
    確認する。"""
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger, run_backtest_handler=lambda a: {"metrics": {"pf": 1.0}},
        analyze_corr_handler=lambda a: {})}
    tools["run_backtest"].func(name="x", pair="USDJPY")
    ledger.freeze()
    assert ledger.entries()[0]["trial_count"] == 1


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


# M07 pin: この一覧は **`_FORBIDDEN_KEYS` からは import しない、文字列
# 直書きの固定リスト**にする。`@pytest.mark.parametrize` の引数を
# `_FORBIDDEN_KEYS` から動的に作ると、収集はテスト実行**前** (import 時) に
# 走るため、キーを 1 つ外す変異は該当キーの parametrize ケースそのものを
# 静かに消してしまい (収集数が 1 減るだけ)、red にならず殺せない
# ([[mutation-testing]] のパラメトライズ罠と同型)。固定リストなら、
# 変異後もそのキーのケースは必ず収集され、strip されずに残ったことを
# 直接検出できる。
_EXPECTED_FORBIDDEN_KEYS = (
    "period_start", "period_end", "start", "end", "window", "timestamps",
    "period", "now", "in_sample_until")


def test_forbidden_keys_literal_matches_source_set():
    """`_EXPECTED_FORBIDDEN_KEYS` (固定リスト) と実 `_FORBIDDEN_KEYS` が
    一致することを別途確認する — 固定リストが陳腐化した場合に気づける
    ようにするための対。"""
    assert set(_EXPECTED_FORBIDDEN_KEYS) == set(_FORBIDDEN_KEYS)


@pytest.mark.parametrize("forbidden_key", _EXPECTED_FORBIDDEN_KEYS)
def test_run_backtest_strips_each_forbidden_key_individually(forbidden_key):
    """M07 (段 0 致命): `_FORBIDDEN_KEYS` の各キーを 1 つずつ外す変異が
    red になる pin — トップレベルと入れ子の両方に全キーを仕込んだ dict を
    通し、strip 後にそのキーが (再帰的に) どこにも残っていないことを見る。"""
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    poisoned = {k: f"leak:{k}" for k in _EXPECTED_FORBIDDEN_KEYS}
    poisoned["now"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
    handler_result = {
        "metrics": {"pf": 1.0}, "trial_count": 1,
        **poisoned,
        "nested": dict(poisoned),
    }
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger, run_backtest_handler=lambda a: handler_result,
        analyze_corr_handler=lambda a: {})}
    out = tools["run_backtest"].func(name="x", pair="USDJPY")
    assert forbidden_key not in out
    assert forbidden_key not in out["nested"]


def test_strip_forbidden_recurses_through_lists_of_dicts():
    """M08 (段 0 Important): list 分岐が再帰しない変異 (`return list(value)`)
    が red になる pin — dict を含む list を通し、剥がれることを見る。"""
    poisoned = [{"period": ("2020-01-01", "2020-06-01"), "ok": 1},
                {"nested": [{"now": "2020-01-01T00:00:00"}]}]
    out = _strip_forbidden({"items": poisoned})
    keys, leaves = _walk_leaves(out)
    assert _FORBIDDEN_KEYS.isdisjoint(set(keys))
    assert out["items"][0]["ok"] == 1  # 非禁止キーは残る


def test_strip_forbidden_recurses_through_tuples():
    """L21: `_strip_forbidden` は dict/list しか再帰せず tuple 要素が
    素通りする (`improve_rpc_tools.py:43-48`)。tuple 要素を含む構造を
    通し、剥がれることを確認する。"""
    poisoned = {"items": ({"now": "2020-01-01T00:00:00", "ok": 1},)}
    out = _strip_forbidden(poisoned)
    keys, leaves = _walk_leaves(out)
    assert _FORBIDDEN_KEYS.isdisjoint(set(keys))
    assert out["items"][0]["ok"] == 1


def test_ledger_result_summary_keeps_forbidden_keys_stripped_agent_return_does_not():
    """M09 (段 0 Important): `_strip_forbidden(result)` を台帳へ渡すよう
    差し替える変異が red になる pin — docstring が明示する「台帳は痩せ
    ない」契約 (`_persist_ledger_rows` が `period`/`now` を読む) を、
    台帳側に残る/agent 戻り値からは消えるの対で 1 本にまとめて見る。"""
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    handler_result = {"metrics": {"pf": 1.0}, "trial_count": 1,
                      "period": ("2020-01-01", "2020-06-01"), "now": "2020-01-01T00:00:00"}
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger, run_backtest_handler=lambda a: handler_result,
        analyze_corr_handler=lambda a: {})}
    out = tools["run_backtest"].func(name="x", pair="USDJPY")
    assert "period" not in out and "now" not in out
    ledger.freeze()
    stored = ledger.entries()[0]["result_summary"]
    assert stored["period"] == ("2020-01-01", "2020-06-01")
    assert stored["now"] == "2020-01-01T00:00:00"


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
