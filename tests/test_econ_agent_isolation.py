"""signal の採否・shadow 成績・反応統計を agent の面へ出さないことの遮断 test。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.loops import trade_loop
from agentic_fx.loops.improve_context import build_improve_context
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.loops.signal_decision_contract import SIGNAL_DECISION_TOOLS
from agentic_fx.store import signal_decisions as sd
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.signal_shadow import ShadowSpread
from agentic_fx.tools import improve_rpc_tools, mission_registry, signal_tools
from agentic_fx.tools.registry import ToolDef

NOW = datetime(2026, 10, 30, 13, 0, 7, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example")
HANDLERS = {"run_backtest": lambda a: {}, "analyze_corr": lambda a: {}}


def _improve_registry(tmp_path):
    staging, snapshot = tmp_path / "s", tmp_path / "src"
    staging.mkdir(exist_ok=True)
    snapshot.mkdir(exist_ok=True)
    ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={
        "run_backtest": 1.0, "analyze_corr": 1.0})
    return mission_registry.build_mission_registry(
        "improve", None, SETTINGS, None, None, activity=None,
        staging_dir=staging, source_snapshot_dir=snapshot, ledger=ledger,
        rpc_handlers=HANDLERS)


def test_improve_tool_allowlist_is_disjoint_from_forbidden(tmp_path):
    names = set(_improve_registry(tmp_path).names())
    assert names
    assert not names & signal_tools.IMPROVE_FORBIDDEN
    assert signal_tools.improve_forbidden_hits(names) == []


@pytest.mark.parametrize("name", [
    "get_signals", "bless", "get_signal_decisions", "shadow_results",
    "get_econ_reaction", "get_econ_reactions", "reaction_stats", "decision_log"])
def test_adding_a_forbidden_tool_to_the_improve_allowlist_fails_the_build(
        tmp_path, name, monkeypatch):
    real = improve_rpc_tools.build_improve_rpc_tooldefs

    def with_extra(**kwargs):
        return [*real(**kwargs), ToolDef(
            name=name, description="x", parameters={"type": "object"},
            func=lambda **_: {})]

    monkeypatch.setattr(improve_rpc_tools, "build_improve_rpc_tooldefs", with_extra)
    with pytest.raises(ValueError, match="forbidden"):
        _improve_registry(tmp_path)


def test_trade_agent_tool_lists_never_mix_signal_and_cron_surfaces():
    assert "get_signals" not in trade_loop._CRON_TOOLS
    assert "get_signals" in trade_loop._ASK_TOOLS
    assert set(SIGNAL_DECISION_TOOLS) == {"search_news", "get_positions"}


def test_get_econ_reactions_is_on_the_cron_trade_list_only():
    name = "get_econ_reactions"
    assert name in trade_loop._CRON_TOOLS
    assert name not in trade_loop._ASK_TOOLS
    assert name not in SIGNAL_DECISION_TOOLS
    assert name in trade_loop._TRADE_TOOLS
    assert signal_tools.improve_forbidden_hits([name]) == [name]
    with pytest.raises(ValueError, match="forbidden"):
        signal_tools.assert_improve_allowlist(["get_ohlcv", name])


def test_the_ask_tool_list_keeps_its_contents_and_order():
    assert trade_loop._ASK_TOOLS == [
        "get_ohlcv", "get_indicators", "search_news", "get_econ_calendar",
        "get_positions", "get_account", "get_recent_reflections",
        "search_reflections", "get_signals"]
    assert trade_loop._CRON_TOOLS == [
        "get_ohlcv", "get_indicators", "search_news", "get_econ_calendar",
        "get_positions", "get_account", "get_recent_reflections",
        "search_reflections", "get_econ_reactions"]


def test_the_registry_refuses_get_econ_reactions_under_ask_and_decision_lists(tmp_path):
    from agentic_fx.activity import ActivityLog
    from agentic_fx.core.contracts import FixedClock
    from tests.store.test_rag import FakeEmbedding
    from agentic_fx.store.rag import Rag
    conn = connect(tmp_path / "r.db")
    init_db(conn)
    registry = mission_registry.build_mission_registry(
        "trade", conn, SETTINGS, FixedClock(NOW), Rag(tmp_path / "rag", FakeEmbedding()),
        activity=ActivityLog(tmp_path / "a.log"))
    assert "get_econ_reactions" in registry.names()
    for allowed in (trade_loop._ASK_TOOLS, list(SIGNAL_DECISION_TOOLS)):
        got = json.loads(registry.execute("get_econ_reactions", {"pair": "USDJPY"}, allowed))
        assert "not allowed" in got["error"]
    assert "get_econ_reactions" in [
        t["function"]["name"] for t in registry.openai_tools(trade_loop._CRON_TOOLS)]
    assert "get_econ_reactions" not in [
        t["function"]["name"] for t in registry.openai_tools(trade_loop._ASK_TOOLS)]


def test_the_improve_registry_has_no_get_econ_reactions(tmp_path):
    assert "get_econ_reactions" not in _improve_registry(tmp_path).names()


def test_reaction_values_are_not_injected_into_the_trade_prompt_or_state_summary(tmp_path):
    from agentic_fx.analytics.econ_reactions import WindowValue
    from agentic_fx.runners.base import MissionResult
    from agentic_fx.store import econ_reaction_values as rv
    from tests.loops.test_trade_loop import _loop
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    for i in range(25):
        ts = f"2026-10-{i + 1:02d}T12:00:00+00:00"
        event = conn.execute("INSERT INTO econ_events(ts,country,name,importance) VALUES(?,?,?,?)",
                             (ts, "JPY", "SentinelRate", 3)).lastrowid
        set_id = conn.execute(
            "INSERT INTO ohlcv_slice_sets(event_id,pair,range_start,range_end,expected_rows,state,"
            "created_at,completed_at,rule_version) VALUES(?,?,?,?,?,'complete',?,?,1)",
            (event, "USDJPY", ts, f"2026-11-{i + 1:02d}T12:00:00+00:00", 255, ts, ts)).lastrowid
        conn.commit()
        values = tuple(WindowValue(w, "resolved", 7777.25, -2.0, 2.0, 1.0) for w in (15, 60, 240))
        assert rv.write_set_tx(conn, set_id=set_id, values=values, keys=[(event, "JPY")],
                               now=NOW).committed
    _, loop, runner, _ = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "w"}, [])], conn=conn)
    loop.run_once()
    prompt = runner.missions[0].prompt
    for sentinel in ("7777", "SentinelRate", "get_econ_reactions", "range_median"):
        assert sentinel not in prompt


def test_improve_context_carries_no_decision_or_shadow_values(tmp_path):
    conn = connect(tmp_path / "d.db")
    init_db(conn)
    mid = conn.execute(
        "INSERT INTO missions (loop,runner,model,status,started_at,trigger) "
        "VALUES ('trade','local','m','running',?,'signal')",
        (NOW.isoformat(),)).lastrowid
    sid = conn.execute(
        "INSERT INTO signals (plugin,content_hash,pair,timeframe,bar_ts,kind,"
        "payload_json,status,claimed_by_mission_id,claimed_at,created_at,"
        "contract_version) VALUES ('sentinelplugin','h','USDJPY','15m',"
        "'2026-10-30T12:45:00+00:00','strategy','{}','claimed',?,?,?,"
        "'executable_v1')", (mid, NOW.isoformat(), NOW.isoformat())).lastrowid
    conn.commit()
    sd.commit_reject(
        conn, mission_id=mid, signal_id=sid, actor="llm", reject_code="news_shock",
        spread=ShadowSpread(pip_size=0.01, spread_pips=1.2, source="quote"),
        output={"decision": "reject"}, transcript=[], now=NOW)
    conn.execute("UPDATE signal_shadow SET state='resolved', result_code='tp', "
                 "entry_price=150.123, entry_at=?, exit_price=151.987, "
                 "pnl_pips=186.4, resolved_at=? WHERE signal_id=?",
                 (NOW.isoformat(), NOW.isoformat(), sid))
    conn.commit()
    ctx = build_improve_context(conn, settings=SETTINGS, now=NOW, root=tmp_path,
                                allowed_backlog_ids=None)
    blob = json.dumps(ctx, default=str, ensure_ascii=False)
    for sentinel in ("sentinelplugin", "news_shock", "186.4", "151.987",
                     "signal_shadow", "signal_decisions"):
        assert sentinel not in blob


@pytest.mark.parametrize("name", [
    "trade_mission", "ask_mission", "signal_decision_mission", "improve_mission",
    "reflection"])
def test_agent_prompts_name_no_human_only_signal_facts(name):
    text = (Path(__file__).resolve().parents[1] / "src" / "agentic_fx" / "loops"
            / "prompts" / f"{name}.md").read_text(encoding="utf-8").lower()
    for word in ("shadow", "abandon", "slice", "wait_budget", "reaction",
                 "worst_case_guarantee", "signal_capacity"):
        assert word not in text
