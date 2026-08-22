"""ClaudeRunner の argv/env 組立と fake CLI 契約テスト (設計書 §1.2、§8.1-8)。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agentic_fx.runners.base import Mission
from agentic_fx.runners.claude_runner import ClaudeRunner
from agentic_fx.tools.registry import ToolRegistry

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude.py"
SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}},
          "required": ["answer"]}


def _mission(**over):
    d = dict(prompt="do the thing", tools=[], output_schema=SCHEMA,
             max_turns=8, timeout_sec=10)
    d.update(over)
    return Mission(**d)


def _runner(tmp_path, *, allowed_tools=None, behavior="success", **over):
    workdir = tmp_path / "wd"
    workdir.mkdir()

    # Write behavior marker file instead of env var
    if behavior != "success":
        (workdir / "_fake_claude_behavior").write_text(behavior)

    kw = dict(bin_path=FAKE_CLAUDE, model="claude-haiku-4-5",
              workdir=workdir, credentials_file_copied=True,
              allowed_tools=allowed_tools or ["mcp__afx__*", "Bash", "Read",
                                              "Write", "Edit", "Glob", "Grep"],
              cli_terminate_grace_sec=0.3, registry=ToolRegistry())
    kw.update(over)
    r = ClaudeRunner(**kw)
    return r, workdir


def test_claude_argv_shape(tmp_path):
    """argv が骨格 §1.2 の形と一致する (`-p` プロンプト・--output-format
    stream-json --verbose・--json-schema・--setting-sources ""・
    --strict-mcp-config・--mcp-config・--allowedTools・--max-turns・--model)。"""
    runner, workdir = _runner(tmp_path)
    argv = runner._build_argv(_mission(), mcp_socket=workdir / "afx.sock")
    assert "--output-format" in argv and "stream-json" in argv
    assert "--verbose" in argv
    assert "--json-schema" in argv
    assert "--setting-sources" in argv
    idx = argv.index("--setting-sources")
    assert argv[idx + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" in argv
    assert "--allowedTools" in argv
    assert "--max-turns" in argv
    assert str(_mission().max_turns) in argv
    assert "--model" in argv


def test_claude_completed_terminal_status(tmp_path):
    runner, workdir = _runner(tmp_path)
    result = runner.run(_mission())
    assert result.status == "completed"
    assert result.output == {"answer": 4}


def test_claude_fenced_output_is_recovered(tmp_path):
    """probe §5-⑤: schema 準拠は provider 依存。フェンス剥がしで吸収する。"""
    runner, workdir = _runner(tmp_path, behavior="fenced_output")
    result = runner.run(_mission())
    assert result.status == "completed"
    assert result.output == {"answer": 4}


def test_claude_schema_mismatch_is_failed(tmp_path):
    runner, workdir = _runner(tmp_path, behavior="schema_mismatch")
    result = runner.run(_mission())
    assert result.status == "failed"
    assert result.reason is not None


def test_claude_nonzero_exit_is_failed_with_reason(tmp_path):
    runner, workdir = _runner(tmp_path, behavior="nonzero_exit")
    result = runner.run(_mission())
    assert result.status == "failed"
    assert "\n" not in result.reason


def test_claude_ignoring_sigterm_reaches_timeout(tmp_path):
    runner, workdir = _runner(tmp_path, behavior="hang_then_ignore_sigterm")
    result = runner.run(_mission(timeout_sec=0.3))
    assert result.status == "timeout"


def test_claude_max_turns_semantics_is_passthrough(tmp_path):
    """§1.5: Claude の `--max-turns` は mission.max_turns をそのまま渡す
    (到達不能ではない — Codex と対照させる)。"""
    runner, workdir = _runner(tmp_path)
    assert runner._max_turns_semantics() == "passthrough"


def test_claude_env_has_no_anthropic_api_key(tmp_path):
    """§1.1-3: 課金鍵は子 env に決して入れない。"""
    runner, workdir = _runner(tmp_path)
    env = runner._build_env(_mission())
    assert "ANTHROPIC_API_KEY" not in env


def test_claude_env_uses_scratch_home_and_config_dir(tmp_path):
    """§1.1-3: HOME/CLAUDE_CONFIG_DIR は workdir/home, workdir/cfg。"""
    runner, workdir = _runner(tmp_path)
    env = runner._build_env(_mission())
    assert env["HOME"] == str(workdir / "home")
    assert env["CLAUDE_CONFIG_DIR"] == str(workdir / "cfg")
    assert env["TMPDIR"] == str(workdir / "tmp")


def test_claude_trade_profile_allowed_tools_excludes_bash(tmp_path):
    """§1.6: trade + claude は `mcp__afx__*` のみ。`Bash`/`Write` を含まない。"""
    runner, workdir = _runner(tmp_path, allowed_tools=["mcp__afx__*"])
    argv = runner._build_argv(_mission(), mcp_socket=workdir / "afx.sock")
    idx = argv.index("--allowedTools")
    allowed = argv[idx + 1]
    assert "Bash" not in allowed
    assert "Write" not in allowed
    assert "mcp__afx__*" in allowed


def test_claude_reason_does_not_leak_stderr_body(tmp_path):
    """§1.1-7: CLI の stderr は要約 (先頭行+終了コード) のみ。"""
    runner, workdir = _runner(tmp_path, behavior="nonzero_exit")
    result = runner.run(_mission())
    assert len(result.reason) <= 500
