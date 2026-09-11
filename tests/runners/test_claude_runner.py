"""ClaudeRunner の argv/env 組立と fake CLI 契約テスト (設計書 §1.2、§8.1-8)。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agentic_fx.runners.base import Mission
from agentic_fx.runners.claude_runner import (CLAUDE_DISALLOWED_BUILTIN_TOOLS,
                                              ClaudeRunner)
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


def _flag_value(argv, flag):
    """`flag` の直後の要素を返す (隣接値の pin 用)。存在しなければ AssertionError。"""
    idx = argv.index(flag)
    assert idx + 1 < len(argv), f"{flag} has no adjacent value in argv"
    return argv[idx + 1]


def test_claude_runner_rejects_empty_allowed_tools(tmp_path):
    """#26 (`verified-round1.md` 1-B): `allowed_tools=[]` は
    `--allowedTools ""` になり、claude CLI の意味論では「制限なし」に
    近い挙動になりうる — 構築時点で fail closed する。"""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    with pytest.raises(ValueError, match="allowed_tools"):
        ClaudeRunner(bin_path=FAKE_CLAUDE, model="claude-haiku-4-5",
                    workdir=workdir, credentials_file_copied=True,
                    allowed_tools=[], cli_terminate_grace_sec=0.3,
                    registry=ToolRegistry())


def test_claude_argv_shape(tmp_path):
    """argv が骨格 §1.2 の形と一致する (`-p` プロンプト・--output-format
    stream-json --verbose・--json-schema・--setting-sources ""・
    --strict-mcp-config・--mcp-config・--allowedTools・--max-turns・--model)。
    各フラグは値だけを落とす変異 (例: `--mcp-config` の値を消し、次のフラグが
    値として食われる形) を殺すため、メンバシップではなく隣接値まで pin する。"""
    mission = _mission()
    runner, workdir = _runner(tmp_path)
    mcp_socket = workdir / "afx.sock"
    argv = runner._build_argv(mission, mcp_socket=mcp_socket)
    mcp_config_path = workdir / "mcp.json"

    assert argv[0] == str(runner._bin_path)
    assert argv[1] == "-p"
    assert argv[2] == mission.prompt

    assert "--output-format" in argv
    assert _flag_value(argv, "--output-format") == "stream-json"
    assert "--verbose" in argv
    assert _flag_value(argv, "--json-schema") == json.dumps(mission.output_schema)
    assert _flag_value(argv, "--setting-sources") == ""
    assert "--strict-mcp-config" in argv
    assert _flag_value(argv, "--mcp-config") == str(mcp_config_path)
    assert _flag_value(argv, "--allowedTools") == ",".join(runner._allowed_tools)
    assert _flag_value(argv, "--disallowedTools") == ",".join(
        CLAUDE_DISALLOWED_BUILTIN_TOOLS)
    assert _flag_value(argv, "--max-turns") == str(mission.max_turns)
    assert _flag_value(argv, "--model") == runner._model
    # #34 (`verified-round1.md` 1-B): 各フラグの隣接値は pin 済みだが、
    # argv の長さ (= 余分なフラグが無いこと) は未検査だった。
    assert len(argv) == 21, (
        f"argv に既知フラグ以外の要素が混入している (len={len(argv)}): {argv!r}")


def test_claude_argv_disallows_builtin_tools_but_keeps_read_and_output(tmp_path):
    """[claude-builtin-tools-exposed] 是正 (A4 10 回目 claude #69 観測 C):
    `--disallowedTools` は 1 回だけ現れ、外向き経路 (Bash/WebFetch/
    SendMessage) を遮断リストへ含む一方、`mcp__afx__*` (registry 経由の
    afx ツール) は含まない。`Read`/`ToolSearch`/`StructuredOutput` は
    keep 側 — 遮断すると workdir 内の無害な読み取りや最終出力の抽出
    (`_extract_output` は `structured_output` を読む) が壊れるため、
    keep 側も明示的に pin する (`ToolSearch`/`StructuredOutput` を
    遮断リストへ足す変異を殺す)。"""
    mission = _mission()
    runner, workdir = _runner(tmp_path)
    argv = runner._build_argv(mission, mcp_socket=workdir / "afx.sock")

    assert argv.count("--disallowedTools") == 1
    disallowed = _flag_value(argv, "--disallowedTools")
    assert "Bash" in disallowed
    assert "WebFetch" in disallowed
    assert "SendMessage" in disallowed
    assert "mcp__afx__" not in disallowed
    for kept in ("Read", "ToolSearch", "StructuredOutput"):
        assert kept not in disallowed.split(","), (
            f"{kept} must stay usable — it must not appear in "
            f"--disallowedTools: {disallowed!r}")


def test_claude_argv_does_not_leak_parent_secrets(tmp_path, monkeypatch):
    """#30 (`verified-round1.md` 1-B): argv 全体への秘密混入検査が無い —
    `_build_argv` は `mission.prompt`/`allowed_tools`/`model` しか触らない
    はずだが、それを pin するテストが無かった。親 (pytest) 側の秘密が
    argv に混入していないことを、fake CLI が書く `observed_argv.json` で
    実プロセス実行して確認する (§4「安価な観測点が既にある」の消化)。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-sentinel-argv")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-sentinel-argv")
    runner, workdir = _runner(tmp_path)
    runner.run(_mission())
    observed_argv = json.loads((workdir / "observed_argv.json").read_text())
    joined = " ".join(observed_argv)
    assert "sk-ant-sentinel-argv" not in joined
    assert "sk-sentinel-argv" not in joined


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


def test_claude_env_has_no_anthropic_api_key(tmp_path, monkeypatch):
    """§1.1-3: 課金鍵は子 env に決して入れない。親 (pytest) 側に鍵を置いて
    から検査する — 置かないと「無いものが無い」を assert するだけで
    `_build_env` が親を継承する変異を捕まえられず空振りする (検収 B1)。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-sentinel")
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


_CLAUDE_ENV_ALLOWLIST = {
    "PATH", "HOME", "TMPDIR", "CLAUDE_CONFIG_DIR", "PYTHONPATH", "PYTHONSAFEPATH",
    # launcher.py (`os.execv` 経由の 2 段目) の CPython 起動時ロケール強制
    # (PEP 538) がこの環境では `LC_CTYPE=C.UTF-8` を自動注入する。`_build_env`
    # が渡す 6 キーには含まれないが、launcher ホップ自体の副作用でありシークレ
    # ットではないため許容する (実測: `Popen(env={"PATH":...})` だけでも
    # 同じ注入が再現する — `_build_env` の変異ではない)。
    "LC_CTYPE",
}


def test_claude_child_env_excludes_parent_secrets_and_matches_allowlist(tmp_path, monkeypatch):
    """§7.1-2: 「全 spawn の env に `*_API_KEY` が無い」を、鍵が実在する
    親 env から fake CLI を実際に起動して観測する (`observed_env.json`)。
    `_build_env()` の戻り値だけを見る検査は、pytest プロセスの env に
    そもそも鍵が無いため「無いものが無い」を assert するだけで空振りする。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-sentinel")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-sentinel")
    monkeypatch.setenv("TWELVEDATA_API_KEY", "td-sentinel")
    runner, workdir = _runner(tmp_path)
    runner.run(_mission())
    observed = json.loads((workdir / "observed_env.json").read_text())
    assert not any("API_KEY" in k for k in observed)
    assert set(observed) == _CLAUDE_ENV_ALLOWLIST
    # #32 (`verified-round1.md` 1-B): キー名集合の一致だけでは PATH/TMPDIR/
    # PYTHONPATH/PYTHONSAFEPATH の**値**が未 pin。
    assert observed["PATH"] == "/usr/bin:/bin"
    assert observed["TMPDIR"] == str(workdir / "tmp")
    assert observed["PYTHONPATH"] == ""
    assert observed["PYTHONSAFEPATH"] == "1"


def test_claude_reason_does_not_leak_stderr_body(tmp_path):
    """§1.1-7: CLI の stderr は要約 (先頭行+終了コード) のみ。"""
    runner, workdir = _runner(tmp_path, behavior="nonzero_exit")
    result = runner.run(_mission())
    assert len(result.reason) <= 500
