"""Runner factory — backend に基づいて LocalRunner/ClaudeRunner/CodexRunner を構築。

設計書 §1.3「Runtime Backend Selection」の実装。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from agentic_fx.runners.base import AgentRunner
from agentic_fx.runners.local_runner import LocalRunner
from agentic_fx.config import Settings
from agentic_fx.tools.registry import ToolRegistry


def build_runner(
    profile: str,
    settings: Settings,
    registry: ToolRegistry,
    *,
    on_message: Callable[[dict], None] | None = None,
    workdir: Path,
    cli_started_sink: Callable[[int], None] | None = None,
) -> AgentRunner:
    """settings.runner.<profile>.backend に応じて LocalRunner/ClaudeRunner/CodexRunner
    を構築する。認証コピー・handshake 検証は WorkerRunner (親、Landlock 外) の責務であり
    ここでは行わない — 呼び出し時点で workdir/cfg 等は準備済みという前提。

    Args:
        profile: "trade" または "improve"
        settings: Settings オブジェクト
        registry: improve registry (MCP シム経由で公開)
        on_message: メッセージコールバック (既定 None)
        workdir: 作業ディレクトリ
        cli_started_sink: CLI pgid 回収点 (§7.1-2) (既定 None)
    """
    runner_settings = settings.runner
    profile_config = getattr(runner_settings, profile)
    backend = profile_config.backend

    if backend == "local":
        return LocalRunner(
            base_url=settings.llama_swap.base_url,
            model=profile_config.model,
            registry=registry,
            on_message=on_message,
        )
    elif backend == "claude":
        # Task 2 が実装 (ClaudeRunner は CliRunner を継承)
        from agentic_fx.runners.claude_runner import ClaudeRunner
        return ClaudeRunner(
            bin_path=Path(runner_settings.claude.bin),
            model=profile_config.model,
            workdir=workdir,
            credentials_file_copied=True,  # 呼び出し元が準備済み
            allowed_tools=_allowed_tools_for(profile),
            cli_terminate_grace_sec=runner_settings.cli_terminate_grace_sec,
            registry=registry,
            on_message=on_message,
            cli_started_sink=cli_started_sink,
        )
    elif backend == "codex":
        # Task 3 が実装 (CodexRunner は CliRunner を継承)
        from agentic_fx.runners.codex_runner import CodexRunner
        return CodexRunner(
            bin_path=Path(runner_settings.codex.bin),
            model=profile_config.model,
            workdir=workdir,
            provider=runner_settings.codex.provider,
            llama_swap_base_url=(
                settings.llama_swap.base_url if runner_settings.codex.provider == "llama_swap"
                else None
            ),
            cli_terminate_grace_sec=runner_settings.cli_terminate_grace_sec,
            registry=registry,
            on_message=on_message,
            cli_started_sink=cli_started_sink,
        )
    else:
        raise ValueError(f"Unknown backend: {backend}")


def _allowed_tools_for(profile: str) -> list[str]:
    """profile 別の許可ツール一覧 (§1.6)。

    trade: 取引実行用ツール群 (外向き network 禁止、資格情報利用)
    improve: 改善 / 分析ツール群 (外向き network 許可、sandbox 内)
    """
    if profile == "trade":
        # 取引 mission で使う tools (計画 LoopRunner でフィルタ)
        return [
            "get_latest_bars", "get_indicator_values", "run_backtest",
            "get_pending_orders", "place_order", "cancel_order", "close_position",
            "get_equity_at", "get_positions", "get_open_trades", "news_lookup",
        ]
    elif profile == "improve":
        # 改善 mission で使う tools (MCP shim 経由で転送)
        return [
            "run_backtest", "analyze_corr",
            "list_staging", "read_staging_file", "write_staging_file",
            "read_plugin_source", "run_plugin_tests",
            "web_search", "fetch_article",
        ]
    else:
        raise ValueError(f"Unknown profile: {profile}")
