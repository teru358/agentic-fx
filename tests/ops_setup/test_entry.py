from __future__ import annotations

import json

import pytest

from agentic_fx.entry import main
from agentic_fx.ops.keys import KeySetError
from agentic_fx.ops_setup.stages import InvalidState, StageResult
from agentic_fx.ops_setup.systemd import SystemdError


def test_ops_enable_dispatches_feature_without_starting_daemon(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []

    def enable(self, feature: str) -> StageResult:
        seen.append(feature)
        return StageResult(feature, "enable", "enabled")

    monkeypatch.setattr("agentic_fx.ops_setup.stages.OpsSetup.enable", enable)

    assert main(["ops", "enable", "bot"]) == 0
    assert seen == ["bot"]
    assert "bot: enabled" in capsys.readouterr().out
    assert not (tmp_path / "data" / "instance.lock").exists()


def test_ops_status_reports_saved_operation_stage_and_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    stage_dir = tmp_path / "data" / "ops"
    stage_dir.mkdir(parents=True)
    (stage_dir / "bot.stage.json").write_text(json.dumps({
        "feature": "bot", "operation": "enable", "stage": "installed",
        "failure": "ops_not_ready",
    }))
    asked: list[tuple[str, tuple[str, ...]]] = []

    def show(self, unit: str, *props: str) -> dict[str, str]:
        asked.append((unit, props))
        return {"ActiveState": "activating", "Result": "exit-code"}

    monkeypatch.setattr("agentic_fx.ops_setup.systemd.Systemd.show", show)

    assert main(["ops", "status", "bot"]) == 0

    assert capsys.readouterr().out.strip() == (
        "bot: enable installed (ops_not_ready) unit=activating result=exit-code")
    assert asked == [("afx-ops@bot.service", ("ActiveState", "Result"))]


@pytest.mark.parametrize("feature", ["bot", "web", "notify"])
@pytest.mark.parametrize("action", ["enable", "disable"])
def test_ops_dispatches_each_feature_and_action(tmp_path, monkeypatch, capsys, feature, action):
    monkeypatch.chdir(tmp_path)
    seen: list[tuple[str, str]] = []

    def run(self, name: str) -> StageResult:
        seen.append((action, name))
        return StageResult(name, action, action + "d")

    monkeypatch.setattr(f"agentic_fx.ops_setup.stages.OpsSetup.{action}", run)

    assert main(["ops", action, feature]) == 0
    assert seen == [(action, feature)]
    assert capsys.readouterr().out == f"{feature}: {action}d\n"


@pytest.mark.parametrize("error, reason", [
    (InvalidState("invalid_state"), "invalid_state"),
    (SystemdError("afx_still_running"), "afx_still_running"),
    (KeySetError("token_dir_in_sandbox"), "token_dir_in_sandbox"),
])
def test_ops_failure_prints_fixed_reason_and_exits_1(tmp_path, monkeypatch, capsys, error, reason):
    monkeypatch.chdir(tmp_path)

    def enable(self, feature: str) -> StageResult:
        raise error

    monkeypatch.setattr("agentic_fx.ops_setup.stages.OpsSetup.enable", enable)

    assert main(["ops", "enable", "bot"]) == 1
    captured = capsys.readouterr()
    assert captured.err == f"afx ops: {reason}\n"
    assert captured.out == ""


def test_ops_failure_prints_detail_after_the_fixed_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def enable(self, feature: str) -> StageResult:
        raise SystemdError("start", "rc=5 stderr=Unit afx.service not found.")

    monkeypatch.setattr("agentic_fx.ops_setup.stages.OpsSetup.enable", enable)

    assert main(["ops", "enable", "bot"]) == 1
    assert capsys.readouterr().err == "afx ops: start (rc=5 stderr=Unit afx.service not found.)\n"


def test_ops_status_without_systemctl_reports_unknown_unit(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    assert main(["ops", "status", "bot"]) == 0

    assert capsys.readouterr().out == "bot: disable disabled unit=unknown result=unknown\n"


def test_ops_marker_rejection_prints_owner_and_recovery(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def enable(self, feature: str) -> StageResult:
        raise InvalidState("marker_foreign", "path=/x feature=web")

    monkeypatch.setattr("agentic_fx.ops_setup.stages.OpsSetup.enable", enable)

    assert main(["ops", "enable", "bot"]) == 1
    assert capsys.readouterr().err == "afx ops: marker_foreign (path=/x feature=web)\n"
