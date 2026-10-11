"""build_runner — mission_worker から backend を選ぶ唯一の入口 (設計書 §1.1)。

認証コピー・handshake 検証は `WorkerRunner` (親、Landlock 外) の責務で
あり、ここでは行わない — 呼び出し時点で workdir/cfg 等は準備済みという
前提。"""
from __future__ import annotations

from pathlib import Path
import threading
from typing import Any, Callable, Literal

from agentic_fx.runners.base import AgentRunner
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
    abort_event: threading.Event | None = None,
    abort_reason_fn: Callable[[], str] | None = None,
    after_tool_call: Callable[[], None] | None = None,
    launch: Any | None = None,
) -> AgentRunner:
    """`launch` は CLI を起こす方法の差し替え (改善 mission の隔離の経路だけが渡す)。
    claude / codex / opencode の runner にそのまま渡し、local は使わない。隔離の経路の
    opencode は llama-swap の URL を設定値でなく egress 定数の literal にする (supervisor は
    `127.0.0.1:8080` だけを通す)。"""
    if launch is not None and profile != "improve":
        raise ValueError("launch is used only by the improve profile")
    choice = getattr(settings.runner, profile)
    if choice.backend == "local":
        from agentic_fx.runners.local_runner import LocalRunner
        return LocalRunner(base_url=settings.llama_swap.base_url,
                           model=choice.model, registry=registry,
                           on_message=on_message, abort_event=abort_event,
                           abort_reason_fn=abort_reason_fn,
                           # local は in-process tool 実行なので dispatcher の
                           # after_send に相当するフックをここで渡す
                           after_tool_call=after_tool_call)
    if choice.backend == "claude":
        from agentic_fx.runners.claude_runner import ClaudeRunner
        return ClaudeRunner(
            bin_path=Path(settings.runner.claude.bin), model=choice.model,
            workdir=workdir, credentials_file_copied=True,
            allowed_tools=["mcp__afx__*"],
            cli_terminate_grace_sec=settings.runner.cli_terminate_grace_sec,
            registry=registry, on_message=on_message,
            cli_started_sink=cli_started_sink, abort_event=abort_event,
            abort_reason_fn=abort_reason_fn, launch=launch)
    if choice.backend == "codex":
        from agentic_fx.runners.codex_runner import CodexRunner
        codex_settings = settings.runner.codex
        return CodexRunner(
            bin_path=Path(codex_settings.bin), model=choice.model,
            workdir=workdir, provider="chatgpt",
            cli_started_sink=cli_started_sink,
            cli_terminate_grace_sec=settings.runner.cli_terminate_grace_sec,
            registry=registry, on_message=on_message,
            abort_event=abort_event, abort_reason_fn=abort_reason_fn, launch=launch)
    if choice.backend == "opencode":
        from agentic_fx.runners.opencode_runner import OpencodeRunner
        # verify_backend は model_copy(update={"backend": backend}) で backend
        # を上書きし、Pydantic validator を再実行しない。設定 validator を
        # 迂回した経路でも未設定の context 窓を runner へ通さない二重の柵。
        if settings.runner.opencode.context_limit <= 0:
            raise ValueError(
                "runner.opencode.context_limit must be >0 when opencode "
                "backend is selected (set it equal to llama-swap --ctx-size "
                "for the model)")
        return OpencodeRunner(
            # 既定値が "~/.opencode/bin/opencode" (チルダ入り) のため展開が
            # 必須 — launcher は argv[0] が絶対パスでないと拒否する (検収
            # 実測 2026-08-30: 未展開だと handshake failed で即死)
            bin_path=Path(settings.runner.opencode.bin).expanduser(),
            model=choice.model,
            context_limit=settings.runner.opencode.context_limit,
            mcp_timeout_ms=int(
                settings.improve.backtest_rpc_timeout_sec * 1000) + 5000,
            workdir=workdir,
            llama_swap_base_url=(settings.llama_swap.base_url if launch is None
                                 else _isolated_llama_swap_url()),
            cli_terminate_grace_sec=settings.runner.cli_terminate_grace_sec,
            registry=registry, on_message=on_message,
            cli_started_sink=cli_started_sink, abort_event=abort_event,
            abort_reason_fn=abort_reason_fn, launch=launch)
    raise ValueError(f"unknown runner backend: {choice.backend!r}")


def _isolated_llama_swap_url() -> str:
    from agentic_fx.egress.policy import LLAMA_SWAP_URL

    return LLAMA_SWAP_URL
