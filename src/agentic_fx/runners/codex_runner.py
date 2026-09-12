"""CodexRunner — codex CLI (vendor native) 直駆動、improve 専用 (設計書 §1.3)。

`max_turns` は到達不能: codex CLI に上限を渡す手段が無く、timeout_sec の
みが唯一の上限になる。`runner.trade.backend=codex` は `Settings` の
`_trade_backend_not_codex` バリデータが拒否する (codex は shell を外せず、
trade worker は Landlock 無し + データ資格情報を持つため)。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable, Literal

from agentic_fx.runners.base import Mission
from agentic_fx.runners.cli_runner import CliRunner
from agentic_fx.runners.response_parser import ParseError, parse_json_output
from agentic_fx.tools.registry import ToolRegistry


class CodexRunner(CliRunner):
    """improve 専用。`max_turns` は無視 (到達不能) — timeout_sec のみが
    唯一の上限になる (probe §5-④「claude の structured output は 1 ターン
    では終わらない」と対照的に、codex はそもそもターン上限の概念を CLI に
    渡す手段が無い)。"""

    # precheck 2026-08-22 pass2: RB3 追随 (build_runner から cli_started_sink=
    # を透過するため、__init__ シグネチャと super().__init__() 呼び出しに追加)
    def __init__(self, *, bin_path: Path, model: str, workdir: Path,
                 provider: Literal["chatgpt"],
                 cli_terminate_grace_sec: float,
                 registry: ToolRegistry,
                 on_message: Callable[[dict], None] | None = None,
                 cli_started_sink: Callable[[int], None] | None = None,
                 abort_event: threading.Event | None = None,
                 abort_reason_fn: Callable[[], str] | None = None) -> None:
        self._provider = provider
        super().__init__(bin_path=bin_path, model=model, workdir=workdir,
                         cli_terminate_grace_sec=cli_terminate_grace_sec,
                         registry=registry, on_message=on_message,
                         cli_started_sink=cli_started_sink,
                         abort_event=abort_event,
                         abort_reason_fn=abort_reason_fn)

    def _build_argv(self, mission: Mission, *, mcp_socket: Path) -> list[str]:
        import sys as _sys
        schema_path = self._workdir / "schema.json"
        schema_path.write_text(json.dumps(mission.output_schema))
        out_path = self._workdir / "output.txt"
        argv = [
            # 設計書 §D: codex `exec [PROMPT]` は「引数無し、または `-`
            # なら stdin から読む」(指揮者の CLI 事前確認)。引数を単に
            # 省くと後続の argv 要素が誤って PROMPT 位置引数と解釈され
            # かねないため、`-` を明示して stdin 読みだと確定させる。
            str(self._bin_path), "exec", "-",
            "--json",
            "--output-schema", str(schema_path), "-o", str(out_path),
            "--ignore-user-config",
            "--dangerously-bypass-approvals-and-sandbox",
            "--disable", "plugins",
            "--disable", "remote_plugin",
            "--disable", "recommended_plugins",
            "--disable", "apps",
            "-c", f"mcp_servers.afx.command={_sys.executable}",
            "-c", "mcp_servers.afx.args=[\"-m\",\"agentic_fx.tools.mcp_shim\","
                  f"\"{mcp_socket}\"]",
        ]
        argv += ["-m", self._model]
        return argv

    def _build_env(self, mission: Mission) -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self._workdir / "home"),
            "TMPDIR": str(self._workdir / "tmp"),
            "CODEX_HOME": str(self._workdir / "cfg"),
            "PYTHONPATH": "",
            "PYTHONSAFEPATH": "1",
        }

    def _extract_output(self, stdout_lines: list[str], workdir: Path) -> dict[str, Any] | None:
        out_path = workdir / "output.txt"
        if not out_path.is_file():
            return None
        text = out_path.read_text().strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass
        try:
            return parse_json_output(text)
        except ParseError:
            return None

    def _max_turns_semantics(self) -> Literal["passthrough", "ignored"]:
        return "ignored"

    def _stdin_prompt(self, mission: Mission) -> str | None:
        # [mission-prompt-in-argv-readable-via-proc] 是正 (設計書 §D):
        # `_build_argv` はもう `mission.prompt` を argv に積まない
        # (`exec -` で stdin 読みを明示) — `/proc/<pid>/cmdline` から
        # 読めなくするため、代わりにここで stdin (workdir/prompt.txt
        # 経由) に回す。
        return mission.prompt
