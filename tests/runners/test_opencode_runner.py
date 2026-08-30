"""OpencodeRunner の隔離 CLI 契約 pin。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from agentic_fx.runners.base import Mission
from agentic_fx.runners.opencode_runner import OpencodeRunner
from agentic_fx.tools.registry import ToolRegistry


def _mission() -> Mission:
    return Mission(prompt="reply", tools=[], output_schema={"type": "object"},
                   max_turns=1, timeout_sec=10)


def _runner(tmp_path):
    workdir = tmp_path / "wd"; workdir.mkdir()
    return OpencodeRunner(bin_path=Path("/opt/opencode"), model="qwen-test",
                          workdir=workdir,
                          llama_swap_base_url="http://localhost:8080/v1",
                          cli_terminate_grace_sec=1, registry=ToolRegistry()), workdir


def test_opencode_argv_writes_self_contained_mcp_provider_config(tmp_path):
    """mcp/provider 節を落とす変異では、隔離 HOME で tools も provider も
    見えず起動不能になるため、生成済み設定と argv の両方を pin する。"""
    runner, workdir = _runner(tmp_path)
    argv = runner._build_argv(_mission(), mcp_socket=workdir / "afx.sock")
    config = json.loads((workdir / "home/.config/opencode/opencode.json").read_text())
    assert argv == ["/opt/opencode", "run", "reply", "--format", "json", "--pure",
                    "-m", "llama-swap/qwen-test", "--dir", str(workdir)]
    assert config["provider"]["llama-swap"]["options"]["baseURL"] == "http://localhost:8080/v1"
    assert config["provider"]["llama-swap"]["models"]["qwen-test"]["limit"] == {"context": 131072, "output": 8192}
    assert config["mcp"]["afx"]["command"] == [sys.executable, "-m", "agentic_fx.tools.mcp_shim", str(workdir / "afx.sock")]


def test_opencode_env_is_minimal_and_output_is_last_text_json(tmp_path):
    """親 env 継承または最後の text 以外を採る変異を防ぐ。最後の assistant
    text が唯一の採用対象で、非 JSON は fail closed (None) にする。"""
    runner, workdir = _runner(tmp_path)
    assert runner._build_env(_mission()) == {
        "PATH": "/usr/bin:/bin", "HOME": str(workdir / "home"),
        "TMPDIR": str(workdir / "tmp"), "TERM": "dumb", "PYTHONPATH": "",
        "PYTHONSAFEPATH": "1"}
    assert runner._extract_output([
        '{"type":"text","part":{"type":"text","text":"{\\"old\\": 1}"}}',
        '{"type":"text","part":{"type":"text","text":"{\\"answer\\": 4}"}}'], workdir) == {"answer": 4}
    assert runner._extract_output(['{"type":"text","part":{"type":"text","text":"not json"}}'], workdir) is None
    assert runner._max_turns_semantics() == "ignored"


def test_opencode_extract_output_fails_closed_when_no_text_event(tmp_path):
    """検収変異 M3 (2026-08-30) killer: stdout に text イベントが 1 つも
    無いとき `_extract_output` は None (fail closed)。`{}` 等を返す変異は
    親の出力検証を素通りしかねない。"""
    from agentic_fx.runners.opencode_runner import OpencodeRunner
    from agentic_fx.tools.registry import ToolRegistry
    workdir = tmp_path / "wd"; workdir.mkdir()
    runner = OpencodeRunner(
        bin_path=Path("/usr/bin/true"), model="m", workdir=workdir,
        llama_swap_base_url="http://127.0.0.1:1/v1",
        cli_terminate_grace_sec=0.3, registry=ToolRegistry())
    lines = ['{"type": "step_start", "part": {"type": "step-start"}}',
             'not-json', '{"type": "step_finish"}']
    assert runner._extract_output(lines, workdir) is None
