"""build_runner — mission_worker から backend を選ぶ唯一の入口 (設計書 §1.1)。

認証コピー・handshake 検証は `WorkerRunner` (親、Landlock 外) の責務で
あり、ここでは行わない — 呼び出し時点で workdir/cfg 等は準備済みという
前提。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal

from agentic_fx.runners.base import AgentRunner
from agentic_fx.runners.local_runner import LocalRunner
from agentic_fx.tools.registry import ToolRegistry


# precheck 2026-08-22 pass2: RB3 — cli_started_sink= を追加し claude/codex
# 分岐の CliRunner 系へ透過する。local 分岐は無視する (LocalRunner は
# CLI 子プロセスを持たないため cli_started フレームの送出点が無い)。
def build_runner(
    profile: Literal["trade", "improve"],
    settings: Any,
    registry: ToolRegistry,
    *,
    on_message: Callable[[dict], None] | None = None,
    workdir: Path,
    cli_started_sink: Callable[[int], None] | None = None,
) -> AgentRunner:
    choice = getattr(settings.runner, profile)
    if choice.backend == "local":
        return LocalRunner(base_url=settings.llama_swap.base_url,
                           model=choice.model, registry=registry,
                           on_message=on_message)
    if choice.backend == "claude":
        from agentic_fx.runners.claude_runner import ClaudeRunner
        return ClaudeRunner(
            bin_path=Path(settings.runner.claude.bin), model=choice.model,
            workdir=workdir, credentials_file_copied=True,
            allowed_tools=(["mcp__afx__*"] if profile == "trade"
                           else ["mcp__afx__*", "Bash", "Read", "Write",
                                 "Edit", "Glob", "Grep"]),
            cli_terminate_grace_sec=settings.runner.cli_terminate_grace_sec,
            registry=registry, on_message=on_message,
            cli_started_sink=cli_started_sink)
    if choice.backend == "codex":
        from agentic_fx.runners.codex_runner import CodexRunner
        codex_settings = settings.runner.codex
        return CodexRunner(
            bin_path=Path(codex_settings.bin), model=choice.model,
            workdir=workdir, provider=codex_settings.provider,
            cli_started_sink=cli_started_sink,
            llama_swap_base_url=(settings.llama_swap.base_url
                                 if codex_settings.provider == "llama_swap"
                                 else None),
            cli_terminate_grace_sec=settings.runner.cli_terminate_grace_sec,
            registry=registry, on_message=on_message)
    raise ValueError(f"unknown runner backend: {choice.backend!r}")
