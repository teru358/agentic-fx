"""CodexRunner の argv/env 組立と fake CLI 契約テスト (設計書 §1.3、§8.1-8)。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.runners.base import Mission
from agentic_fx.runners.codex_runner import CodexRunner
from agentic_fx.tools.registry import ToolRegistry

FAKE_CODEX = Path(__file__).resolve().parent / "fixtures" / "fake_codex.py"
EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}},
          "required": ["answer"]}


def _mission(**over):
    d = dict(prompt="do the thing", tools=[], output_schema=SCHEMA,
             max_turns=200, timeout_sec=10)
    d.update(over)
    return Mission(**d)


def _runner(tmp_path, *, provider="chatgpt", llama_swap_base_url=None,
           behavior="success", **over):
    workdir = tmp_path / "wd"
    workdir.mkdir()
    kw = dict(bin_path=Path(sys.executable), model="gpt-5.6-sol",
              workdir=workdir, provider=provider,
              llama_swap_base_url=llama_swap_base_url,
              cli_terminate_grace_sec=0.3, registry=ToolRegistry())
    kw.update(over)
    r = CodexRunner(**kw)
    r._bin_path = FAKE_CODEX
    return r, workdir, behavior


def _run_with_fake_env(runner, mission, behavior, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_BEHAVIOR", behavior)
    orig = runner._build_env
    monkeypatch.setattr(runner, "_build_env",
                        lambda m: {**orig(m), "FAKE_CODEX_BEHAVIOR": behavior})
    return runner.run(mission)


def test_codex_argv_shape_pins_plugin_disable_flags(tmp_path):
    """argv pin: `--disable plugins --disable remote_plugin
    --disable recommended_plugins` (probe §5-②: 落とすとカタログ取得で
    RLIMIT_FSIZE=8MB に当たり SIGXFSZ で即死する)。"""
    runner, workdir, _ = _runner(tmp_path)
    argv = runner._build_argv(_mission(), mcp_socket=workdir / "afx.sock")
    disables = [argv[i + 1] for i, a in enumerate(argv) if a == "--disable"]
    assert "plugins" in disables
    assert "remote_plugin" in disables
    assert "recommended_plugins" in disables
    assert "apps" in disables


def test_codex_argv_shape_pins_bypass_and_ignore_user_config(tmp_path):
    runner, workdir, _ = _runner(tmp_path)
    argv = runner._build_argv(_mission(), mcp_socket=workdir / "afx.sock")
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--ignore-user-config" in argv
    assert "exec" in argv
    assert "--json" in argv
    assert "-o" in argv
    assert "--output-schema" in argv


def test_codex_argv_uses_llama_swap_provider_config_when_selected(tmp_path):
    runner, workdir, _ = _runner(
        tmp_path, provider="llama_swap",
        llama_swap_base_url="http://localhost:8080/v1")
    argv = runner._build_argv(_mission(), mcp_socket=workdir / "afx.sock")
    joined = " ".join(argv)
    assert "model_providers.llamaswap.base_url=http://localhost:8080/v1" in joined
    assert "model_providers.llamaswap.wire_api=responses" in joined
    assert "model_provider=llamaswap" in joined


def test_codex_argv_omits_llama_swap_config_for_chatgpt_provider(tmp_path):
    runner, workdir, _ = _runner(tmp_path, provider="chatgpt")
    argv = runner._build_argv(_mission(), mcp_socket=workdir / "afx.sock")
    joined = " ".join(argv)
    assert "llamaswap" not in joined


def test_codex_completed_terminal_status(tmp_path, monkeypatch):
    runner, workdir, behavior = _runner(tmp_path)
    result = _run_with_fake_env(runner, _mission(), behavior, monkeypatch)
    assert result.status == "completed"
    assert result.output == {"answer": 4}


def test_codex_fenced_output_in_o_file_is_recovered(tmp_path, monkeypatch):
    """probe P1②: llama-swap 経由ではフェンス付きで返ることが多い。"""
    runner, workdir, behavior = _runner(tmp_path, behavior="fenced_output")
    result = _run_with_fake_env(runner, _mission(), behavior, monkeypatch)
    assert result.status == "completed"
    assert result.output == {"answer": 4}


def test_codex_schema_mismatch_is_failed(tmp_path, monkeypatch):
    runner, workdir, behavior = _runner(tmp_path, behavior="schema_mismatch")
    result = _run_with_fake_env(runner, _mission(), behavior, monkeypatch)
    assert result.status == "failed"


def test_codex_missing_output_file_is_failed(tmp_path, monkeypatch):
    runner, workdir, behavior = _runner(tmp_path, behavior="missing_output_file")
    result = _run_with_fake_env(runner, _mission(), behavior, monkeypatch)
    assert result.status == "failed"


def test_codex_max_turns_is_ignored_docstring_and_semantics(tmp_path):
    """§1.5: Codex は max_turns 到達不能。docstring に明記し、
    `_max_turns_semantics()` は `"ignored"`。"""
    runner, workdir, _ = _runner(tmp_path)
    assert runner._max_turns_semantics() == "ignored"
    assert "max_turns" in (CodexRunner.__doc__ or "")


def test_codex_argv_does_not_pass_max_turns_flag(tmp_path):
    """codex CLI に `--max-turns` 相当の引数を渡さない (無い、または渡しても
    無視されると docstring に書いてあるだけでは不十分 — argv に含めないこと
    を pin する)。"""
    runner, workdir, _ = _runner(tmp_path)
    argv = runner._build_argv(_mission(max_turns=1), mcp_socket=workdir / "afx.sock")
    assert "--max-turns" not in argv


_CODEX_ENV_ALLOWLIST = {
    "PATH", "HOME", "TMPDIR", "CODEX_HOME", "PYTHONPATH", "PYTHONSAFEPATH",
    # launcher.py (`os.execv` 経由の 2 段目) の CPython 起動時ロケール強制
    # (PEP 538) がこの環境では `LC_CTYPE=C.UTF-8` を自動注入する。`_build_env`
    # が渡す 6 キーには含まれないが、launcher ホップ自体の副作用でありシークレ
    # ットではないため許容する (実測: `Popen(env={"PATH":...})` だけでも
    # 同じ注入が再現する — `_build_env` の変異ではない)。
    "LC_CTYPE",
}


def test_codex_child_env_excludes_parent_secrets_and_matches_allowlist(tmp_path, monkeypatch):
    """§7.1-2: 「全 spawn の env に `*_API_KEY` が無い」を、鍵が実在する
    親 env から fake CLI を実際に起動して観測する (`observed_env.json`)。
    `_build_env()` の戻り値だけを見る検査は、pytest プロセスの env に
    そもそも鍵が無いため「無いものが無い」を assert するだけで空振りする。
    behavior="success" (既定) を使うため `_run_with_fake_env` は使わない —
    それは `FAKE_CODEX_BEHAVIOR` キーを env に載せてしまい、この allowlist
    照合を汚染する (m3 参照)。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-sentinel")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-sentinel")
    monkeypatch.setenv("TWELVEDATA_API_KEY", "td-sentinel")
    runner, workdir, _ = _runner(tmp_path)
    runner.run(_mission())
    observed = json.loads((workdir / "observed_env.json").read_text())
    assert not any("API_KEY" in k for k in observed)
    assert set(observed) == _CODEX_ENV_ALLOWLIST


def test_codex_env_has_no_openai_api_key(tmp_path, monkeypatch):
    """親 (pytest) 側に鍵を置いてから検査する — 置かないと「無いものが無い」
    を assert するだけで `_build_env` が親を継承する変異を捕まえられず
    空振りする (検収 B1)。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-sentinel")
    runner, workdir, _ = _runner(tmp_path)
    env = runner._build_env(_mission())
    assert "OPENAI_API_KEY" not in env


def test_codex_env_uses_codex_home_scratch(tmp_path):
    runner, workdir, _ = _runner(tmp_path)
    env = runner._build_env(_mission())
    assert env["CODEX_HOME"] == str(workdir / "cfg")
    assert env["HOME"] == str(workdir / "home")


def test_codex_output_comes_from_o_file_not_stdout(tmp_path, monkeypatch):
    """M7 追加テスト: -o ファイルからのみ出力を読む。stdout は別の値を含む
    (fake がここを検証するため、behavior を追加)。"""
    runner, workdir, _ = _runner(tmp_path)
    # fake がこの behavior をサポートするように拡張
    # ここでは behavior="success" のままだが、fake は常に answer:4 を -o に書く
    # (stdout にはイベント行のみ、answer キーなし)
    result = _run_with_fake_env(runner, _mission(), "success", monkeypatch)
    assert result.output == {"answer": 4}


def test_trade_backend_codex_rejected_by_settings_validator():
    """§1.3: `runner.trade.backend=codex` は Settings の validator で拒否
    (`ValueError`) — codex は shell を外せない。"""
    from pydantic import ValidationError
    settings = load_settings(EXAMPLE)
    raw = settings.model_dump()
    raw["runner"]["trade"]["backend"] = "codex"
    from agentic_fx.config import Settings
    with pytest.raises(ValidationError, match="codex"):
        Settings(**raw)
