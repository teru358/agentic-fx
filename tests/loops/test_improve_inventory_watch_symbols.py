"""The improve inventory tells the agent which symbols it may reference, and nothing about their history."""
from __future__ import annotations

import json
from pathlib import Path

from agentic_fx.config import load_settings
from agentic_fx.loops.improve_loop import build_inventory_view
from agentic_fx.store.db import connect, init_db
from agentic_fx.tools.improve_staging_tools import build_improve_staging_tooldefs

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
BASE = load_settings(EXAMPLE)


def _settings(watch, *, source="mt5"):
    return BASE.model_copy(update={
        "backtest": BASE.backtest.model_copy(update={"eval_source": source}),
        "datafeed": BASE.datafeed.model_copy(update={"watch_symbols": list(watch)})})


def _result():
    from agentic_fx.plugin.resolve import ApprovedInventory, InventoryBuildResult
    return InventoryBuildResult(
        inventory=ApprovedInventory(root=Path("/nonexistent"), metas=()),
        phase1_metas=(), resolved={}, rejected_strategies=())


def _conn(tmp_path, rows=()):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    for symbol, bar_time, volume in rows:
        conn.execute(
            "INSERT INTO ohlcv_history (symbol, interval, bar_time, open, high, low,"
            " close, volume, source) VALUES (?, '1m', ?, 1, 1, 1, 1, ?, 'mt5')",
            (symbol, bar_time, volume))
    conn.commit()
    return conn


def test_the_view_carries_the_watch_list_and_a_boolean_per_symbol(tmp_path):
    conn = _conn(tmp_path, [("JP225", "2019-03-04T05:06:07+00:00", 7777)])
    view = build_inventory_view(_result(), settings=_settings(["JP225"]), conn=conn)
    assert view["watch_symbols"] == ["JP225"]
    assert view["history_available"] == {"JP225": True}
    assert set(view) == {"plugins", "pin_broken_strategies", "watch_symbols",
                         "history_available"}


def test_the_boolean_is_per_symbol_and_false_without_history(tmp_path):
    conn = _conn(tmp_path, [("JP225", "2019-03-04T05:06:07+00:00", 1)])
    view = build_inventory_view(_result(), settings=_settings(["JP225", "US500"]), conn=conn)
    assert view["watch_symbols"] == ["JP225", "US500"]
    assert view["history_available"] == {"JP225": True, "US500": False}
    empty = build_inventory_view(_result(), settings=_settings([]), conn=conn)
    assert empty["watch_symbols"] == [] and empty["history_available"] == {}
    unconfigured = build_inventory_view(
        _result(), settings=_settings(["JP225"], source="none"), conn=conn)
    assert unconfigured["history_available"] == {"JP225": False}


def test_nothing_about_the_extent_or_quality_of_the_history_reaches_the_view(tmp_path):
    conn = _conn(tmp_path, [("JP225", "2019-03-04T05:06:07+00:00", 4242),
                            ("JP225", "2025-09-08T10:11:12+00:00", 4343)])
    view = build_inventory_view(_result(), settings=_settings(["JP225"]), conn=conn)
    text = json.dumps(view)
    for leaked in ("2019", "2025", "4242", "4343", "05:06", "count", "first", "last"):
        assert leaked not in text
    assert view["watch_symbols"] == ["JP225"]


def test_the_tool_returns_the_watch_list_and_the_booleans_only(tmp_path):
    staging = tmp_path / "s"
    source = tmp_path / "src"
    staging.mkdir(), source.mkdir()
    view = {"plugins": [], "pin_broken_strategies": [], "watch_symbols": ["JP225", "US500"],
            "history_available": {"JP225": True, "US500": False}, "history_range": ["2019", "2025"]}
    tools = {t.name: t for t in build_improve_staging_tooldefs(
        staging_dir=staging, source_snapshot_dir=source, inventory_view=view)}
    assert tools["list_deployed_plugins"].func() == {
        "plugins": [], "pin_broken_strategies": [], "watch_symbols": ["JP225", "US500"],
        "history_available": {"JP225": True, "US500": False}}


def test_the_prompt_rule_about_reference_symbols_is_present():
    text = (Path(__file__).resolve().parents[2] / "src" / "agentic_fx" / "loops"
            / "prompts" / "improve_mission.md").read_text(encoding="utf-8")
    assert "参照系列は `symbol:` 宣言で取り、自前で別 symbol を読まないでください。" in text


def test_a_deployed_reference_strategy_is_shown_under_its_own_v2_family_label():
    from types import SimpleNamespace

    from agentic_fx.loops.improve_context import _deployed_view, deployed_families

    def ref(symbol):
        return SimpleNamespace(alias="a", plugin="ema", symbol=symbol, timeframe=None)

    def meta(name, kind, indicators=()):
        return SimpleNamespace(name=name, kind=kind, timeframe="1h", pairs=("USDJPY",),
                               indicators=indicators)

    families = deployed_families([
        meta("ema", "indicator"), meta("plain", "strategy", (ref("self"),)),
        meta("jp", "strategy", (ref("JP225"),)), meta("us", "strategy", (ref("US500"),))])
    jp = next(f for f in families if f["name"] == "jp")
    assert jp["dependencies"] == [
        {"alias": "a", "plugin": "ema", "symbol": "JP225", "timeframe": None}]
    assert "dependencies" not in next(f for f in families if f["name"] == "plain")
    keyed, _, _, _ = _deployed_view(families, {"USDJPY"})
    assert keyed["strategy:ema@1h@USDJPY"] == ["plain"]
    assert keyed["strategy-v2:ema@1h@USDJPY"] == ["jp", "us"]
