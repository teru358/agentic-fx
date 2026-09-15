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
from agentic_fx.config import ImproveToolBudgetSettings
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.tools.improve_rpc_tools import (
    RpcOutcome, _FORBIDDEN_KEYS, _is_successful_backtest, _strip_forbidden,
    build_improve_rpc_tooldefs, build_rpc_handlers,
)
from agentic_fx.tools.mission_counters import MissionToolCounters

from tests.backtest.factories import _conn
from tests.backtest.test_analysis import (
    BEFORE_BOUNDARY, NOW, _seed_two_series, _settings_watch_eurusd,
)

_ISO_DATETIME_LEAF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _write_loader_candidate(base, name, config):
    candidate = base / name
    candidate.mkdir()
    (candidate / "plugin.py").write_text(
        ("def evaluate(df, indicators, signals, params): return {'action': 'hold'}\n"
         if config and "kind: strategy" in config
         else "def compute(df, params): return {}\n"))
    (candidate / "test_plugin.py").write_text("def test_placeholder(): pass\n")
    if config is not None:
        (candidate / "config.yaml").write_text(config)
    return candidate


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


def test_parent_rpc_wrapper_returns_public_private_without_recording(tmp_path):
    class HandlerResult(dict):
        pass

    result = HandlerResult(
        metrics={"pf": 1.2}, period=("secret-start", "secret-end"),
        trial_count=3)
    result.save_kwargs = {
        "metrics": {"pf": 1.2}, "period": ("secret-start", "secret-end"),
        "trial_count": 3, "archive_tmp": "/tmp/snapshot",
    }
    handlers = build_rpc_handlers(
        {"run_backtest": lambda args: result,
         "analyze_corr": lambda args: {"trial_count": 1}},
        None,
    )

    outcome = handlers["run_backtest"]({"name": "x", "pair": "USDJPY"})

    assert isinstance(outcome, RpcOutcome)
    assert outcome.public == {"metrics": {"pf": 1.2}, "trial_count": 3}
    assert outcome.private == result.save_kwargs


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


def test_run_backtest_error_does_not_count_as_success_and_budget_stops_seventh_call():
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    counters = MissionToolCounters()
    calls = []
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger,
        run_backtest_handler=lambda args: calls.append(args) or {"error": "no history"},
        analyze_corr_handler=lambda args: {}, counters=counters,
        budget=ImproveToolBudgetSettings(max_backtests_per_candidate=6))}

    for _ in range(6):
        assert tools["run_backtest"].func("a", "USDJPY")["error"] == "no history"
    rejected = tools["run_backtest"].func("a", "USDJPY")
    assert rejected["error"] == "budget exhausted"
    assert rejected["budget"] == "max_backtests_per_candidate"
    assert len(calls) == 6
    assert counters.successful_backtests["a"] == 0
    ledger.freeze()
    assert len(ledger.entries()) == 6


def test_run_backtest_success_is_recorded_in_shared_counters():
    counters = MissionToolCounters()
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0}),
        run_backtest_handler=lambda args: {"metrics": {"trades": 1, "evaluable": False}},
        analyze_corr_handler=lambda args: {}, counters=counters,
        budget=ImproveToolBudgetSettings())}
    tools["run_backtest"].func("a", "USDJPY")
    assert counters.successful_backtests["a"] == 1


def test_run_backtest_guides_indicator_without_calling_handler(tmp_path):
    _write_loader_candidate(tmp_path, "rsi_v2", "kind: indicator\n")
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
    _write_loader_candidate(
        tmp_path, "sma_cross_v2",
        "kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\n"
        "exit_mode: levels\nmax_bars: 200\n")
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
    ("missing", None, "loader_rejected: missing required files", None),
    ("bad_yaml", "kind: [\n", "loader_rejected: invalid YAML", None),
    ("missing_kind", "pairs: []\n", "loader_rejected: invalid kind: None", None),
    ("non_str", "kind: 3\n", "loader_rejected: invalid kind", None),
    ("indicator", "kind: indicator\n",
     "run_backtest is only for kind=strategy candidates", "indicator"),
])
def test_run_backtest_fails_closed_for_invalid_candidate_config(
        tmp_path, name, config, expected_error, expected_kind):
    _write_loader_candidate(tmp_path, name, config)
    calls = []
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ledger, staging_dir=tmp_path,
        run_backtest_handler=lambda args: calls.append(args) or {"ok": True},
        analyze_corr_handler=lambda args: {})}

    out = tools["run_backtest"].func(name=name, pair="USDJPY")
    assert out["error"].startswith(expected_error)
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
    assert out["error"] == "invalid candidate name"
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


# --- codex 1 周目 (2026-09-07、tmp/review-20260907-st/codex-round1.md) の pin ---

@pytest.mark.parametrize("reply", [{}, {"scope": "in_sample"}, {"error": "x", "metrics": {}}])
def test_run_backtest_counts_success_only_when_metrics_present(reply):
    """Important 1: 「成功 backtest」= error 無し **かつ** metrics あり。空 dict や
    metrics 欠落は Tier B を解除しない。"""
    counters = MissionToolCounters()
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0}),
        run_backtest_handler=lambda args: reply,
        analyze_corr_handler=lambda args: {}, counters=counters,
        budget=ImproveToolBudgetSettings())}
    tools["run_backtest"].func("a", "USDJPY")
    assert counters.successful_backtests["a"] == 0


def test_run_backtest_validation_errors_do_not_consume_candidate_budget(tmp_path):
    """Important 2: 名前不正 / loader 拒否 / indicator の応答は候補あたり予算を
    消費しない。予算が減るのは handler に到達した呼び出しだけ。"""
    _write_loader_candidate(tmp_path, "rsi_v2", "kind: indicator\n")
    counters = MissionToolCounters()
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0}),
        staging_dir=tmp_path,
        run_backtest_handler=lambda args: {"metrics": {}},
        analyze_corr_handler=lambda args: {}, counters=counters,
        budget=ImproveToolBudgetSettings(max_backtests_per_candidate=1))}
    assert "error" in tools["run_backtest"].func(name="Bad-Name", pair="USDJPY")
    assert "error" in tools["run_backtest"].func(name="missing", pair="USDJPY")
    assert tools["run_backtest"].func(name="rsi_v2", pair="USDJPY")["error"] == \
        "run_backtest is only for kind=strategy candidates"
    assert counters.backtest_calls["Bad-Name"] == 0
    assert counters.backtest_calls["missing"] == 0
    assert counters.backtest_calls["rsi_v2"] == 0


@pytest.mark.parametrize("kwargs", [
    {"counters": MissionToolCounters()},
    {"budget": ImproveToolBudgetSettings()},
])
def test_rpc_builder_rejects_half_wiring(kwargs):
    """codex 2 周目 (2026-09-07): counters と budget は対 (staging 側と同じ規律)。"""
    with pytest.raises(ValueError):
        build_improve_rpc_tooldefs(
            ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0}),
            run_backtest_handler=lambda args: {}, analyze_corr_handler=lambda args: {},
            **kwargs)


def test_run_backtest_reports_remaining_budget_and_records_terminal_refusal():
    budget = ImproveToolBudgetSettings(
        max_backtests_per_candidate=1, max_refusal_streak=1)
    counters = MissionToolCounters(budget=budget)
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ImproveRpcLedger(
            rpc_timeout_sec_by_kind={"run_backtest": 600.0}),
        run_backtest_handler=lambda args: {"metrics": {"pf": 1.1}},
        analyze_corr_handler=lambda args: {}, counters=counters,
        budget=budget)}
    first = tools["run_backtest"].func("a", "USDJPY")
    assert first["remaining_budget"] == {"backtests_for_candidate": 0}
    rejected = tools["run_backtest"].func("a", "USDJPY")
    assert "remaining_budget" not in rejected
    assert counters.abort_pending


def test_successful_backtest_resets_terminal_refusal_streak():
    """段 0 pin A18 (2026-09-08): 成功 backtest (metrics あり) は tool 経由で
    `record_progress(name, "backtest_ok")` を呼び、terminal streak を 0 に戻す。"""
    budget = ImproveToolBudgetSettings()
    counters = MissionToolCounters(budget=budget)
    counters.record_terminal_refusal(); counters.record_terminal_refusal()
    assert counters.terminal_refusal_streak == 2
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0}),
        run_backtest_handler=lambda args: {"metrics": {"trades": 1, "evaluable": False}},
        analyze_corr_handler=lambda args: {}, counters=counters, budget=budget)}
    tools["run_backtest"].func("a", "USDJPY")
    assert counters.terminal_refusal_streak == 0



# --- run7 是正 [tier-b-release-requires-evaluable-backtest] (2026-09-09) ---

@pytest.mark.parametrize("metrics", [
    {"trades": 0, "evaluable": False},   # 空振り backtest (run7 #63 の実形)
    {"evaluable": True},                  # trades 欠落
    {"trades": None, "evaluable": None},
    {"trades": "3"},                      # 型違い
    {"trades": True},                     # bool は int の subclass — 数えない
])
def test_run_backtest_does_not_count_unevaluable_backtest_as_success(metrics):
    """run7 欠陥 A: trades=0 の空振り backtest 1 本で Tier B が恒久解除され、モデルが
    self-test 修正ループへ復帰した。成功 = metrics.trades が 1 以上 (int) のみ。
    `evaluable` (gate 用、trades ≥ 30) は見ない — codex 是正束レビュー Important。"""
    counters = MissionToolCounters(budget=ImproveToolBudgetSettings())
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0}),
        run_backtest_handler=lambda args: {"metrics": metrics},
        analyze_corr_handler=lambda args: {}, counters=counters,
        budget=ImproveToolBudgetSettings())}
    tools["run_backtest"].func("a", "USDJPY")
    assert counters.successful_backtests["a"] == 0
    assert counters.backtest_calls["a"] == 1      # 予算は消費する


@pytest.mark.parametrize("trades", [1, 29, 30])
def test_run_backtest_counts_any_traded_backtest_as_success(trades):
    """境界: trades=1 (低頻度、evaluable=False) でも成功。gate の 30 本閾値とは独立。"""
    counters = MissionToolCounters(budget=ImproveToolBudgetSettings())
    counters.record_terminal_refusal()
    tools = {t.name: t for t in build_improve_rpc_tooldefs(
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0}),
        run_backtest_handler=lambda args: {"metrics": {"trades": trades,
                                                      "evaluable": trades >= 30}},
        analyze_corr_handler=lambda args: {}, counters=counters,
        budget=ImproveToolBudgetSettings())}
    tools["run_backtest"].func("a", "USDJPY")
    assert counters.successful_backtests["a"] == 1
    assert counters.terminal_refusal_streak == 0


# --- ローカルレビュー pin Y1 (2026-09-09、tmp/review-20260909-r7/verified-local.md L1) ---
@pytest.mark.parametrize("result", [
    None, "backtest_failed", ["metrics"],
    {"error": "backtest_failed", "metrics": {"trades": 5}},
    {"metrics": None},
    {"metrics": "trades=5"},
])
def test_is_successful_backtest_rejects_malformed_replies(result):
    """ローカル 1 周目 L1 (muse/ornith/qwen): docstring 契約の前段 (「error が
    無く」「metrics が dict」「result が dict」) は 1 つも変異で落ちなかった。
    tool 経由では踏めない — 後続 (`result.get("trial_count", 1)`、
    `_strip_forbidden`) が dict を前提にしているため、関数を直接呼んで pin する。"""
    assert _is_successful_backtest(result) is False


# --- [indicator-consumption-wiring] T5a Step 5-3: release_backtest (F4) ---

from tests.fixtures.wiring_envs import (
    rpc_tooldefs as _rpc_tooldefs,
    rpc_tools as _rpc_tools,
)
from tests.tools.test_mission_counters import _budget


def test_unresolved_backtest_releases_the_reservation(tmp_path):
    """F4 (予約・解放の部分): `{"started": false, ...}` で予約を戻す。

    **opus r1 I12 是正**: 旧案は `before_calls = dict(counters.backtest_calls)`
    → `assert dict(counters.backtest_calls) == before_calls` と書いていたが、
    `backtest_calls` は `defaultdict(int)` なので呼び出し前は `{}`、
    `reserve_backtest("cand", …)` で `{"cand": 1}`、`release_backtest("cand")`
    で `{"cand": 0}` になる。`{} != {"cand": 0}` なので **予約解放が正しく
    動いていてもこの assert は落ちる**し、逆変異 (M8: `release_backtest` を
    no-op) でも同じ理由で落ちる = 判別力ゼロだった。**キー単位の直接 assert**
    にする (段 0 の M8 の観測点も同じものに差し替えること —
    [[mutation-testing]] の「pin の観測点は probe で決める」)。

    **`_strip_forbidden` は denylist** (`{k: … for k, v in value.items()
    if k not in _FORBIDDEN_KEYS}`、着手時に
    `rg -n 'def _strip_forbidden' -A 12 src/agentic_fx/tools/improve_rpc_tools.py`
    で再確認する) なので、新規キー `started` / `alias` / `reason` /
    `available` はそのまま通る (opus r1 I11)。`_FORBIDDEN_KEYS` には
    `start` はあるが `started` は**別キー**で完全一致では当たらない。
    **src 側の変更は不要** — 下の 4 キーの pin がそれを固定する。
    """
    counters = MissionToolCounters(budget=_budget())
    handler_result = {"started": False, "error": "indicator_unresolved",
                      "alias": "rsi", "reason": "not_found",
                      "available": ["sma", "adx"]}
    tools = _rpc_tools(tmp_path, counters=counters,
                       run_backtest_handler=lambda args: handler_result)

    out = tools["run_backtest"](name="cand", pair="USDJPY")

    # `_strip_forbidden` (denylist) を通っても 4 キーが残る (opus r1 I11)
    assert out["started"] is False
    assert out["error"] == "indicator_unresolved"
    assert out["alias"] == "rsi"
    assert out["reason"] == "not_found"
    assert out["available"] == ["sma", "adx"]
    # 予約 → 解放で 0 に戻る (opus r1 I12: defaultdict 比較にしない)
    assert counters.backtest_calls["cand"] == 0
    assert counters.successful_backtests["cand"] == 0


def test_unresolved_backtest_counts_errors_and_streak_via_the_registry(tmp_path):
    """F4 (counters の残りのフィールド、**opus r1 I5 是正**)。

    設計書 §5 は「未解決 `run_backtest` は予算枠を消費しないが `errors` /
    refusal streak に計上」と書いているが、`errors` / `recoverable_refusal_streak`
    を増やすのは **`ToolRegistry` の `_notify_result` → `counters.record_tool_result`**
    だけ (`registry.py` の `if isinstance(result, dict) and "error" in result:`
    分岐、配線は `mission_registry.py` の `on_result=counters.record_tool_result`。
    着手時に `rg -n 'on_result' src/agentic_fx/tools/registry.py src/agentic_fx/tools/mission_registry.py`
    で再取得)。tooldef を**直接呼ぶ** `_rpc_tools` 経路は registry を通らない
    ので、上のテストだけでは受入が空振りする。**registry 経由で 1 本足す**。

    前提 (1 行で固定): `started:false` 応答には `"error"` キーが含まれる
    ので registry は `ok=False` と判定する。
    """
    from agentic_fx.tools.registry import ToolRegistry
    counters = MissionToolCounters(budget=_budget())
    defs = _rpc_tooldefs(tmp_path, counters=counters,
                         run_backtest_handler=lambda args: {
                             "started": False, "error": "indicator_unresolved",
                             "alias": "rsi", "reason": "not_found",
                             "available": ["sma", "adx"]})
    registry = ToolRegistry(on_execute=counters.record_call,
                            on_result=counters.record_tool_result)
    registry.register_all(defs)
    before_errors = counters.errors
    before_total = counters.total_calls        # codex plan r1 I8

    registry.execute("run_backtest", {"name": "cand", "pair": "USDJPY"},
                     allowed=registry.names())

    assert counters.errors == before_errors + 1
    assert counters.total_calls == before_total + 1
    assert counters.recoverable_refusal_streak[
        ("run_backtest", "tool_error:indicator_unresolved")] == 1
    assert counters.backtest_calls["cand"] == 0      # 予約は戻っている


def test_response_without_started_key_keeps_the_reservation(tmp_path):
    """F4: `started` キーが無い応答 (旧形式・RPC 失敗) では解放しない
    (fail closed — 予算は消費されたまま)。"""
    counters = MissionToolCounters(budget=_budget())
    tools = _rpc_tools(tmp_path, counters=counters,
                       run_backtest_handler=lambda args: {"error": "backtest_failed"})
    tools["run_backtest"](name="cand", pair="USDJPY")
    assert counters.backtest_calls["cand"] == 1


def test_started_true_keeps_the_reservation(tmp_path):
    counters = MissionToolCounters(budget=_budget())
    tools = _rpc_tools(tmp_path, counters=counters,
                       run_backtest_handler=lambda args: {
                           "started": True, "metrics": {"trades": 5}})
    tools["run_backtest"](name="cand", pair="USDJPY")
    assert counters.backtest_calls["cand"] == 1
    assert counters.successful_backtests["cand"] == 1
