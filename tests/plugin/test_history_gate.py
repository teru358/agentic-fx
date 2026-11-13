"""共有 gate の履歴検査: 位置・回数・既定拒否・証跡の運搬。

前半は本物の検査 (`history_health`) を通し、後半は共通 fixture の差し替え
(`history_guard_fake`) で呼出しの回数と引数を見る。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_fx.backtest import history_evidence as ev
from agentic_fx.backtest import history_health as hh
from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.config import load_settings
from agentic_fx.core.market_hours import is_market_open
from agentic_fx.plugin import approval, strategy_gate, switch
from agentic_fx.plugin.resolve import ResolvedIndicatorSet
from agentic_fx.plugin.strategy_gate import evaluate_strategy_adoption_gate
from tests.backtest.factories import _conn
from tests.plugin.test_strategy_gate import _meta, _metrics

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 6, 2, 20, tzinfo=UTC)
W = timedelta(minutes=5)
EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
EMPTY = ResolvedIndicatorSet.empty(Path("/nonexistent/plugins"))


def _settings(**backtest):
    settings = load_settings(EXAMPLE)
    settings.backtest.eval_source = "mt5"
    settings.backtest.base_interval = "5m"
    settings.backtest.holdout_months = 1
    for key, value in backtest.items():
        setattr(settings.backtest, key, value)
    return settings


def _fill(conn, symbol="USDJPY", *, until=datetime(2026, 11, 10, 6, 0, tzinfo=UTC)):
    t, rows = datetime(2026, 10, 1, tzinfo=UTC), []
    while t < until:
        if is_market_open(t):
            rows.append((symbol, "5m", t.isoformat(), 148.0, 148.2, 147.9, 148.1, 1.0, "mt5"))
        t += W
    conn.executemany(
        "INSERT OR IGNORE INTO ohlcv_history (symbol, interval, bar_time, open, high, low, "
        "close, volume, source, spread) VALUES (?,?,?,?,?,?,?,?,?,NULL)", rows)
    conn.commit()


@pytest.fixture
def runs(monkeypatch):
    """In-sample と holdout の実行回数。本物の backtest は回さない。"""
    calls = {"in_sample": [], "holdout": []}
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
        lambda *a, **kw: calls["in_sample"].append(kw["symbol"]) or _metrics(trades=40, pf=1.5))
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.run_holdout_gate",
        lambda *a, **kw: calls["holdout"].append(kw["symbol"]) or _metrics(trades=40, pf=1.5))
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.strategy_adapter.build_intent_source",
        lambda meta, **kw: MagicMock(close=lambda: None, cpu_sec=0.0))
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.holdout.require_reference_history",
        lambda *a, **kw: None)
    return calls


def _gate(conn, settings, *, pairs=("USDJPY",), floor_mode="enforce", **kw):
    return evaluate_strategy_adoption_gate(
        conn, meta=_meta(pairs=pairs), name="myst", pairs=list(pairs), timeframe="1h",
        resolved=EMPTY, content_hash="h1", now=NOW, settings=settings,
        floor_mode=floor_mode, **kw)


# ---- 本物の検査 ---------------------------------------------------------------

@pytest.mark.real_history_guard
def test_a_healthy_history_passes_and_the_evidence_reaches_the_verdict(tmp_path, runs):
    conn = _conn(tmp_path)
    _fill(conn)
    verdict = _gate(conn, _settings(), no_bars_path=tmp_path / "none.yaml")
    assert verdict.evaluable and verdict.history_guard is not None
    assert runs["holdout"] == ["USDJPY"]
    check = ev.verify_evidence(verdict.history_guard, eval_source="mt5", base_interval="5m",
                               pairs=["USDJPY"], no_bars_path=tmp_path / "none.yaml")
    assert check.ok
    assert verdict.history_guard["checked_at"] == NOW.isoformat()


@pytest.mark.real_history_guard
@pytest.mark.parametrize("until,kind", [
    (datetime(2026, 11, 5, tzinfo=UTC), "history_stale"),
])
def test_a_stale_history_stops_before_any_holdout_and_files_nothing(tmp_path, runs, until, kind):
    conn = _conn(tmp_path)
    _fill(conn, until=until)
    rows = []
    verdict = _gate(conn, _settings(), no_bars_path=tmp_path / "none.yaml", record_fn=rows.append)
    assert verdict.evaluable is False and verdict.unevaluable_kind == kind
    assert verdict.observation_reason == kind
    assert verdict.holdout_metrics is None and verdict.history_guard is None
    assert runs["in_sample"] == ["USDJPY"] and runs["holdout"] == []
    assert conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 0


@pytest.mark.real_history_guard
def test_one_unhealthy_pair_stops_the_candidate_even_if_the_other_is_fine(tmp_path, runs):
    conn = _conn(tmp_path)
    _fill(conn, "USDJPY")
    _fill(conn, "EURUSD", until=datetime(2026, 11, 3, tzinfo=UTC))
    verdict = _gate(conn, _settings(), pairs=("USDJPY", "EURUSD"),
                    no_bars_path=tmp_path / "none.yaml")
    assert verdict.unevaluable_kind == "history_stale"
    assert runs["holdout"] == []


@pytest.mark.real_history_guard
def test_a_hole_in_the_window_is_a_gap(tmp_path, runs):
    conn = _conn(tmp_path)
    _fill(conn)
    conn.execute("DELETE FROM ohlcv_history WHERE bar_time >= '2026-11-04T00:00:00+00:00' "
                 "AND bar_time < '2026-11-04T06:00:00+00:00'")
    conn.commit()
    verdict = _gate(conn, _settings(), no_bars_path=tmp_path / "none.yaml")
    assert verdict.unevaluable_kind == "history_gap" and runs["holdout"] == []


@pytest.mark.real_history_guard
def test_an_unreadable_no_bars_list_stops_the_gate_as_unavailable(tmp_path, runs):
    conn = _conn(tmp_path)
    _fill(conn)
    bad = tmp_path / "bad.yaml"
    bad.write_text("entries: [", encoding="utf-8")
    verdict = _gate(conn, _settings(), no_bars_path=bad)
    assert verdict.unevaluable_kind == "history_check_unavailable" and runs["holdout"] == []


@pytest.mark.real_history_guard
def test_a_same_name_update_carries_the_evidence_too(tmp_path, runs):
    from agentic_fx.store import approvals as approvals_store
    conn = _conn(tmp_path)
    _fill(conn)
    approvals_store.create(conn, kind="plugin", payload={
        "name": "myst", "kind": "strategy", "content_hash": "base"}, now=NOW, commit=False)
    conn.execute("UPDATE approval_requests SET status='approved'")
    conn.commit()
    verdict = _gate(conn, _settings(), no_bars_path=tmp_path / "none.yaml")
    assert verdict.baseline_variant == "baseline" and verdict.history_guard is not None


# ---- 差し替えた検査: 位置・回数・引数 -----------------------------------------------

def test_the_check_runs_once_after_the_in_sample_floor_with_the_gates_own_clock(
        tmp_path, runs, history_guard_fake):
    conn = _conn(tmp_path)
    verdict = _gate(conn, _settings(), pairs=("USDJPY", "EURUSD"),
                    no_bars_path=tmp_path / "x.yaml")
    assert verdict.evaluable and len(history_guard_fake.calls) == 1
    call = history_guard_fake.calls[0]
    assert call["pairs"] == ["USDJPY", "EURUSD"] and call["now"] == NOW
    assert call["holdout_months"] == 1 and call["grace"] == timedelta(seconds=30)
    assert call["no_bars_path"] == tmp_path / "x.yaml"
    assert runs["in_sample"] == ["USDJPY", "EURUSD"] and runs["holdout"] == ["USDJPY", "EURUSD"]


def test_the_check_runs_once_with_a_baseline_replay_too(tmp_path, runs, history_guard_fake):
    from agentic_fx.store import approvals as approvals_store
    conn = _conn(tmp_path)
    approvals_store.create(conn, kind="plugin", payload={
        "name": "myst", "kind": "strategy", "content_hash": "base"}, now=NOW, commit=False)
    conn.execute("UPDATE approval_requests SET status='approved'")
    conn.commit()
    _gate(conn, _settings())
    assert len(history_guard_fake.calls) == 1


def test_too_few_trades_never_reach_the_check(tmp_path, monkeypatch, runs, history_guard_fake):
    monkeypatch.setattr("agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
                        lambda *a, **kw: _metrics(trades=3, pf=1.5))
    verdict = _gate(_conn(tmp_path), _settings())
    assert verdict.unevaluable_kind == "insufficient_trades" and history_guard_fake.calls == []


def test_an_enforced_floor_failure_never_reaches_the_check(
        tmp_path, monkeypatch, runs, history_guard_fake):
    monkeypatch.setattr("agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
                        lambda *a, **kw: _metrics(trades=40, pf=0.5))
    verdict = _gate(_conn(tmp_path), _settings())
    assert verdict.floor_reason and history_guard_fake.calls == []


def test_a_warned_floor_failure_still_goes_through_the_check_and_cannot_skip_it(
        tmp_path, monkeypatch, runs, history_guard_fake):
    monkeypatch.setattr("agentic_fx.plugin.strategy_gate.holdout.run_in_sample",
                        lambda *a, **kw: _metrics(trades=40, pf=0.5))
    verdict = _gate(_conn(tmp_path), _settings(), floor_mode="warn")
    assert verdict.evaluable and verdict.floor_reason and len(history_guard_fake.calls) == 1
    history_guard_fake.verdict = hh.HISTORY_GAP
    runs["holdout"].clear()
    stopped = _gate(_conn(tmp_path), _settings(), floor_mode="warn")
    assert stopped.evaluable is False and stopped.unevaluable_kind == "history_gap"
    assert runs["holdout"] == []


@pytest.mark.parametrize("verdict", [hh.HISTORY_STALE, hh.HISTORY_GAP, hh.HISTORY_UNAVAILABLE])
def test_each_history_verdict_maps_to_its_own_fixed_kind(tmp_path, runs, history_guard_fake,
                                                         verdict):
    history_guard_fake.verdict = verdict
    result = _gate(_conn(tmp_path), _settings())
    assert (result.evaluable, result.unevaluable_kind, result.observation_reason) == (
        False, verdict, verdict)
    assert result.baseline_variant == "no_strategy" and result.candidate_metrics is None


def test_a_check_that_raises_or_answers_unknown_stops_the_gate(tmp_path, runs,
                                                               history_guard_fake):
    history_guard_fake.error = RuntimeError("boom")
    assert _gate(_conn(tmp_path), _settings()).unevaluable_kind == "history_check_unavailable"
    history_guard_fake.error = None
    history_guard_fake.verdict = "something_new"
    assert _gate(_conn(tmp_path), _settings()).unevaluable_kind == "history_check_unavailable"
    assert runs["holdout"] == []


def test_the_explicit_injection_replaces_the_module_check(tmp_path, runs, history_guard_fake):
    seen = []

    def injected(conn, **kw):
        seen.append(kw)
        from tests.history_support import ready_health
        return ready_health(dataset=kw["dataset"], pairs=kw["pairs"], now=kw["now"],
                            holdout_months=kw["holdout_months"], verdict=hh.HISTORY_GAP)

    verdict = _gate(_conn(tmp_path), _settings(), history_health_fn=injected)
    assert verdict.unevaluable_kind == "history_gap" and len(seen) == 1
    assert history_guard_fake.calls == []


# ---- _run_full_gate の既定拒否と入口の運搬 --------------------------------------------

@pytest.mark.parametrize("kind,evaluable,mode,expected", [
    ("ok", True, "enforce", True), ("ok", True, "warn", True),
    ("floor", True, "warn", True), ("floor", True, "enforce", False),
    ("ok", False, "warn", False), ("floor", False, "warn", False),
    ("history_stale", False, "warn", False), ("history_gap", True, "warn", False),
    ("history_check_unavailable", False, "enforce", False),
    ("insufficient_trades", False, "warn", False),
    ("reference_history_missing", False, "warn", False),
    ("history_source_unconfigured", False, "warn", False),
    ("indicator_unresolved", False, "warn", False),
    ("a_kind_added_later", True, "warn", False), ("a_kind_added_later", True, "enforce", False),
])
def test_only_ok_and_a_warned_floor_may_continue(kind, evaluable, mode, expected):
    outcome = approval.GateOutcome(metrics={}, evaluable=evaluable, verdict_kind=kind)
    assert switch._gate_outcome_may_continue(outcome, floor_mode=mode) is expected
