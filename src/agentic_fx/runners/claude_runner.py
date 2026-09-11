"""ClaudeRunner — claude CLI 直駆動 (設計書 §1.2)。"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable, Literal

from agentic_fx.runners.base import Mission
from agentic_fx.runners.cli_runner import CliRunner
from agentic_fx.runners.response_parser import ParseError, parse_json_output
from agentic_fx.tools.registry import ToolRegistry

# [claude-builtin-tools-exposed] 是正 (A4 10 回目 claude #69 観測 C、
# 2026-09-11): `--allowedTools mcp__afx__*` は claude CLI の意味論では
# 「自動承認の allowlist」であって「可用性のフィルタ」ではないため、
# 組み込みツール (Bash/Edit/Write/…) は `--allowedTools` を絞っても
# `system/init` にそのまま広告され、実測でも `ToolSearch`/`StructuredOutput`
# が実行された (registry を通らず `max_tool_calls` に数えられない)。
#
# codex 1 周目 I2/I3 是正 (2026-09-11): denylist (`--disallowedTools`) は
# CLI 更新で新しい組み込みツール名 (`Glob`/`Grep`/`LS`/`MultiEdit`/
# `BashOutput`/`KillShell`/`TodoWrite`/`AskUserQuestion`/`EnterPlanMode`/
# `ExitPlanMode` 等、観測時点の denylist に無い名前) が広告されると
# fail-open する。CLI 2.1.268 の `--help` が明記する `--tools` (組み込み
# ツール可用性そのものの allowlist、`""` で全無効化) に置換する — 未知の
# 新規組み込みツールも既定で不可視になる。`StructuredOutput`
# (`_extract_output` が最終出力として読む) と `ToolSearch` (MCP の
# deferred ツール読み込みに要る可能性がある) だけを許可する。`Read` は
# 許可集合から落とす — Landlock の read_only は workdir だけでなく
# `/etc`・コードツリー・`/proc` を含むため、built-in `Read` を許すと
# registry を経由しない読み取りが可能になる (監査可能な MCP
# `read_staging_file` / `read_plugin_source` に一本化する)。
CLAUDE_BUILTIN_TOOLS = ("StructuredOutput", "ToolSearch")


# precheck 2026-08-22 pass2: RB3 追随 (build_runner から cli_started_sink= を
# 透過するため、__init__ シグネチャと super().__init__() 呼び出しに追加)
class ClaudeRunner(CliRunner):
    def __init__(self, *, bin_path: Path, model: str, workdir: Path,
                 credentials_file_copied: bool,
                 allowed_tools: list[str],
                 cli_terminate_grace_sec: float,
                 registry: ToolRegistry,
                 on_message: Callable[[dict], None] | None = None,
                 cli_started_sink: Callable[[int], None] | None = None,
                 abort_event: threading.Event | None = None,
                 abort_reason_fn: Callable[[], str] | None = None) -> None:
        # #26 (`verified-round1.md` 1-B): `--allowedTools ""` (空リスト) は
        # claude CLI の意味論では「制限なし」に近い挙動になりうる —
        # 空リストを渡す変異/設定ミスを fail closed で拒否する (production
        # では `factory.py` が非空の固定リストしか渡さない)。
        if not allowed_tools:
            raise ValueError("ClaudeRunner requires a non-empty allowed_tools list")
        self._allowed_tools = allowed_tools
        super().__init__(bin_path=bin_path, model=model, workdir=workdir,
                         cli_terminate_grace_sec=cli_terminate_grace_sec,
                         registry=registry, on_message=on_message,
                         cli_started_sink=cli_started_sink,
                         abort_event=abort_event,
                         abort_reason_fn=abort_reason_fn)

    def _build_argv(self, mission: Mission, *, mcp_socket: Path) -> list[str]:
        mcp_config_path = self._workdir / "mcp.json"
        mcp_config = {"mcpServers": {"afx": {
            "command": str(Path(__import__("sys").executable)),
            "args": ["-m", "agentic_fx.tools.mcp_shim", str(mcp_socket)],
            "env": {}}}}
        mcp_config_path.write_text(json.dumps(mcp_config))
        return [
            str(self._bin_path), "-p", mission.prompt,
            "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(mission.output_schema),
            "--setting-sources", "",
            "--strict-mcp-config",
            "--mcp-config", str(mcp_config_path),
            "--allowedTools", ",".join(self._allowed_tools),
            "--tools", ",".join(CLAUDE_BUILTIN_TOOLS),
            "--max-turns", str(mission.max_turns),
            "--model", self._model,
        ]

    def _build_env(self, mission: Mission) -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self._workdir / "home"),
            "TMPDIR": str(self._workdir / "tmp"),
            "CLAUDE_CONFIG_DIR": str(self._workdir / "cfg"),
            "PYTHONPATH": "",
            "PYTHONSAFEPATH": "1",
        }

    def _extract_output(self, stdout_lines: list[str], workdir: Path) -> dict[str, Any] | None:
        last_result: dict | None = None
        for line in stdout_lines:
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                last_result = obj
        if last_result is None:
            return None
        if "structured_output" in last_result:
            out = last_result["structured_output"]
            return out if isinstance(out, dict) else None
        text = last_result.get("result")
        if isinstance(text, str):
            try:
                return parse_json_output(text)
            except ParseError:
                return None
        return None

    def _max_turns_semantics(self) -> Literal["passthrough", "ignored"]:
        return "passthrough"
