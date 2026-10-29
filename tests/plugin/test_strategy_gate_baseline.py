"""同名の配備中の版との比較 (`compare_mode`) の pin。

run_in_sample / run_holdout_gate / build_intent_source は差し替え、
中の再生は走らせない。`(content_hash, scope, pair)` を引き当てて成績を
返す fake が、実際の関数と同じ形の行を `record_fn` へ渡す。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.resolve import (
    ApprovedInventory, InventoryBuildResult, ResolvedIndicatorSet)
from agentic_fx.plugin.sandbox import SandboxError
from agentic_fx.plugin.strategy_gate import (
    SAME_NAME_CONTRACT_CHANGED, classify_replay_failure, compare_metrics,
    evaluate_strategy_adoption_gate)

_EMPTY = ResolvedIndicatorSet.empty(Path("/nonexistent/plugins"))
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
CAND = "c" * 64
DEPL = "d" * 64


def _m(trades=60, pf=1.5, avg_r=0.2, *, evaluable=None, win_rate=0.5,
       max_drawdown=0.01, total_pnl=100.0):
    if evaluable is None:
        evaluable = trades >= 30
    return {"trades": trades, "pf": pf, "win_rate": win_rate,
            "avg_r": avg_r, "max_drawdown": max_drawdown,
            "total_pnl": total_pnl, "evaluable": evaluable,
            "fallback_spread_used": False, "kill_switch_latches": 0}


def _settings():
    s = MagicMock()
    s.improve.gate.min_pf = 1.0
    s.improve.gate.require_positive_avg_r = True
    s.improve.gate.require_holdout_evaluable = False
    s.backtest.eval_source = "dukascopy"
    s.backtest.dataset.return_value = HistoryDataset("dukascopy", "1m")
    return s


def _meta(content_hash, *, name="myst", pairs=("USDJPY",), timeframe="1h"):
    return PluginMeta(name=name, kind="strategy", path=Path("/tmp/x"),
                      params={}, timeframe=timeframe, pairs=tuple(pairs),
                      max_bars=1000, content_hash=content_hash)


def _inventory(*metas, resolved=None):
    return InventoryBuildResult(
        inventory=ApprovedInventory(root=Path("/nonexistent/plugins"),
                                    metas=tuple(metas)),
        phase1_metas=(), resolved=resolved or {}, rejected_strategies=())


class _Source:
    def __init__(self, meta, hook=None):
        self.meta = meta
        self.cpu_sec = 1.5
        self.closed = False
        self._hook = hook

    def __call__(self, bar):
        if self._hook:
            self._hook()
        return None

    def close(self):
        self.closed = True


class Env:
    """gate の差し替え一式。`table` は (content_hash, scope, pair) -> 成績。
    `fail` は (content_hash, scope, pair) -> 投げる例外。"""

    def __init__(self, monkeypatch, *, pairs=("USDJPY",), table=None,
                 fail=None, clock=None):
        self.pairs = list(pairs)
        self.table = table if table is not None else {}
        self.fail = fail or {}
        self.rows: list[dict] = []
        self.calls: list[tuple] = []
        self.sources: list[_Source] = []
        self.sink_calls: list[tuple] = []
        self.clock = clock
        self.source_hook = None
        monkeypatch.setattr(
            "agentic_fx.plugin.strategy_gate.strategy_adapter."
            "build_intent_source", self._build)

    def _build(self, meta, **kw):
        src = _Source(meta, self.source_hook)
        self.sources.append(src)
        return src

    def _run(self, scope):
        def run(settings, *, history_conn, symbol, dataset, intent_source,
                eval_timeframe, plugin_ref, content_hash, kind, now,
                record_fn=None):
            self.calls.append((scope, symbol, content_hash, plugin_ref,
                               eval_timeframe, now, dataset))
            key = (content_hash, scope, symbol)
            if key in self.fail:
                raise self.fail[key]
            metrics = self.table.get(key, _m())
            record_fn({"scope": "holdout_gate" if scope == "holdout" else scope,
                       "pair": symbol, "plugin_ref": plugin_ref,
                       "content_hash": content_hash, "kind": kind,
                       "timeframe": eval_timeframe, "metrics": metrics,
                       "now": now})
            return dict(metrics)
        return run

    def sink(self, scope, pair, source, error):
        self.sink_calls.append((scope, pair, source, error))

    def gate(self, *, mode="shadow", inventory=None, candidate=None,
             timeframe="1h", pairs=None, **extra):
        pairs = list(self.pairs if pairs is None else pairs)
        cand = candidate or _meta(CAND, pairs=pairs, timeframe=timeframe)
        if inventory is None:
            inventory = _inventory(_meta(DEPL, pairs=self.pairs))
        kw = dict(
            meta=cand, name="myst", pairs=pairs, timeframe=timeframe,
            content_hash=CAND, now=NOW, settings=_settings(),
            resolved=_EMPTY, inventory=inventory,
            run_in_sample_fn=self._run("in_sample"),
            run_holdout_gate_fn=self._run("holdout"),
            record_fn=self.rows.append, compare_mode=mode,
            evaluation_sink=self.sink)
        kw.update(extra)
        return evaluate_strategy_adoption_gate(MagicMock(), **kw)

    def baseline_rows(self):
        return [r for r in self.rows if r.get("variant") == "baseline"]


# ---- compare_metrics の境界表 -------------------------------------------

@pytest.mark.parametrize("b,c,expected", [
    (_m(pf=1.2, avg_r=0.1), _m(pf=1.3, avg_r=0.2), "improved"),
    (_m(pf=1.2, avg_r=0.1), _m(pf=1.2, avg_r=0.2), "not_improved"),
    (_m(pf=1.2, avg_r=0.1), _m(pf=1.3, avg_r=0.1), "not_improved"),
    (_m(pf=1.2, avg_r=0.1), _m(pf=1.1, avg_r=0.3), "not_improved"),
    (_m(pf=1.2, avg_r=0.1), _m(pf=1.5, avg_r=0.05), "not_improved"),
    (_m(pf=None, avg_r=0.1), _m(pf=None, avg_r=0.2), "not_improved"),
    (_m(pf=None, avg_r=0.1), _m(pf=9.0, avg_r=0.2), "not_improved"),
    (_m(pf=1.2, avg_r=0.1), _m(pf=None, avg_r=0.2), "improved"),
    (_m(pf=1.2, avg_r=0.1), _m(pf=None, avg_r=0.1), "not_improved"),
    (_m(pf=0.0, avg_r=-0.1), _m(pf=None, avg_r=0.2), "improved"),
    (_m(pf=1.2, avg_r=None), _m(pf=1.3, avg_r=0.2), "not_improved"),
    (_m(pf=1.2, avg_r=0.1), _m(pf=1.3, avg_r=None), "not_improved"),
    (_m(trades=10, pf=1.0), _m(pf=2.0, avg_r=0.9), "unavailable"),
    (_m(pf=1.0), _m(trades=10, pf=2.0, avg_r=0.9), "unavailable"),
])
def test_compare_metrics_boundaries(b, c, expected):
    assert compare_metrics(b, c) == expected


def test_compare_metrics_pf_zero_is_not_treated_as_none():
    # pf=0.0 (全敗) の基準に pf=0.0 の候補は並ぶだけで上回らない
    assert compare_metrics(_m(pf=0.0, avg_r=-0.3),
                           _m(pf=0.0, avg_r=0.1)) == "not_improved"


def test_compare_metrics_does_not_mutate_inputs():
    b, c = _m(), _m(pf=2.0, avg_r=0.9)
    b0, c0 = dict(b), dict(c)
    compare_metrics(b, c)
    assert (b, c) == (b0, c0)


# ---- 再生失敗の対応表 ---------------------------------------------------

@pytest.mark.parametrize("code,expected", [
    ("sandbox_unavailable", "retryable"),
    ("plugin_error", "unavailable"), ("crashed", "unavailable"),
    ("protocol_error", "unavailable"), ("backtest_failed", "unavailable"),
    ("timeout", "unavailable"), ("cpu_limit", "unavailable"),
    ("something_new", "fail_closed"), ("", "fail_closed"),
])
def test_classify_replay_failure_table(code, expected):
    assert classify_replay_failure(code) == expected


# ---- 同条件の再生と行 ---------------------------------------------------

def test_improved_replays_deployed_under_same_conditions(monkeypatch):
    env = Env(monkeypatch, pairs=("USDJPY", "EURUSD"), table={
        (DEPL, "holdout", "USDJPY"): _m(pf=1.2, avg_r=0.1),
        (DEPL, "holdout", "EURUSD"): _m(pf=1.2, avg_r=0.1),
        (DEPL, "in_sample", "USDJPY"): _m(pf=1.1, avg_r=0.05),
        (DEPL, "in_sample", "EURUSD"): _m(pf=1.1, avg_r=0.05)})
    v = env.gate(mode="enforce")
    assert v.comparison["verdict"] == "improved"
    assert v.not_improved is False
    assert v.baseline_variant == "baseline"
    assert v.baseline_row == {"ref_plugin_ref": "plugins/myst",
                              "variant": "baseline",
                              "base_content_hash": DEPL}
    base_calls = [c for c in env.calls if c[2] == DEPL]
    cand_calls = [c for c in env.calls if c[2] == CAND]
    assert len(base_calls) == len(cand_calls) == 4
    # 同じ now / dataset / eval_timeframe / plugin_ref
    for b in base_calls:
        twin = next(c for c in cand_calls if c[:2] == b[:2])
        assert b[3:] == twin[3:]
    # baseline の行: pair x 段で 4 行、候補の行の後ろ
    rows = env.baseline_rows()
    assert sorted((r["scope"], r["pair"]) for r in rows) == sorted(
        (s, p) for s in ("in_sample", "holdout_gate")
        for p in ("USDJPY", "EURUSD"))
    for r in rows:
        assert r["plugin_ref"] == "plugins/myst"
        assert r["content_hash"] == DEPL
        assert r["ref_plugin_ref"] == "plugins/myst"
        assert r["ref_content_hash"] == CAND
    first_base = env.rows.index(rows[0])
    assert all(r.get("variant") != "baseline" for r in env.rows[:first_base])
    assert first_base == 4


def test_comparison_payload_schema(monkeypatch):
    env = Env(monkeypatch, table={
        (DEPL, "holdout", "USDJPY"): _m(pf=1.2, avg_r=0.1)})
    v = env.gate(mode="shadow")
    c = v.comparison
    assert list(c) == ["mode", "verdict", "reason", "stages"]
    assert c["mode"] == "shadow" and c["verdict"] == "improved"
    assert c["reason"] == "holdout_exceeds"
    assert list(c["stages"]) == ["holdout", "in_sample"]
    for stage in ("holdout", "in_sample"):
        entry = c["stages"][stage]["USDJPY"]
        assert list(entry) == ["candidate", "deployed", "verdict"]
        assert set(entry["candidate"]) == {
            "trades", "pf", "win_rate", "avg_r", "max_drawdown",
            "total_pnl", "evaluable", "fallback_spread_used"}
        assert set(entry["deployed"]) == set(entry["candidate"])
    import json
    json.dumps(c)


def test_in_sample_is_attached_but_not_decisive(monkeypatch):
    env = Env(monkeypatch, table={
        (DEPL, "holdout", "USDJPY"): _m(pf=1.2, avg_r=0.1),
        (DEPL, "in_sample", "USDJPY"): _m(pf=9.0, avg_r=5.0)})
    v = env.gate()
    assert v.comparison["verdict"] == "improved"
    assert v.comparison["stages"]["in_sample"]["USDJPY"][
        "verdict"] == "not_improved"


def test_not_improved_when_equal_holdout(monkeypatch):
    env = Env(monkeypatch)  # 候補も配備中も同じ成績
    v = env.gate(mode="enforce")
    assert v.comparison["verdict"] == "not_improved"
    assert v.comparison["reason"] == "holdout_not_exceeds"
    assert v.not_improved is True
    assert len(env.baseline_rows()) == 2  # 却下でも行は残る


def test_multi_pair_one_failing_pair_fails_whole(monkeypatch):
    better = _m(pf=1.2, avg_r=0.1)
    env = Env(monkeypatch, pairs=("USDJPY", "EURUSD", "GBPUSD"), table={
        (DEPL, "holdout", "USDJPY"): better,
        (DEPL, "holdout", "EURUSD"): _m(pf=1.5, avg_r=0.2),  # 候補と同じ
        (DEPL, "holdout", "GBPUSD"): better})
    v = env.gate()
    assert v.comparison["verdict"] == "not_improved"
    stage = v.comparison["stages"]["holdout"]
    assert [stage[p]["verdict"] for p in ("USDJPY", "EURUSD", "GBPUSD")] == [
        "improved", "not_improved", "improved"]


def test_shadow_and_enforce_produce_the_same_rows(monkeypatch):
    out = []
    for mode in ("shadow", "enforce"):
        env = Env(monkeypatch)
        v = env.gate(mode=mode)
        out.append((env.rows, v.not_improved))
    assert out[0][0] == out[1][0]
    assert out[0][1] == out[1][1] is True


# ---- 床が先、契約が最優先 -----------------------------------------------

def test_floor_failure_never_replays_deployed(monkeypatch):
    env = Env(monkeypatch, table={
        (CAND, "in_sample", "USDJPY"): _m(pf=0.5, avg_r=-0.1)})
    v = env.gate(mode="enforce")
    assert v.floor_reason == "unprofitable"
    assert v.comparison is None
    assert not [c for c in env.calls if c[2] == DEPL]
    assert env.baseline_rows() == []


def test_holdout_floor_failure_never_replays_deployed(monkeypatch):
    env = Env(monkeypatch, table={
        (CAND, "holdout", "USDJPY"): _m(pf=0.5, avg_r=-0.1)})
    v = env.gate(mode="enforce")
    assert v.floor_reason == "unprofitable"
    assert v.comparison is None
    assert not [c for c in env.calls if c[2] == DEPL]


def test_floor_rows_and_order_unchanged_by_compare_mode(monkeypatch):
    off = Env(monkeypatch, pairs=("USDJPY", "EURUSD"))
    v_off = off.gate(mode="off")
    on = Env(monkeypatch, pairs=("USDJPY", "EURUSD"))
    v_on = on.gate(mode="shadow")
    cand_rows = [r for r in on.rows if r.get("variant") != "baseline"]
    assert cand_rows == off.rows
    assert v_on.candidate_metrics == v_off.candidate_metrics
    assert v_on.holdout_metrics == v_off.holdout_metrics
    assert v_on.floor_reason == v_off.floor_reason == ""


@pytest.mark.parametrize("cand_tf,cand_pairs", [
    ("4h", ["USDJPY"]),                 # 足の変更
    ("1h", ["USDJPY", "EURUSD"]),       # 上位集合
    ("1h", ["EURUSD"]),                 # 別 pair
])
def test_contract_change_fails_closed_before_any_backtest(
        monkeypatch, cand_tf, cand_pairs):
    env = Env(monkeypatch, pairs=("USDJPY",))
    v = env.gate(mode="enforce", timeframe=cand_tf, pairs=cand_pairs)
    assert v.contract_violation == SAME_NAME_CONTRACT_CHANGED
    assert env.calls == [] and env.rows == [] and env.sources == []
    assert v.comparison is None


def test_contract_subset_of_pairs_is_changed(monkeypatch):
    env = Env(monkeypatch, pairs=("USDJPY", "EURUSD"))
    v = env.gate(mode="shadow", pairs=["USDJPY"])
    assert v.contract_violation == SAME_NAME_CONTRACT_CHANGED
    assert env.calls == []


def test_contract_pair_order_does_not_matter(monkeypatch):
    env = Env(monkeypatch, pairs=("USDJPY", "EURUSD"))
    v = env.gate(mode="shadow", pairs=["EURUSD", "USDJPY"])
    assert v.contract_violation == ""
    assert v.comparison is not None


def test_contract_wins_over_floor_failure(monkeypatch):
    env = Env(monkeypatch, pairs=("USDJPY",), table={
        (CAND, "in_sample", "USDJPY"): _m(pf=0.1, avg_r=-1.0)})
    v = env.gate(mode="enforce", timeframe="4h")
    assert v.contract_violation == SAME_NAME_CONTRACT_CHANGED
    assert v.floor_reason == ""
    assert env.rows == []


def test_compare_off_skips_contract_check(monkeypatch):
    env = Env(monkeypatch, pairs=("USDJPY",))
    v = env.gate(mode="off", timeframe="4h")
    assert v.contract_violation == ""
    assert v.comparison is None
    assert env.baseline_rows() == []


# ---- 対象外は従来と同一 -------------------------------------------------

def _verdict_fields(v):
    return (v.evaluable, v.baseline_variant, v.baseline_row,
            v.candidate_metrics, v.holdout_metrics, v.floor_reason,
            v.floor_detail, v.comparison, v.not_improved,
            v.contract_violation)


def test_new_name_is_identical_to_off(monkeypatch):
    inv = _inventory(_meta("e" * 64, name="other"))
    a = Env(monkeypatch)
    va = a.gate(mode="shadow", inventory=inv)
    b = Env(monkeypatch)
    vb = b.gate(mode="off", inventory=inv)
    assert a.rows == b.rows
    assert _verdict_fields(va) == _verdict_fields(vb)
    assert va.baseline_variant == "no_strategy"
    assert va.baseline_row == {"plugin_ref": "no_strategy:myst",
                               "content_hash": CAND, "kind": "strategy",
                               "variant": "no_strategy"}


def test_off_with_deployed_keeps_legacy_baseline_row(monkeypatch):
    env = Env(monkeypatch)
    v = env.gate(mode="off")
    assert v.baseline_row == {"ref_plugin_ref": "plugins/myst",
                              "variant": "baseline"}
    assert v.comparison is None and v.not_improved is False
    assert not [c for c in env.calls if c[2] == DEPL]


def test_indicator_kind_returns_none_regardless_of_mode(monkeypatch):
    env = Env(monkeypatch)
    assert env.gate(mode="enforce", kind="indicator") is None
    assert env.calls == []


def test_compare_mode_requires_inventory(monkeypatch):
    env = Env(monkeypatch)
    with pytest.raises(ValueError):
        evaluate_strategy_adoption_gate(
            MagicMock(), meta=_meta(CAND), name="myst", pairs=["USDJPY"],
            timeframe="1h", content_hash=CAND, now=NOW, settings=_settings(),
            resolved=_EMPTY, run_in_sample_fn=env._run("in_sample"),
            run_holdout_gate_fn=env._run("holdout"), compare_mode="shadow")


def test_unknown_compare_mode_rejected(monkeypatch):
    env = Env(monkeypatch)
    with pytest.raises(ValueError):
        env.gate(mode="strict")


# ---- 再生失敗・切り戻し -------------------------------------------------

@pytest.mark.parametrize("code", [
    "plugin_error", "crashed", "protocol_error", "backtest_failed",
    "timeout", "cpu_limit"])
def test_replay_failure_becomes_unavailable_with_no_baseline_rows(
        monkeypatch, code):
    env = Env(monkeypatch, pairs=("USDJPY", "EURUSD"), fail={
        (DEPL, "holdout", "EURUSD"): SandboxError("x", code=code)})
    v = env.gate(mode="enforce")
    assert v.comparison["verdict"] == "unavailable"
    assert v.comparison["reason"] == "baseline_unhealthy"
    assert v.comparison["stages"] == {"holdout": {}, "in_sample": {}}
    assert v.baseline_failure_code == code
    assert v.not_improved is False
    # 先に回った USDJPY の baseline 行も出ていない。候補は完全
    assert env.baseline_rows() == []
    cand = [r for r in env.rows if r["content_hash"] == CAND]
    assert len(cand) == 4 and len(env.rows) == 4
    assert v.baseline_row["base_content_hash"] == DEPL


@pytest.mark.parametrize("code", ["sandbox_unavailable", "brand_new_code"])
def test_retryable_and_unknown_codes_propagate_without_rows(monkeypatch, code):
    env = Env(monkeypatch, pairs=("USDJPY", "EURUSD"), fail={
        (DEPL, "holdout", "EURUSD"): SandboxError("x", code=code)})
    with pytest.raises(SandboxError) as ei:
        env.gate(mode="enforce")
    assert ei.value.code == code
    assert env.baseline_rows() == []
    assert len([r for r in env.rows if r["content_hash"] == CAND]) == 4


def test_non_sandbox_error_during_replay_propagates_without_rows(monkeypatch):
    env = Env(monkeypatch, fail={(DEPL, "in_sample", "USDJPY"): ValueError("x")})
    with pytest.raises(ValueError):
        env.gate(mode="enforce")
    assert env.baseline_rows() == []


def test_failed_replay_still_closes_sources_and_reports_sink(monkeypatch):
    err = SandboxError("x", code="crashed")
    env = Env(monkeypatch, fail={(DEPL, "holdout", "USDJPY"): err})
    v = env.gate()
    assert all(s.closed for s in env.sources)
    scopes = [c[0] for c in env.sink_calls]
    assert scopes == ["in_sample", "holdout", "baseline_in_sample",
                      "baseline_holdout"]
    assert env.sink_calls[-1][3] is err
    assert [s[0] for s in v.cpu_samples] == scopes


def test_baseline_holdout_not_evaluable_is_unavailable_but_keeps_rows(
        monkeypatch):
    env = Env(monkeypatch, table={
        (DEPL, "holdout", "USDJPY"): _m(trades=12, pf=1.0, avg_r=0.0)})
    v = env.gate(mode="enforce")
    assert v.comparison["verdict"] == "unavailable"
    assert v.comparison["reason"] == "baseline_not_evaluable"
    assert v.not_improved is False
    assert v.baseline_failure_code == ""
    assert len(env.baseline_rows()) == 2  # 完全な対が残る


def test_candidate_holdout_not_evaluable_is_unavailable(monkeypatch):
    env = Env(monkeypatch, table={
        (CAND, "holdout", "USDJPY"): _m(trades=12, pf=2.0, avg_r=0.5)})
    v = env.gate()
    assert v.comparison["verdict"] == "unavailable"
    assert v.comparison["reason"] == "candidate_not_evaluable"


def test_one_non_evaluable_pair_makes_whole_unavailable(monkeypatch):
    env = Env(monkeypatch, pairs=("USDJPY", "EURUSD"), table={
        (DEPL, "holdout", "USDJPY"): _m(pf=1.0, avg_r=0.0),
        (DEPL, "holdout", "EURUSD"): _m(trades=5, pf=1.0, avg_r=0.0)})
    v = env.gate()
    assert v.comparison["verdict"] == "unavailable"


def test_missing_resolved_with_indicators_is_unavailable(monkeypatch):
    from dataclasses import replace
    deployed = replace(_meta(DEPL), indicators=(object(),))
    env = Env(monkeypatch)
    v = env.gate(inventory=_inventory(deployed))
    assert v.comparison["verdict"] == "unavailable"
    assert v.comparison["reason"] == "baseline_unhealthy"
    assert env.baseline_rows() == []


# ---- 壁時計 -------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _advancing_env(monkeypatch, clock, *, cand_cost=0.0, base_cost=0.0,
                   pairs=("USDJPY",)):
    env = Env(monkeypatch, pairs=pairs)
    inner = env._run

    def run_factory(scope):
        run = inner(scope)

        def wrapped(*a, **kw):
            clock.t += base_cost if kw["content_hash"] == DEPL else cand_cost
            return run(*a, **kw)
        return wrapped
    env._run = run_factory
    return env


def test_timer_starts_after_floor_candidate_floor_never_times_out(monkeypatch):
    clock = FakeClock()
    env = _advancing_env(monkeypatch, clock, cand_cost=10_000.0,
                         base_cost=1.0)
    v = env.gate(mode="enforce", clock=clock,
                 baseline_replay_wall_clock_max_sec=300.0)
    assert v.comparison["reason"] in ("holdout_exceeds",
                                      "holdout_not_exceeds")
    assert len(env.baseline_rows()) == 2


def test_baseline_replay_over_limit_is_gate_timeout(monkeypatch):
    clock = FakeClock()
    env = _advancing_env(monkeypatch, clock, base_cost=200.0)
    v = env.gate(mode="enforce", clock=clock,
                 baseline_replay_wall_clock_max_sec=300.0)
    # in_sample 200s + holdout 200s で合計 400s > 300s
    assert v.comparison["verdict"] == "unavailable"
    assert v.comparison["reason"] == "gate_timeout"
    assert v.baseline_failure_code == "gate_timeout"
    assert env.baseline_rows() == []
    assert len([r for r in env.rows if r["content_hash"] == CAND]) == 2


def test_replay_exactly_within_limit_is_kept(monkeypatch):
    clock = FakeClock()
    env = _advancing_env(monkeypatch, clock, base_cost=100.0)
    v = env.gate(mode="enforce", clock=clock,
                 baseline_replay_wall_clock_max_sec=300.0)
    assert v.comparison["reason"] != "gate_timeout"
    assert len(env.baseline_rows()) == 2


def test_deadline_cancels_replay_midway_through_source_calls(monkeypatch):
    clock = FakeClock()
    env = Env(monkeypatch)
    underlying_calls = []
    env.source_hook = lambda: underlying_calls.append(1)
    base_run = env._run

    def run_factory(scope):
        run = base_run(scope)

        def wrapped(settings, **kw):
            if kw["content_hash"] == DEPL and scope == "in_sample":
                src = kw["intent_source"]
                for _ in range(5):
                    clock.t += 100.0
                    src(object())          # 3 回目で上限に達する
            return run(settings, **kw)
        return wrapped
    env._run = run_factory
    v = env.gate(mode="enforce", clock=clock,
                 baseline_replay_wall_clock_max_sec=300.0)
    assert v.comparison["reason"] == "gate_timeout"
    assert env.baseline_rows() == []
    assert all(s.closed for s in env.sources)
    # 上限に達した 3 回目以降は中の source を呼ばずに取り消す
    assert len(underlying_calls) == 2


def test_wrapped_source_delegates_attributes(monkeypatch):
    clock = FakeClock()
    env = Env(monkeypatch)
    seen = {}
    base_run = env._run

    def run_factory(scope):
        run = base_run(scope)

        def wrapped(settings, **kw):
            if kw["content_hash"] == DEPL:
                seen["cpu"] = kw["intent_source"].cpu_sec
            return run(settings, **kw)
        return wrapped
    env._run = run_factory
    env.gate(clock=clock)
    assert seen["cpu"] == 1.5


@pytest.mark.parametrize("bad", [0, -1, 0.0])
def test_nonpositive_wall_clock_rejected(monkeypatch, bad):
    env = Env(monkeypatch)
    with pytest.raises(ValueError):
        env.gate(baseline_replay_wall_clock_max_sec=bad)


# ---- 決定論 -------------------------------------------------------------

def test_same_inputs_twice_give_identical_rows_and_comparison(monkeypatch):
    outs = []
    for _ in range(2):
        env = Env(monkeypatch, pairs=("USDJPY", "EURUSD"), table={
            (DEPL, "holdout", "USDJPY"): _m(pf=1.2, avg_r=0.1)})
        v = env.gate(mode="enforce")
        outs.append((env.rows, v.comparison, v.baseline_row,
                     v.cpu_samples))
    assert outs[0] == outs[1]


def test_decide_comparison_without_pairs_is_unavailable():
    from agentic_fx.plugin.strategy_gate import _decide_comparison
    c = _decide_comparison(mode="shadow", pairs=[], in_sample_c={},
                           in_sample_d={}, holdout_c={}, holdout_d={})
    assert c["verdict"] == "unavailable" and c["reason"] == "no_pairs"
