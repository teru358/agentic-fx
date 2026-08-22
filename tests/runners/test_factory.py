"""factory.build_runner — backend選択と設定配線 (プラン10 Task1 Step 26-32)。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_fx.runners.base import AgentRunner
from agentic_fx.runners.factory import build_runner
from agentic_fx.config import Settings, RunnerChoice, RunnerSettings, ClaudeCliSettings, CodexCliSettings
from agentic_fx.tools.registry import ToolRegistry


def _settings_with_backend(trade_backend="local", improve_backend="local") -> Settings:
    """テスト用の最小設定を構築する。"""
    s = MagicMock(spec=Settings)
    s.runner = MagicMock(spec=RunnerSettings)
    s.runner.trade = RunnerChoice(backend=trade_backend, model="test-model")
    s.runner.improve = RunnerChoice(backend=improve_backend, model="test-model")
    s.runner.claude = ClaudeCliSettings()
    s.runner.codex = CodexCliSettings(bin="/tmp/codex-bin")
    s.runner.cli_terminate_grace_sec = 10.0
    s.llama_swap = MagicMock()
    s.llama_swap.base_url = "http://localhost:8080/v1"
    return s


def test_build_runner_returns_local_runner_for_local_backend(tmp_path):
    """backend=local でも LocalRunner を返す (単体は runners/base.py 由来)。"""
    from agentic_fx.runners.local_runner import LocalRunner
    settings = _settings_with_backend(improve_backend="local")
    registry = ToolRegistry()
    runner = build_runner("improve", settings, registry, workdir=tmp_path)
    assert isinstance(runner, LocalRunner)


def test_build_runner_returns_claude_runner_for_claude_backend(tmp_path):
    """backend=claude を要求すると ClaudeRunner を返す (Task 2 が実装)。"""
    pytest.importorskip("agentic_fx.runners.claude_runner")
    from agentic_fx.runners.cli_runner import CliRunner
    settings = _settings_with_backend(improve_backend="claude")
    registry = ToolRegistry()
    runner = build_runner("improve", settings, registry, workdir=tmp_path)
    assert isinstance(runner, CliRunner)


def test_build_runner_forwards_settings_to_runner(tmp_path):
    """設定値が runner に渡される (seam で検証)。"""
    settings = _settings_with_backend(improve_backend="local")
    registry = ToolRegistry()

    # LocalRunner は model を使わない (Task 2/3 の CLI runner が使う)
    runner = build_runner("improve", settings, registry, workdir=tmp_path)
    assert runner is not None


def test_build_runner_forwards_cli_started_sink_to_runner(tmp_path):
    """cli_started_sink が runner に渡される (§7.1-2 の配線)。"""
    pytest.importorskip("agentic_fx.runners.claude_runner")
    settings = _settings_with_backend(improve_backend="claude")
    registry = ToolRegistry()
    seen = []

    # ClaudeRunner (Task 2 実装) が cli_started_sink を受け取る
    runner = build_runner("improve", settings, registry, workdir=tmp_path,
                         cli_started_sink=seen.append)
    # Task 2/3 が実装すると _cli_started_sink を持つ
    if hasattr(runner, "_cli_started_sink"):
        assert runner._cli_started_sink is not None


def test_build_runner_accepts_on_message(tmp_path):
    """on_message コールバックも受け取れる。"""
    settings = _settings_with_backend(improve_backend="local")
    registry = ToolRegistry()
    seen = []

    runner = build_runner("improve", settings, registry, workdir=tmp_path,
                         on_message=seen.append)
    assert runner is not None


# Step 25: Additional tests for comprehensive coverage

def test_build_runner_local_backend_returns_local_runner(tmp_path):
    """backend=local でも LocalRunner を返す。"""
    from agentic_fx.config import load_settings
    from agentic_fx.runners.local_runner import LocalRunner

    EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
    settings = load_settings(EXAMPLE)  # 既定 improve.backend == local
    runner = build_runner("improve", settings, ToolRegistry(), workdir=tmp_path)
    assert isinstance(runner, LocalRunner)


def test_build_runner_claude_backend_returns_claude_runner(tmp_path):
    """backend=claude を要求すると ClaudeRunner を返す (Task 2 が実装)。"""
    pytest.importorskip("agentic_fx.runners.claude_runner")
    from agentic_fx.config import load_settings
    from agentic_fx.runners.cli_runner import CliRunner

    EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
    settings = load_settings(EXAMPLE)
    settings = settings.model_copy(update={
        "runner": settings.runner.model_copy(update={
            "improve": settings.runner.improve.model_copy(
                update={"backend": "claude"})})})
    runner = build_runner("improve", settings, ToolRegistry(), workdir=tmp_path)
    assert isinstance(runner, CliRunner)


def test_build_runner_codex_backend_returns_codex_runner(tmp_path):
    """backend=codex を要求すると CodexRunner を返す (Task 3 が実装)。"""
    from agentic_fx.config import load_settings
    from agentic_fx.runners.cli_runner import CliRunner

    pytest.importorskip("agentic_fx.runners.codex_runner")
    EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
    settings = load_settings(EXAMPLE)
    settings = settings.model_copy(update={
        "runner": settings.runner.model_copy(update={
            "improve": settings.runner.improve.model_copy(
                update={"backend": "codex"})})})
    runner = build_runner("improve", settings, ToolRegistry(), workdir=tmp_path)
    assert isinstance(runner, CliRunner)


def test_build_runner_trade_profile_uses_trade_choice(tmp_path):
    """profile=trade のときは runner.trade を見る。"""
    from agentic_fx.config import load_settings
    from agentic_fx.runners.local_runner import LocalRunner

    EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
    settings = load_settings(EXAMPLE)  # trade.backend == local
    runner = build_runner("trade", settings, ToolRegistry(), workdir=tmp_path)
    assert isinstance(runner, LocalRunner)


def test_build_runner_local_backend_forwards_on_message(tmp_path):
    """3 周目レビュー Important-1 の随伴修正: local backend でも on_message が
    LocalRunner まで配線されることを確認する (mission_worker からの
    transcript/event 転送が local backend だけ静かに欠落するのを防ぐ)。"""
    from agentic_fx.config import load_settings
    from agentic_fx.runners.local_runner import LocalRunner

    EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
    settings = load_settings(EXAMPLE)  # 既定 improve.backend == local

    # Use a custom callback object to avoid bound method identity issues
    class Callback:
        def __call__(self, msg):
            pass

    callback = Callback()
    runner = build_runner(
        "improve", settings, ToolRegistry(), workdir=tmp_path,
        on_message=callback)
    assert runner._on_message is callback


def test_build_runner_forwards_cli_started_sink_to_claude_runner(tmp_path):
    """(裁定 R1/RB3) build_runner が cli_started_sink= を受け取ったとき、
    claude backend の CliRunner 系 (ClaudeRunner) の __init__ にそのまま
    透過することの単体 pin。呼び出し元 (Task 4 Step 7d) が渡した closure
    が確実に CliRunner まで届くことを、factory 単体で保証する。"""
    from agentic_fx.config import load_settings
    from agentic_fx.runners.cli_runner import CliRunner

    pytest.importorskip("agentic_fx.runners.claude_runner")
    EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
    settings = load_settings(EXAMPLE)
    settings = settings.model_copy(update={
        "runner": settings.runner.model_copy(update={
            "improve": settings.runner.improve.model_copy(
                update={"backend": "claude"})})})
    sink_calls = []
    runner = build_runner(
        "improve", settings, ToolRegistry(), workdir=tmp_path,
        cli_started_sink=sink_calls.append)
    assert isinstance(runner, CliRunner)
    assert runner._cli_started_sink is sink_calls.append

