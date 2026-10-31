"""run_backtest refuses before any work when a declared reference has no history."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agentic_fx.config import ImproveToolBudgetSettings
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult
from agentic_fx.plugin.version_store import content_hash_bytes
from agentic_fx.tools.improve_rpc_tools import build_improve_rpc_tooldefs
from agentic_fx.tools.mission_counters import MissionToolCounters
from agentic_fx.tools.registry import ToolRegistry

ARGS = {"name": "myst", "pair": "USDJPY"}
SAVE = dict(
    scope="in_sample", plugin_ref="plugins/_staging/x/myst", content_hash="cand-hash",
    kind="strategy", pair="USDJPY", timeframe="1h", source="mt5", base_interval="1m",
    params={}, period=(datetime(2020, 1, 1, tzinfo=timezone.utc),
                       datetime(2026, 1, 1, tzinfo=timezone.utc)),
    metrics={"pf": 1.3, "trades": 40, "avg_r": 0.1, "evaluable": True},
    settings_hash="s", core_commit="c", initial_balance=10000.0,
    now=datetime(2026, 8, 22, 12, tzinfo=timezone.utc))


def _row(symbol, source="mt5", interval="1m"):
    return (f"INSERT INTO ohlcv_history (symbol, interval, bar_time, open, high, low, "
            f"close, volume, source) VALUES ('{symbol}', '{interval}', "
            f"'2026-01-01T00:00:00+00:00', 1, 1, 1, 1, 0, '{source}')")


def _world(loop_no_seam, tmp_path, monkeypatch, *, symbols=("JP225",), refs=True,
           seed=()):
    loop, _ = loop_no_seam
    loop._settings = loop._settings.model_copy(update={
        "backtest": loop._settings.backtest.model_copy(update={"eval_source": "mt5"}),
        "datafeed": loop._settings.datafeed.model_copy(
            update={"watch_symbols": list(symbols)})})
    staging = tmp_path / "staging"
    (staging / "myst").mkdir(parents=True)
    for filename, body in (("plugin.py", b"plugin"), ("config.yaml", b"config"),
                           ("test_plugin.py", b"test")):
        (staging / "myst" / filename).write_bytes(body)
    ema = SimpleNamespace(name="ema", kind="indicator", outputs=("v",), max_bars=200,
                          params={}, content_hash="a" * 64, pairs=(), path=tmp_path / "ema")
    inventory = InventoryBuildResult(
        inventory=ApprovedInventory(root=tmp_path, metas=(ema,)),
        phase1_metas=(ema,), resolved={}, rejected_strategies=())
    declared = tuple(
        SimpleNamespace(alias=f"r{i}", plugin="ema", pin=None, params={},
                        symbol=symbol, timeframe="1h", max_age=None)
        for i, symbol in enumerate(symbols)) if refs else ()
    monkeypatch.setattr(
        "agentic_fx.plugin.loader._discover_one",
        lambda path, name: SimpleNamespace(
            name="myst", kind="strategy", timeframe="1h", max_bars=100,
            content_hash=content_hash_bytes(b"plugin", b"config"),
            pairs=("USDJPY",), indicators=declared))
    built, ran = [], []
    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_adapter.build_intent_source",
        lambda meta, **kw: built.append(kw) or SimpleNamespace(close=lambda: None))

    def run_in_sample(*a, record_fn=None, **kw):
        ran.append(1)
        record_fn(dict(SAVE))
        return dict(SAVE["metrics"])
    monkeypatch.setattr("agentic_fx.loops.improve_loop.holdout.run_in_sample", run_in_sample)
    writer = loop._db_write_conn_factory()
    for statement in seed:
        writer.execute(statement)
    writer.commit()
    writer.close()
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={"run_backtest": 600.0})
    handlers = loop._build_rpc_handlers(ledger, staging_dir=staging, inventory=inventory)
    budget = ImproveToolBudgetSettings()
    counters = MissionToolCounters(budget=budget)
    registry = ToolRegistry(on_execute=counters.record_call,
                            on_result=counters.record_tool_result)
    registry.register_all(build_improve_rpc_tooldefs(
        ledger=ledger, run_backtest_handler=handlers["run_backtest"],
        analyze_corr_handler=handlers["analyze_corr"], staging_dir=staging,
        counters=counters, budget=budget))
    return loop, registry, ledger, counters, built, ran


def _counts(loop):
    conn = loop._db_readonly_conn_factory()
    try:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("backtest_runs", "signals", "orders")}
    finally:
        conn.close()


def test_missing_reference_history_stops_with_no_business_side_effect(
        loop_no_seam, tmp_path, monkeypatch):
    loop, registry, ledger, counters, built, ran = _world(
        loop_no_seam, tmp_path, monkeypatch, seed=[_row("USDJPY")])

    raw = registry.execute("run_backtest", ARGS, allowed=registry.names())
    result = json.loads(raw)
    ledger.freeze()

    assert result["started"] is False
    assert result["error"] == "reference_history_missing(JP225, 1h, no_rows)"
    assert _counts(loop) == {"backtest_runs": 0, "signals": 0, "orders": 0}
    assert ran == [] and built == []
    archive = tmp_path / "plugins" / "_archive"
    assert not archive.exists() or not any(archive.rglob("*"))
    entries = ledger.entries()
    assert len(entries) == 1 and entries[0]["kind"] == "run_backtest"
    assert entries[0]["result_summary"]["error"] == result["error"]
    lines = (tmp_path / "activity.log").read_text().splitlines()
    assert len([line for line in lines if "reference_history_missing" in line]) == 1
    assert len(lines) == 1
    # the call is counted like any other, and its candidate slot is handed back
    assert counters.total_calls == 1 and counters.backtest_calls["myst"] == 0


def test_the_refusal_names_the_first_missing_reference_in_declared_order(
        loop_no_seam, tmp_path, monkeypatch):
    _, registry, _, _, _, ran = _world(
        loop_no_seam, tmp_path, monkeypatch, symbols=("US500", "JP225"),
        seed=[_row("USDJPY"), _row("US500")])
    result = json.loads(registry.execute("run_backtest", ARGS, allowed=registry.names()))
    assert result["error"] == "reference_history_missing(JP225, 1h, no_rows)"
    assert ran == []


def test_history_of_another_source_or_interval_does_not_count(
        loop_no_seam, tmp_path, monkeypatch):
    _, registry, _, _, _, ran = _world(
        loop_no_seam, tmp_path, monkeypatch,
        seed=[_row("JP225", source="dukascopy"), _row("JP225", interval="5m")])
    result = json.loads(registry.execute("run_backtest", ARGS, allowed=registry.names()))
    assert result["error"] == "reference_history_missing(JP225, 1h, no_rows)"
    assert ran == []


def test_any_history_at_all_lets_the_backtest_run_even_if_it_is_old_or_sparse(
        loop_no_seam, tmp_path, monkeypatch):
    loop, registry, _, _, built, ran = _world(
        loop_no_seam, tmp_path, monkeypatch, seed=[_row("JP225")])
    result = json.loads(registry.execute("run_backtest", ARGS, allowed=registry.names()))
    assert result.get("started") is not False
    assert "error" not in result
    assert ran == [1] and len(built) == 1


def test_a_strategy_without_literal_references_never_looks_for_history(
        loop_no_seam, tmp_path, monkeypatch):
    _, registry, _, _, built, ran = _world(
        loop_no_seam, tmp_path, monkeypatch, refs=False)
    result = json.loads(registry.execute("run_backtest", ARGS, allowed=registry.names()))
    assert "error" not in result
    assert ran == [1]
