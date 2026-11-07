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
    "get_econ_reaction", "reaction_stats", "decision_log"])
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
