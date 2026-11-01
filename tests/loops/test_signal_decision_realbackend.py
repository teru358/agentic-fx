from __future__ import annotations

import json
import shutil
from pathlib import Path

import jsonschema
import pytest

from agentic_fx.config import load_settings
from agentic_fx.loops.signal_decision_contract import (
    DECISION_SCHEMA,
    Reject,
    SIGNAL_DECISION_TOOLS,
    parse_decision,
    verify_opposite_position,
)
from agentic_fx.runners.base import Mission
from agentic_fx.runners.claude_runner import ClaudeRunner
from agentic_fx.tools.registry import ToolRegistry

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "signal_decision_claude.json"
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _runner(tmp_path: Path) -> ClaudeRunner:
    settings = SETTINGS
    if settings.runner.trade.backend != "claude":
        pytest.skip("runner.trade.backend=claude が必要")
    bin_path = shutil.which(settings.runner.claude.bin)
    if bin_path is None:
        pytest.skip(f"claude CLI 未解決: {settings.runner.claude.bin}")
    credentials = Path(settings.runner.claude.credentials_file).expanduser()
    if not credentials.is_file():
        pytest.skip("claude credentials 不在")

    workdir = tmp_path / "claude-work"
    config_dir = workdir / "cfg"
    spool = tmp_path / "mcp-spool"
    config_dir.mkdir(parents=True)
    spool.mkdir()
    shutil.copy2(credentials, config_dir / ".credentials.json")
    runner = ClaudeRunner(
        bin_path=Path(bin_path), model=settings.runner.trade.model,
        workdir=workdir, credentials_file_copied=True,
        allowed_tools=[f"mcp__afx__{name}" for name in SIGNAL_DECISION_TOOLS],
        cli_terminate_grace_sec=settings.runner.cli_terminate_grace_sec,
        registry=ToolRegistry())
    runner.mcp_spool_dir = spool
    return runner


@pytest.mark.realbackend
def test_signal_decision_claude_accept_and_opposite_position_reject(tmp_path):
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    runner = _runner(tmp_path)
    missions = {
        "accept": Mission(
            prompt=("Strategy signal USDJPY long is executable. Return only the "
                    "schema response that accepts it. Do not use tools."),
            tools=list(SIGNAL_DECISION_TOOLS), output_schema=DECISION_SCHEMA,
            max_turns=3, timeout_sec=180.0),
        "reject": Mission(
            prompt=("Strategy signal USDJPY long is executable. A USDJPY short "
                    "order is OPEN, so return only the schema response rejecting "
                    "it with opposite_position. Do not use tools."),
            tools=list(SIGNAL_DECISION_TOOLS), output_schema=DECISION_SCHEMA,
            max_turns=3, timeout_sec=180.0),
    }
    results = {}
    for name, mission in missions.items():
        argv = runner._build_argv(mission, mcp_spool=runner.mcp_spool_dir)
        assert "--json-schema" in argv
        assert json.loads(argv[argv.index("--json-schema") + 1]) == DECISION_SCHEMA
        result = runner.run(mission)
        assert result.status == "completed", result.reason
        assert result.output is not None
        jsonschema.validate(result.output, DECISION_SCHEMA)
        results[name] = result.output

    output_path = tmp_path / "signal_decision_claude.json"
    output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    assert set(results) == {"accept", "reject"}
    assert results == expected
    reject = parse_decision(results["reject"])
    assert isinstance(reject, Reject)
    assert verify_opposite_position(
        Reject(reject.code, pair="USDJPY", direction="long"),
        [{"pair": "USDJPY", "direction": "short", "status": "open"}],
    )
