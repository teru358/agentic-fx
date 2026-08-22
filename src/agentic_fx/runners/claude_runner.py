"""ClaudeRunner — claude CLI 直駆動 (設計書 §1.2)。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Literal

from agentic_fx.runners.base import Mission
from agentic_fx.runners.cli_runner import CliRunner
from agentic_fx.runners.response_parser import ParseError, parse_json_output
from agentic_fx.tools.registry import ToolRegistry


# precheck 2026-08-22 pass2: RB3 追随 (build_runner から cli_started_sink= を
# 透過するため、__init__ シグネチャと super().__init__() 呼び出しに追加)
class ClaudeRunner(CliRunner):
    def __init__(self, *, bin_path: Path, model: str, workdir: Path,
                 credentials_file_copied: bool,
                 allowed_tools: list[str],
                 cli_terminate_grace_sec: float,
                 registry: ToolRegistry,
                 on_message: Callable[[dict], None] | None = None,
                 cli_started_sink: Callable[[int], None] | None = None) -> None:
        self._allowed_tools = allowed_tools
        super().__init__(bin_path=bin_path, model=model, workdir=workdir,
                         cli_terminate_grace_sec=cli_terminate_grace_sec,
                         registry=registry, on_message=on_message,
                         cli_started_sink=cli_started_sink)

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
