"""factory.build_runner — backend選択と設定配線 (プラン10 Task1 Step 26-32)。"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_fx.runners.base import AgentRunner
from agentic_fx.runners.factory import build_runner
from agentic_fx.config import Settings, RunnerChoice, RunnerSettings, ClaudeCliSettings, CodexCliSettings
from agentic_fx.tools.registry import ToolRegistry


def _install_fake_cli_runner_module(monkeypatch, module_name, class_name):
    """指揮者検収 (B-2/B-3, プラン10 Task1) の捕捉形ヘルパ。

    `agentic_fx.runners.claude_runner`/`codex_runner` は Task 2/3 が実装する
    未実装モジュールなので、実クラスを import せず `sys.modules` へ fake module
    を差し込む。`factory.build_runner` はこれらを関数内ローカル import
    (`from agentic_fx.runners.<module> import <ClassName>`) で参照するため、
    `sys.modules` に同名の fake を仕込めば実クラス無しで
    `build_runner` に渡された kwargs (allowed_tools / cli_started_sink 等) を
    捕捉できる。**Task 2/3 merge 後に実クラスで再確認すること** (B-2(a))。
    """
    captured: dict = {}

    class FakeCliRunner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = types.ModuleType(module_name)
    setattr(fake_module, class_name, FakeCliRunner)
    monkeypatch.setitem(sys.modules, module_name, fake_module)
    return FakeCliRunner, captured


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


def test_build_runner_returns_claude_runner_for_claude_backend(tmp_path, monkeypatch):
    """backend=claude を要求すると ClaudeRunner を返す (Task 2 が実装、
    現時点は fake module 捕捉形。Task 2 merge 後に実クラスで再確認)。"""
    FakeClaudeRunner, captured = _install_fake_cli_runner_module(
        monkeypatch, "agentic_fx.runners.claude_runner", "ClaudeRunner")
    settings = _settings_with_backend(improve_backend="claude")
    registry = ToolRegistry()
    runner = build_runner("improve", settings, registry, workdir=tmp_path)
    assert isinstance(runner, FakeClaudeRunner)


def test_build_runner_forwards_settings_to_runner(tmp_path):
    """設定値が runner に渡される (seam で検証)。"""
    settings = _settings_with_backend(improve_backend="local")
    registry = ToolRegistry()

    # LocalRunner は model を使わない (Task 2/3 の CLI runner が使う)
    runner = build_runner("improve", settings, registry, workdir=tmp_path)
    assert runner is not None


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


def test_build_runner_claude_backend_returns_claude_runner(tmp_path, monkeypatch):
    """backend=claude を要求すると ClaudeRunner を返す (Task 2 が実装、
    現時点は fake module 捕捉形。Task 2 merge 後に実クラスで再確認)。"""
    from agentic_fx.config import load_settings

    FakeClaudeRunner, captured = _install_fake_cli_runner_module(
        monkeypatch, "agentic_fx.runners.claude_runner", "ClaudeRunner")
    EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
    settings = load_settings(EXAMPLE)
    settings = settings.model_copy(update={
        "runner": settings.runner.model_copy(update={
            "improve": settings.runner.improve.model_copy(
                update={"backend": "claude"})})})
    runner = build_runner("improve", settings, ToolRegistry(), workdir=tmp_path)
    assert isinstance(runner, FakeClaudeRunner)


def test_build_runner_codex_backend_returns_codex_runner(tmp_path, monkeypatch):
    """backend=codex を要求すると CodexRunner を返す (Task 3 が実装、
    現時点は fake module 捕捉形。Task 3 merge 後に実クラスで再確認)。"""
    from agentic_fx.config import load_settings

    FakeCodexRunner, captured = _install_fake_cli_runner_module(
        monkeypatch, "agentic_fx.runners.codex_runner", "CodexRunner")
    EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
    settings = load_settings(EXAMPLE)
    settings = settings.model_copy(update={
        "runner": settings.runner.model_copy(update={
            "improve": settings.runner.improve.model_copy(
                update={"backend": "codex"})})})
    runner = build_runner("improve", settings, ToolRegistry(), workdir=tmp_path)
    assert isinstance(runner, FakeCodexRunner)


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


def test_build_runner_forwards_cli_started_sink_to_claude_runner(tmp_path, monkeypatch):
    """(裁定 R1/RB3) build_runner が cli_started_sink= を受け取ったとき、
    claude backend の CliRunner 系 (ClaudeRunner) の __init__ にそのまま
    透過することの単体 pin。呼び出し元 (Task 4 Step 7d) が渡した closure
    が確実に CliRunner まで届くことを、factory 単体で保証する。
    現時点は fake module 捕捉形。Task 2 merge 後に実クラスで再確認する。"""
    from agentic_fx.config import load_settings

    FakeClaudeRunner, captured = _install_fake_cli_runner_module(
        monkeypatch, "agentic_fx.runners.claude_runner", "ClaudeRunner")
    EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
    settings = load_settings(EXAMPLE)
    settings = settings.model_copy(update={
        "runner": settings.runner.model_copy(update={
            "improve": settings.runner.improve.model_copy(
                update={"backend": "claude"})})})
    sink_calls = []
    sink = sink_calls.append
    runner = build_runner(
        "improve", settings, ToolRegistry(), workdir=tmp_path,
        cli_started_sink=sink)
    assert isinstance(runner, FakeClaudeRunner)
    assert captured["cli_started_sink"] is sink


def test_build_runner_trade_claude_allowed_tools_excludes_bash(tmp_path, monkeypatch):
    """Step 29 M2 killer: profile=trade + backend=claude のとき
    `allowed_tools` に `"Bash"` 等の裸ツール名が混入していないことの pin
    (§1.6: trade は `["mcp__afx__*"]` のみ)。現時点は fake module 捕捉形。
    Task 2 merge 後に実クラスで再確認する。"""
    from agentic_fx.config import load_settings

    FakeClaudeRunner, captured = _install_fake_cli_runner_module(
        monkeypatch, "agentic_fx.runners.claude_runner", "ClaudeRunner")
    EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
    settings = load_settings(EXAMPLE)
    settings = settings.model_copy(update={
        "runner": settings.runner.model_copy(update={
            "trade": settings.runner.trade.model_copy(
                update={"backend": "claude"})})})
    build_runner("trade", settings, ToolRegistry(), workdir=tmp_path)
    assert captured["allowed_tools"] == ["mcp__afx__*"]
    assert "Bash" not in captured["allowed_tools"]


def test_build_runner_improve_claude_allowed_tools_includes_edit_tools(
        tmp_path, monkeypatch):
    """段 0 M17 pin: profile=improve + backend=claude のとき `allowed_tools`
    が trade と異なり `Bash`/`Read`/`Write`/`Edit`/`Glob`/`Grep` を含む
    **完全一致**であること (§1.6: 改善ループは `plugins/` を編集できる必要が
    ある)。`in` 判定だけでは 1 要素の欠落を取り逃す (メモリ 6.8) ため、
    リスト全体を等値比較する — 段 0 変異スイープ M17: `build_runner` の
    `allowed_tools=(... if profile == "trade" else [...])` の条件式ごと
    trade 側 (`["mcp__afx__*"]`) に潰す変異が、この pin が無い状態では
    全スイート green のまま生存した (improve の Mission が Bash 系ツールを
    一つも持たずに起動し、改善ループの存在理由そのものを失う致命的な
    退行)。`test_build_runner_trade_claude_allowed_tools_excludes_bash`
    と対で置く。"""
    from agentic_fx.config import load_settings

    FakeClaudeRunner, captured = _install_fake_cli_runner_module(
        monkeypatch, "agentic_fx.runners.claude_runner", "ClaudeRunner")
    EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
    settings = load_settings(EXAMPLE)
    settings = settings.model_copy(update={
        "runner": settings.runner.model_copy(update={
            "improve": settings.runner.improve.model_copy(
                update={"backend": "claude"})})})
    build_runner("improve", settings, ToolRegistry(), workdir=tmp_path)
    assert captured["allowed_tools"] == [
        "mcp__afx__*", "Bash", "Read", "Write", "Edit", "Glob", "Grep"]


def test_build_runner_returns_real_claude_runner_class(tmp_path):
    """Task 2 完了後の real-class pin (束 A 申し送り item 7):
    build_runner(backend=claude) が実 ClaudeRunner クラスを返すことを確認。"""
    from agentic_fx.config import load_settings
    from agentic_fx.runners.claude_runner import ClaudeRunner

    EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
    settings = load_settings(EXAMPLE)
    settings = settings.model_copy(update={
        "runner": settings.runner.model_copy(update={
            "improve": settings.runner.improve.model_copy(
                update={"backend": "claude"})})})
    runner = build_runner("improve", settings, ToolRegistry(), workdir=tmp_path)
    assert isinstance(runner, ClaudeRunner)

