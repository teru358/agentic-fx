"""build_app 経由で有効であるべき既存配線 (policy path、秘密 env の allowlist、
取引判断 loop のツール境界) の回帰ピン。"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

import agentic_fx.service as service_mod
from agentic_fx.config import Settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops.trade_loop import _TRADE_TOOLS
from agentic_fx.policy import Policy, directives_path
from agentic_fx.runners import factory
from agentic_fx.runners.claude_runner import ClaudeRunner
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.runners.local_runner import LocalRunner
from agentic_fx.service import build_app
from agentic_fx.tools.mission_registry import build_mission_registry
from agentic_fx.tools.registry import ToolRegistry
from tests.test_service_app import (
    NOW, _capture_service_warnings, _root_with_settings)

_ALLOWED_NAME = "CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS"


def _claude_trade_root(tmp_path, *, allowlist):
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"token":"x"}')
    creds.chmod(0o600)
    return _root_with_settings(
        tmp_path,
        runner={"trade": {"backend": "claude", "model": "m"},
                "claude": {"bin": sys.executable,
                           "credentials_file": str(creds)}},
        service={"secret_env_allowlist": list(allowlist)})


def _inject_initial_env(monkeypatch, names):
    """検査⑤の既定 seam (`/proc/self/environ`) だけを差し替える。検査本体は実物。"""
    monkeypatch.setitem(
        service_mod._check_service_initial_env_has_no_secrets.__kwdefaults__,
        "read_initial_env_names", lambda: set(names))


def test_build_app_accepts_exact_allowlisted_secret_like_env_name(
        tmp_path, monkeypatch):
    root = _claude_trade_root(tmp_path, allowlist=[_ALLOWED_NAME])
    _inject_initial_env(monkeypatch, {_ALLOWED_NAME, "HOME"})

    with _capture_service_warnings() as records:
        app = build_app(root, runner=FakeRunner([]), clock=FixedClock(NOW))
    app.close()

    assert any(_ALLOWED_NAME in r for r in records)


@pytest.mark.parametrize("leaked", [
    _ALLOWED_NAME + "_V2", "X" + _ALLOWED_NAME, _ALLOWED_NAME.lower() + "_v2"])
def test_build_app_rejects_secret_like_name_that_is_not_an_exact_allowlist_match(
        tmp_path, monkeypatch, leaked):
    root = _claude_trade_root(tmp_path, allowlist=[_ALLOWED_NAME])
    _inject_initial_env(monkeypatch, {leaked, "HOME"})

    with pytest.raises(RuntimeError, match="secret"):
        build_app(root, runner=FakeRunner([]), clock=FixedClock(NOW))


def test_build_app_policy_add_reaches_the_shared_directives_file(tmp_path):
    root = _root_with_settings(tmp_path)
    app = build_app(root, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        assert app.commands._policy_path == directives_path(root)
        assert app.commands.dispatch("policy add 結合ピン") == "policy に追記しました"
    finally:
        app.close()

    assert "結合ピン" in Policy(directives_path(root)).tail(4000)


def _trade_settings(backend: str) -> Settings:
    from pathlib import Path
    import yaml

    raw = yaml.safe_load(
        Path("config/settings.yaml.example").read_text(encoding="utf-8"))
    raw["runner"]["trade"] = {"backend": backend, "model": "m"}
    return Settings.model_validate(raw)


def test_trade_claude_runner_tools_are_exactly_structured_output_and_toolsearch(
        tmp_path):
    from agentic_fx.runners.base import Mission

    settings = _trade_settings("claude")
    workdir = tmp_path / "wd"
    workdir.mkdir()
    runner = factory.build_runner(
        "trade", settings, ToolRegistry(),
        workdir=workdir)
    assert isinstance(runner, ClaudeRunner)

    argv = runner._build_argv(
        Mission(prompt="p", tools=list(_TRADE_TOOLS),
                output_schema={"type": "object"}, max_turns=3, timeout_sec=10),
        mcp_socket=workdir / "afx.sock")

    assert argv.count("--tools") == 1
    assert argv[argv.index("--tools") + 1] == "StructuredOutput,ToolSearch"


def test_trade_local_runner_registry_has_only_read_only_tools(tmp_path):
    from agentic_fx.activity import ActivityLog
    from agentic_fx.store.db import connect, init_db
    from tests.store.test_rag import FakeEmbedding
    from agentic_fx.store.rag import Rag

    settings = _trade_settings("local")
    conn = connect(tmp_path / "agentic.db")
    init_db(conn)
    clock = FixedClock(datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc))
    registry = build_mission_registry(
        "trade", conn, settings, clock, Rag(tmp_path / "rag", FakeEmbedding()),
        activity=ActivityLog(tmp_path / "activity.log"))
    runner = factory.build_runner("trade", settings, registry,
                                  workdir=tmp_path)
    assert isinstance(runner, LocalRunner)

    assert sorted(registry.names()) == sorted(_READ_ONLY_TRADE_TOOLS)
    assert set(_TRADE_TOOLS) <= set(_READ_ONLY_TRADE_TOOLS)


_READ_ONLY_TRADE_TOOLS = [
    "get_ohlcv", "get_indicators", "search_news", "get_econ_calendar",
    "get_positions", "get_account", "get_recent_reflections",
    "search_reflections", "get_signals"]
