"""AC-17: 取引 event は agent の読み取り面に無い。

取引判断・改善・reflection の tool registry は exact-set で固定し、`ops_events` を読む src は
操作 API の event store と DDL に限る。grep は補助で、主な pin は registry の名前集合である。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops.trade_loop import _TRADE_TOOLS
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.rag import Rag
from agentic_fx.tools.mission_registry import build_mission_registry
from tests.store.test_rag import FakeEmbedding

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "agentic_fx"
SETTINGS = load_settings(ROOT / "config" / "settings.yaml.example")

TRADE_TOOLS = frozenset({
    "get_ohlcv", "get_indicators", "search_news", "get_econ_calendar", "get_positions",
    "get_account", "get_recent_reflections", "search_reflections", "get_signals"})
IMPROVE_TOOLS = frozenset({
    "web_search", "fetch_article", "list_staging", "read_staging_file", "write_staging_file",
    "read_plugin_source", "list_examples", "read_example_plugin", "run_plugin_tests",
    "list_deployed_plugins", "lock_staging_deps", "run_backtest", "analyze_corr"})


def _clock():
    return FixedClock(datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc))


def test_ac17_trade_tool_names_are_an_exact_set():
    assert frozenset(_TRADE_TOOLS) == TRADE_TOOLS and len(_TRADE_TOOLS) == len(TRADE_TOOLS)


def test_ac17_trade_registry_has_exactly_the_read_only_tools(tmp_path):
    conn = connect(tmp_path / "a.db")
    init_db(conn)
    registry = build_mission_registry(
        "trade", conn, SETTINGS, _clock(), Rag(tmp_path / "rag", FakeEmbedding()),
        activity=ActivityLog(tmp_path / "act.log"))
    assert frozenset(registry.names()) == TRADE_TOOLS
    assert not any("event" in name for name in registry.names())


def test_ac17_improve_registry_has_exactly_the_approved_tools(tmp_path):
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger

    conn = connect(tmp_path / "b.db")
    init_db(conn)
    staging, snapshot = tmp_path / "staging", tmp_path / "source"
    staging.mkdir()
    snapshot.mkdir()
    registry = build_mission_registry(
        "improve", conn, SETTINGS, _clock(), Rag(tmp_path / "rag", FakeEmbedding()),
        activity=ActivityLog(tmp_path / "act.log"), staging_dir=staging,
        source_snapshot_dir=snapshot, ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        rpc_handlers={"run_backtest": lambda a: {}, "analyze_corr": lambda a: {}})
    assert frozenset(registry.names()) == IMPROVE_TOOLS
    assert not any("event" in name for name in registry.names())


def test_ac17_ops_events_is_touched_only_by_the_event_store_the_sink_and_the_ddl():
    touching = {str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                if "ops_events" in p.read_text(encoding="utf-8")}
    assert touching == {"ops/events.py", "ops/trade_event_sink.py", "store/db.py"}
    readers = {str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
               if re.search(r"SELECT[^\"']*FROM\s+ops_events", p.read_text(encoding="utf-8"),
                            re.IGNORECASE | re.DOTALL)}
    assert readers == {"ops/events.py"}


def test_ac17_no_agent_facing_module_imports_the_event_store_or_the_sink():
    for folder in ("tools", "loops", "runners", "plugin", "datafeed"):
        for path in (SRC / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "ops.events" not in text and "trade_event" not in text, path
