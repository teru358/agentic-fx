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

    def enable(root, feature: str, **_kwargs):
        from agentic_fx.ops_setup.interactive import EnableResult
        seen.append(feature)
        return EnableResult(0, "enabled", "ok")

    monkeypatch.setattr("agentic_fx.ops_setup.interactive.interactive_enable", enable)

    assert main(["ops", "bot", "enable"]) == 0
    assert seen == ["bot"]
    assert capsys.readouterr().out == ""
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

    assert main(["ops", "bot", "status"]) == 0

    assert capsys.readouterr().out.strip() == (
        "bot: enable installed (ops_not_ready) unit=activating result=exit-code")
    assert asked == [("afx-ops@bot.service", ("ActiveState", "Result"))]


@pytest.mark.parametrize("feature", ["bot", "web", "notify"])
@pytest.mark.parametrize("action", ["disable"])
def test_ops_dispatches_each_feature_and_action(tmp_path, monkeypatch, capsys, feature, action):
    monkeypatch.chdir(tmp_path)
    seen: list[tuple[str, str]] = []

    def run(self, name: str) -> StageResult:
        seen.append((action, name))
        return StageResult(name, action, action + "d")

    monkeypatch.setattr(f"agentic_fx.ops_setup.stages.OpsSetup.{action}", run)

    assert main(["ops", feature, action]) == 0
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

    monkeypatch.setattr("agentic_fx.ops_setup.stages.OpsSetup.disable", enable)

    assert main(["ops", "bot", "disable"]) == 1
    captured = capsys.readouterr()
    assert captured.err == f"afx ops: {reason}\n"
    assert captured.out == ""


def test_ops_failure_prints_detail_after_the_fixed_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def enable(self, feature: str) -> StageResult:
        raise SystemdError("start", "rc=5 stderr=Unit afx.service not found.")

    monkeypatch.setattr("agentic_fx.ops_setup.stages.OpsSetup.disable", enable)

    assert main(["ops", "bot", "disable"]) == 1
    assert capsys.readouterr().err == "afx ops: start (rc=5 stderr=Unit afx.service not found.)\n"


def test_ops_status_without_systemctl_reports_unknown_unit(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    assert main(["ops", "bot", "status"]) == 0

    assert capsys.readouterr().out == "bot: disable disabled unit=unknown result=unknown\n"


def test_ops_marker_rejection_prints_owner_and_recovery(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def enable(self, feature: str) -> StageResult:
        raise InvalidState("marker_foreign", "path=/x feature=web")

    monkeypatch.setattr("agentic_fx.ops_setup.stages.OpsSetup.disable", enable)

    assert main(["ops", "bot", "disable"]) == 1
    assert capsys.readouterr().err == "afx ops: marker_foreign (path=/x feature=web)\n"


def test_ops_legacy_shape_normalizes_after_multiple_daemon_prefixes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    seen: list[tuple[str, str]] = []

    def run(root, action: str, feature: str):
        seen.append((action, feature))
        return 0

    monkeypatch.setattr("agentic_fx.ops_setup.stages.run_ops_command", run)
    assert main(["--daemon", "--daemon", "ops", "status", "web"]) == 0
    assert seen == [("status", "web")]
    assert "afx ops status web は旧形です。afx ops web status を使ってください。" in capsys.readouterr().err


def test_ops_reconf_dispatches_the_web_reconfigure_handler(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen: list[tuple[object, str]] = []

    def reconfigure(root, feature: str):
        seen.append((root, feature))
        return 7

    monkeypatch.setattr("agentic_fx.ops_setup.reconfigure.run_ops_reconfigure", reconfigure)
    assert main(["ops", "web", "reconf"]) == 7
    assert seen == [(tmp_path, "web")]


def test_expired_compatibility_does_not_intercept_improve(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("agentic_fx.entry.importlib.metadata.version", lambda _name: "0.3.0")
    with pytest.raises(SystemExit) as exc:
        main(["improve"])
    assert exc.value.code == 2
    assert "廃止されました" not in capsys.readouterr().err
