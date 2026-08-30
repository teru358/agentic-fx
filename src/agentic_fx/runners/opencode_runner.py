"""OpencodeRunner — 隔離した opencode CLI で improve を実行する。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Literal

from agentic_fx.runners.base import Mission
from agentic_fx.runners.cli_runner import CliRunner
from agentic_fx.runners.response_parser import ParseError, parse_json_output
from agentic_fx.tools.registry import ToolRegistry


class OpencodeRunner(CliRunner):
    """improve 専用。CLI に上限指定がないため max_turns は無視する。"""
    def __init__(self, *, bin_path: Path, model: str, workdir: Path,
                 llama_swap_base_url: str, cli_terminate_grace_sec: float,
                 registry: ToolRegistry,
                 on_message: Callable[[dict], None] | None = None,
                 cli_started_sink: Callable[[int], None] | None = None) -> None:
        self._llama_swap_base_url = llama_swap_base_url
        super().__init__(bin_path=bin_path, model=model, workdir=workdir,
                         cli_terminate_grace_sec=cli_terminate_grace_sec,
                         registry=registry, on_message=on_message,
                         cli_started_sink=cli_started_sink)

    def _build_argv(self, mission: Mission, *, mcp_socket: Path) -> list[str]:
        path = self._workdir / "home/.config/opencode/opencode.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"provider": {"llama-swap": {
            "npm": "@ai-sdk/openai-compatible", "name": "llama-swap",
            "options": {"baseURL": self._llama_swap_base_url},
            "models": {self._model: {"name": self._model, "limit": {"context": 131072, "output": 8192}}}}},
            "mcp": {"afx": {"type": "local", "command": [sys.executable, "-m", "agentic_fx.tools.mcp_shim", str(mcp_socket)], "enabled": True}},
            # 組み込み tool は afx MCP 以外の全部を無効化 (実測: probe で bash が
            # tool_use イベント自体を出さなくなることを確認済み)。MCP 側は
            # "<server>_<tool>" (例: afx_list_staging) の別名前空間のため巻き込まれない。
            "tools": {"bash": False, "read": False, "write": False, "edit": False, "patch": False,
                      "glob": False, "grep": False, "list": False, "webfetch": False,
                      "websearch": False, "task": False, "todowrite": False, "question": False,
                      "skill": False}}), encoding="utf-8")
        return [str(self._bin_path), "run", mission.prompt, "--format", "json", "--pure", "-m", f"llama-swap/{self._model}", "--dir", str(self._workdir)]

    def _build_env(self, mission: Mission) -> dict[str, str]:
        return {"PATH": "/usr/bin:/bin", "HOME": str(self._workdir / "home"), "TMPDIR": str(self._workdir / "tmp"), "TERM": "dumb", "PYTHONPATH": "", "PYTHONSAFEPATH": "1"}

    def _extract_output(self, stdout_lines: list[str], workdir: Path) -> dict[str, Any] | None:
        text = None
        for line in stdout_lines:
            try: event = json.loads(line)
            except (json.JSONDecodeError, ValueError): continue
            part = event.get("part")
            if event.get("type") == "text" and isinstance(part, dict) and isinstance(part.get("text"), str): text = part["text"]
        if text is None: return None
        try: return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            try: return parse_json_output(text)
            except ParseError: return None

    def _max_turns_semantics(self) -> Literal["passthrough", "ignored"]: return "ignored"
