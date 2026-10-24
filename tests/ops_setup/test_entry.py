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


# --- 新旧 `afx ops` 語順の同一性 (shim) -----------------------------------

_FEATURES = ("bot", "web", "notify")
_VERBS = ("enable", "disable", "status")
_RC = {"enable": 11, "disable": 12, "status": 13, "reconfigure": 14}


def _ops_spies(monkeypatch):
    """Record every handler call as (handler, args) and the parsed `--daemon` flag."""
    import argparse

    calls: list[tuple] = []
    namespaces: list[argparse.Namespace] = []

    def enable(root, feature, **_kwargs):
        from agentic_fx.ops_setup.interactive import EnableResult
        calls.append(("enable", root, feature))
        return EnableResult(_RC["enable"], "enable", "ok")

    def command(root, action, feature):
        calls.append((action, root, feature))
        return _RC[action]

    def reconfigure(root, feature):
        calls.append(("reconfigure", root, feature))
        return _RC["reconfigure"]

    real_parse = argparse.ArgumentParser.parse_args

    def parse(self, args=None, namespace=None):
        result = real_parse(self, args, namespace)
        namespaces.append(result)
        return result

    monkeypatch.setattr("agentic_fx.ops_setup.interactive.interactive_enable", enable)
    monkeypatch.setattr("agentic_fx.ops_setup.stages.run_ops_command", command)
    monkeypatch.setattr("agentic_fx.ops_setup.reconfigure.run_ops_reconfigure", reconfigure)
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", parse)
    return calls, namespaces


def _legacy_message(verb: str, feature: str) -> str:
    return (f"afx ops {verb} {feature} は旧形です。afx ops {feature} {verb} を使ってください。"
            "この形は 0.3.0 で削除します。")


@pytest.mark.parametrize("daemon", [0, 1, 2])
@pytest.mark.parametrize("verb", _VERBS)
@pytest.mark.parametrize("feature", _FEATURES)
def test_legacy_ops_shape_runs_the_same_handler_as_the_new_shape(
        tmp_path, monkeypatch, capsys, feature, verb, daemon):
    monkeypatch.chdir(tmp_path)
    prefix = ["--daemon"] * daemon
    calls, namespaces = _ops_spies(monkeypatch)

    new_rc = main(prefix + ["ops", feature, verb])
    new = (list(calls), namespaces[-1].daemon, capsys.readouterr())
    calls.clear()
    old_rc = main(prefix + ["ops", verb, feature])
    old = (list(calls), namespaces[-1].daemon, capsys.readouterr())

    assert new[0] == [(verb, tmp_path, feature)]
    assert old[0] == new[0]
    assert new_rc == old_rc == _RC[verb]
    assert old[1] == new[1] == bool(daemon)
    assert new[2].err == "" and new[2].out == ""
    assert old[2].err == _legacy_message(verb, feature) + "\n"
    assert old[2].out == ""


@pytest.mark.parametrize("daemon", [0, 1, 2])
@pytest.mark.parametrize("argv, verb", [
    (["ops", "enable"], "enable"),
    (["ops", "disable"], "disable"),
    (["ops", "status"], "status"),
    (["ops", "enable", "invalid"], "enable"),
    (["ops", "status", "reconf"], "status"),
    (["ops", "disable", "Web"], "disable"),
])
def test_legacy_ops_shape_without_a_valid_feature_is_rc2_with_no_dispatch(
        tmp_path, monkeypatch, capsys, argv, verb, daemon):
    monkeypatch.chdir(tmp_path)
    calls, namespaces = _ops_spies(monkeypatch)

    assert main(["--daemon"] * daemon + argv) == 2

    captured = capsys.readouterr()
    assert calls == [] and namespaces == []
    assert captured.out == ""
    assert captured.err == _legacy_message(verb, "<bot|web|notify>") + "\n"


@pytest.mark.parametrize("argv", [["ops"], ["ops", "bogus"], ["ops", "bot"], ["ops", "bot", "bogus"]])
def test_ops_shapes_argparse_rejects_dispatch_nothing_and_print_no_legacy_notice(
        tmp_path, monkeypatch, capsys, argv):
    monkeypatch.chdir(tmp_path)
    calls, _namespaces = _ops_spies(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert calls == []
    assert "旧形です" not in capsys.readouterr().err


@pytest.mark.parametrize("daemon", [0, 1, 2])
@pytest.mark.parametrize("spelling", ["reconfigure", "reconf"])
def test_web_reconfigure_and_its_alias_reach_one_handler_without_a_notice(
        tmp_path, monkeypatch, capsys, spelling, daemon):
    monkeypatch.chdir(tmp_path)
    calls, namespaces = _ops_spies(monkeypatch)

    assert main(["--daemon"] * daemon + ["ops", "web", spelling]) == _RC["reconfigure"]

    captured = capsys.readouterr()
    assert calls == [("reconfigure", tmp_path, "web")]
    assert namespaces[-1].daemon is bool(daemon)
    assert namespaces[-1].ops_action == "reconfigure"
    assert captured.err == "" and captured.out == ""


@pytest.mark.parametrize("daemon", [0, 1, 2])
@pytest.mark.parametrize("spelling", ["reconfigure", "reconf"])
@pytest.mark.parametrize("feature", ["bot", "notify"])
def test_bot_and_notify_reject_both_reconfigure_spellings_as_typed(
        tmp_path, monkeypatch, capsys, feature, spelling, daemon):
    monkeypatch.chdir(tmp_path)
    calls, _namespaces = _ops_spies(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        main(["--daemon"] * daemon + ["ops", feature, spelling])

    assert exc.value.code == 2
    assert calls == []
    err = capsys.readouterr().err
    assert f"invalid choice: '{spelling}'" in err
    assert "旧形です" not in err


def test_the_unimplemented_web_reconfigure_stub_fails_with_a_fixed_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["ops", "web", "reconfigure"]) == 1
    captured = capsys.readouterr()
    assert captured.err == "afx ops: reconfigure_not_available\n"
    assert captured.out == ""


def test_the_help_marks_the_web_reconfigure_stub_as_unimplemented(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["ops", "web", "--help"])
    assert exc.value.code == 0
    assert "(現在は未実装)" in capsys.readouterr().out


def test_expired_ops_legacy_shape_is_left_to_argparse(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("agentic_fx.entry.importlib.metadata.version", lambda _name: "0.3.0")
    calls, _namespaces = _ops_spies(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(["ops", "enable", "web"])
    assert exc.value.code == 2
    assert calls == []
    assert "旧形です" not in capsys.readouterr().err


def test_a_missing_installed_version_keeps_the_ops_legacy_shape_working(tmp_path, monkeypatch, capsys):
    import importlib.metadata
    monkeypatch.chdir(tmp_path)

    def missing(_name):
        raise importlib.metadata.PackageNotFoundError("agentic-fx")

    monkeypatch.setattr("agentic_fx.entry.importlib.metadata.version", missing)
    calls, _namespaces = _ops_spies(monkeypatch)
    assert main(["ops", "status", "web"]) == _RC["status"]
    assert calls == [("status", tmp_path, "web")]
    assert capsys.readouterr().err == _legacy_message("status", "web") + "\n"


def test_the_version_is_not_looked_up_for_commands_the_shim_does_not_touch(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def forbidden(_name):
        raise AssertionError("version consulted")

    monkeypatch.setattr("agentic_fx.entry.importlib.metadata.version", forbidden)
    calls, _namespaces = _ops_spies(monkeypatch)
    assert main(["ops", "bot", "status"]) == _RC["status"]
    assert main(["ops", "web", "reconf"]) == _RC["reconfigure"]
    with pytest.raises(SystemExit) as exc:
        main(["check", "backend"])
    assert exc.value.code == 2
    assert [call[0] for call in calls] == ["status", "reconfigure"]
    assert "version consulted" not in capsys.readouterr().err


# --- 稼働中 daemon への enable (CLI 経由) ------------------------------------


def test_enable_with_a_running_daemon_and_no_keys_stops_with_rc1_before_keys_init(
        tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from agentic_fx.ops_setup.feature_config import write_inputs

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    write_inputs(tmp_path, "web", {"port": 8788})
    keys_init: list[tuple] = []
    monkeypatch.setattr("agentic_fx.ops_setup.interactive.resolve_ops_exec",
                        lambda paths: SimpleNamespace(ops_exec="/bin/true"))
    monkeypatch.setattr("agentic_fx.ops_setup.interactive.run_probe", lambda *_args: (0, "ok"))
    monkeypatch.setattr("agentic_fx.ops_setup.interactive.run_keys_command",
                        lambda *args: keys_init.append(args) or 0)

    def not_initialized(self, feature):
        from agentic_fx.ops.keys import KeySetError
        raise KeySetError("not_initialized")

    monkeypatch.setattr("agentic_fx.ops_setup.stages.OpsSetup.enable", not_initialized)
    monkeypatch.setattr("agentic_fx.ops_setup.systemd.Systemd.show",
                        lambda self, unit, *props: {"ActiveState": "active"})

    assert main(["ops", "web", "enable"]) == 1

    captured = capsys.readouterr()
    assert keys_init == []
    assert captured.err.startswith("afx ops: afx_running_keys_not_initialized")
    assert captured.err.count("\n") == 1


def test_enable_with_a_running_daemon_completes_with_rc0(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from agentic_fx.ops_setup.feature_config import write_inputs

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    write_inputs(tmp_path, "web", {"port": 8788})
    keys_init: list[tuple] = []
    enabled: list[str] = []
    monkeypatch.setattr("agentic_fx.ops_setup.interactive.resolve_ops_exec",
                        lambda paths: SimpleNamespace(ops_exec="/bin/true"))
    monkeypatch.setattr("agentic_fx.ops_setup.interactive.run_probe", lambda *_args: (0, "ok"))
    monkeypatch.setattr("agentic_fx.ops_setup.interactive.run_keys_command",
                        lambda *args: keys_init.append(args) or 0)
    monkeypatch.setattr("agentic_fx.ops_setup.stages.OpsSetup.enable",
                        lambda self, feature: enabled.append(feature) or SimpleNamespace(stage="enabled"))
    monkeypatch.setattr("agentic_fx.ops_setup.systemd.Systemd.show",
                        lambda self, unit, *props: {"ActiveState": "active"})

    assert main(["ops", "web", "enable"]) == 0

    captured = capsys.readouterr()
    assert enabled == ["web"] and keys_init == []
    assert captured.out == "web: enabled\n"
    assert "afx_running_keys_not_initialized" not in captured.err
