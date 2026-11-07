"""signal 採否 mission の実 Claude 往復 (realbackend、既定 skip)。

reject 場面は USDJPY short の OPEN order を持つ DB を用意し、`get_positions` だけを
登録した本番と同じ MCP spool dispatcher を runner に繋いで通す。固定 JSON
(`tests/fixtures/signal_decision_claude.json`) は実 Claude 応答を一度保存するまで
実応答は 2026-11-01 に抽出済み (fixture と照合する)。「未抽出」の間は出力を tmp に書くだけで照合しない。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.loops.signal_decision_contract import (
    DECISION_SCHEMA,
    Accept,
    Reject,
    SIGNAL_DECISION_TOOLS,
    parse_decision,
    verify_opposite_position,
)
from agentic_fx.runners.base import Mission
from agentic_fx.runners.claude_runner import ClaudeRunner
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.tools import account_tools
from agentic_fx.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "signal_decision_claude.json"
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
UNEXTRACTED = "未抽出"


def _claude_settings():
    """example を土台に、取引判断の backend だけ claude にした test 用 settings。"""
    base = load_settings(ROOT / "config" / "settings.yaml.example")
    trade = base.runner.trade.model_copy(update={"backend": "claude", "model": "sonnet"})
    return base.model_copy(update={"runner": base.runner.model_copy(update={"trade": trade})})


def _capability_checked_settings():
    settings = _claude_settings()
    if shutil.which(settings.runner.claude.bin) is None:
        pytest.skip(f"claude CLI 未解決: {settings.runner.claude.bin}")
    if not Path(settings.runner.claude.credentials_file).expanduser().is_file():
        pytest.skip("claude credentials 不在")
    return settings


def _seed_db(tmp_path: Path, settings):
    conn = connect(tmp_path / "signal.db")
    init_db(conn)
    orders.insert(conn, pair="USDJPY", direction="short", entry_type="market",
                  horizon="swing", status="open", now=NOW, quantity=0.1,
                  avg_fill_price=148.2, stop_loss=148.8, take_profit=147.0)
    return conn, PaperBroker(conn, settings, FixedClock(NOW))


def _runner(tmp_path: Path, settings, registry: ToolRegistry):
    from agentic_fx.mission_worker import _attach_mcp_dispatcher, _start_mcp_dispatcher
    from agentic_fx.runners.worker_runner import _copy_credentials_file

    workdir = tmp_path / "claude-work"
    (workdir / "cfg").mkdir(parents=True)
    _copy_credentials_file(settings.runner.claude.credentials_file,
                           workdir / "cfg" / ".credentials.json")
    dispatcher = _start_mcp_dispatcher(workdir=workdir, registry=registry)
    runner = ClaudeRunner(
        bin_path=Path(shutil.which(settings.runner.claude.bin)),
        model=settings.runner.trade.model, workdir=workdir,
        credentials_file_copied=True, allowed_tools=["mcp__afx__*"],
        cli_terminate_grace_sec=settings.runner.cli_terminate_grace_sec,
        registry=registry)
    _attach_mcp_dispatcher(runner, dispatcher)
    return runner, dispatcher


@pytest.mark.realbackend
def test_signal_decision_claude_accept_and_opposite_position_reject(tmp_path):
    settings = _capability_checked_settings()
    conn, broker = _seed_db(tmp_path, settings)
    calls: list[tuple[str, bool]] = []
    registry = ToolRegistry(on_result=lambda name, ok, error: calls.append((name, ok)))
    registry.register_all([t for t in account_tools.build(conn, broker)
                           if t.name == "get_positions"])
    assert set(registry.names()) <= set(SIGNAL_DECISION_TOOLS)
    tools = ["get_positions"]
    missions = {
        "accept": Mission(
            prompt=("Strategy signal: USDJPY short is executable. Call get_positions, "
                    "then return only the schema response. Accept unless a reject "
                    "reason applies."),
            tools=tools, output_schema=DECISION_SCHEMA, max_turns=5, timeout_sec=180.0),
        "reject": Mission(
            prompt=("Strategy signal: USDJPY long is executable. Call get_positions. "
                    "If the same pair holds an open order in the opposite direction, "
                    "return the schema response rejecting with opposite_position."),
            tools=tools, output_schema=DECISION_SCHEMA, max_turns=5, timeout_sec=180.0),
    }
    results: dict[str, object] = {}
    try:
        for name, mission in missions.items():
            # 1 mission の workdir は 1 回の run() でしか使えない (prompt.txt は O_EXCL)
            runner, dispatcher = _runner(tmp_path / name, settings, registry)
            try:
                argv = runner._build_argv(mission, mcp_spool=runner.mcp_spool_dir)
                assert json.loads(argv[argv.index("--json-schema") + 1]) == DECISION_SCHEMA
                result = runner.run(mission)
                assert result.status == "completed", result.reason
                assert result.output is not None
                jsonschema.validate(result.output, DECISION_SCHEMA)
                results[name] = result.output
            finally:
                dispatcher.stop()
    finally:
        conn.close()

    (tmp_path / "signal_decision_claude.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    assert ("get_positions", True) in calls
    assert isinstance(parse_decision(results["accept"]), Accept)
    reject = parse_decision(results["reject"])
    assert isinstance(reject, Reject)
    assert verify_opposite_position(
        reject,
        [{"pair": "USDJPY", "direction": "short", "status": "open"}],
        pair="USDJPY", direction="long")

    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if expected.get("status") != UNEXTRACTED:
        assert results == expected
