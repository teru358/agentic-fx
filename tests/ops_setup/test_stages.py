from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_fx.ops.keys import Principal, initialize, key_dir
from agentic_fx.ops_setup.stages import InvalidState, OpsSetup, read_stage
from agentic_fx.ops_setup.systemd import SystemdError
from agentic_fx.ops_setup.units import UnitPaths
from agentic_fx.store.enroll_marker import enroll_marker_path
from agentic_fx.store.instance_lock import InstanceAlreadyRunning, acquire_instance_lock


class FakeSystemd:
    """systemctl 境界だけを置き換える。unit 資材と鍵 lifecycle は実物を使う。

    load 状態と Wants は、daemon-reload 時点の unit 資材から導く (reload 前の変更は見えない)。
    """

    def __init__(self, paths: UnitPaths) -> None:
        self.paths = paths
        self.active: set[str] = set()
        # stop しても inactive にならない unit (止まらない afx を模す)。
        self.stuck: set[str] = set()
        self.fail: set[str] = set()
        self.calls: list[str] = []
        self.loaded_template = False
        self.loaded_wants: list[str] = []
        self.results: dict[str, str] = {}
        # afx の unit が持つ RestartPreventExitStatus (marker 中の即終了 75 を再起動しない設定)。
        self.restart_prevent = "75"

    def _call(self, action: str, unit: str | None = None) -> None:
        label = f"{action}:{unit}" if unit else action
        self.calls.append(label)
        if action in self.fail:
            raise SystemdError(action)

    def stop(self, unit: str) -> None:
        self._call("stop", unit)
        if unit not in self.stuck:
            self.active.discard(unit)

    def start(self, unit: str) -> None:
        self._call("start", unit)
        self.active.add(unit)

    def ensure_active(self, unit: str) -> None:
        if unit not in self.active:
            self.start(unit)

    def is_active(self, unit: str) -> bool:
        self._call("is-active", unit)
        return unit in self.active

    def is_stopped(self, unit: str) -> bool:
        self._call("is-active", unit)
        return unit not in self.active

    def daemon_reload(self) -> None:
        self._call("daemon-reload")
        self.loaded_template = self.paths.template_path.exists()
        dropin = self.paths.dropin_path
        self.loaded_wants = []
        if dropin.exists():
            for line in dropin.read_text().splitlines():
                if line.startswith("Wants="):
                    self.loaded_wants += line[len("Wants="):].split()

    def show(self, unit: str, *props: str) -> dict[str, str]:
        self._call("show", unit)
        if unit == self.paths.afx_unit:
            load = "loaded"
        else:
            load = "loaded" if self.loaded_template else "not-found"
        values = {"LoadState": load,
                  "ActiveState": "active" if unit in self.active else "inactive",
                  "Result": self.results.get(unit, "success"),
                  "Wants": " ".join(self.loaded_wants) if unit == self.paths.afx_unit else "",
                  "RestartPreventExitStatus": self.restart_prevent if unit == self.paths.afx_unit else ""}
        return {p: values[p] for p in props}


def _setup(tmp_path: Path) -> tuple[OpsSetup, FakeSystemd, Path]:
    root = tmp_path / "instance"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    # key_dir は HOME から導く。pytest process の実 HOME には触れない。
    import os
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        initialize(key_dir(root), root=root)
    finally:
        if old_home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = old_home
    # ExecStart に書く起動コマンドは実在・自分の持ち物・実行可を確かめられるので、実 file を置く。
    ops_exec = tmp_path / "bin" / "afx-ops"
    ops_exec.parent.mkdir()
    ops_exec.write_text("#!/bin/sh\n")
    ops_exec.chmod(0o755)
    paths = UnitPaths(unit_dir=tmp_path / "units", afx_unit="afx-test.service",
                      ops_exec=str(ops_exec))
    systemd = FakeSystemd(paths)
    # 通常の enable は常駐中の afx に対して行う。
    systemd.active.add(paths.afx_unit)
    return OpsSetup(root, systemd=systemd, paths=paths, ready=lambda _feature: True,
                    config_check=lambda _root, _feature: True, probe=lambda _exec, _feature: (0, "")), systemd, home


def _with_home(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(home))


def _entry(root: Path, principal: Principal) -> dict:
    manifest = json.loads((key_dir(root) / ".ready").read_text())
    return manifest["principals"][principal.value]


def test_enable_enrolls_only_feature_principal_and_records_terminal_stage(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    operator_digest = _entry(setup.root, Principal.OPERATOR)["sha256"]

    result = setup.enable("bot")

    assert result.stage == "enabled"
    saved = read_stage(setup.root, "bot")
    assert saved["operation"] == "enable"
    assert saved["stage"] == "enabled"
    assert saved["principal"] == "ops-bot"
    assert _entry(setup.root, Principal.OPERATOR)["sha256"] == operator_digest
    bot_entry = _entry(setup.root, Principal.OPS_BOT)
    assert bot_entry["generation"] == 1
    assert bot_entry["state"] == "active"
    assert len(bot_entry["sha256"]) == 64
    assert not enroll_marker_path(setup.root).exists()
    assert not any(call.startswith(("mask", "unmask")) for call in systemd.calls)


def test_install_failure_removes_partial_units_and_returns_to_enrolled(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.paths.unit_dir.mkdir()
    setup.paths.unit_dir.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            setup.enable("bot")
    finally:
        setup.paths.unit_dir.chmod(0o700)

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "enrolled"
    assert not setup.paths.template_path.exists()
    assert not setup.paths.dropin_path.exists()
    assert _entry(setup.root, Principal.OPS_BOT)["state"] == "active"


def test_install_reload_failure_removes_written_units_and_returns_to_enrolled(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    systemd.fail.add("daemon-reload")

    with pytest.raises(SystemdError, match="daemon-reload"):
        setup.enable("bot")

    assert read_stage(setup.root, "bot")["stage"] == "enrolled"
    assert not setup.paths.template_path.exists()
    assert not setup.paths.dropin_path.exists()
    assert _entry(setup.root, Principal.OPS_BOT)["state"] == "active"


def test_checked_failure_returns_to_installed_and_retry_finishes(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.ready = lambda _feature: False

    with pytest.raises(SystemdError, match="ops_not_ready"):
        setup.enable("bot")

    assert read_stage(setup.root, "bot")["stage"] == "installed"
    assert setup.paths.template_path.exists()
    assert "afx-test.service" not in systemd.active
    assert not enroll_marker_path(setup.root).exists()
    setup.ready = lambda _feature: True
    assert setup.enable("bot").stage == "enabled"


def test_unterminated_enable_rejects_a_different_operation_without_changes(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    systemd.fail.add("start")
    with pytest.raises(SystemdError):
        setup.enable("bot")
    before = read_stage(setup.root, "bot")

    with pytest.raises(InvalidState, match="invalid_state"):
        setup.enable("web")

    assert read_stage(setup.root, "bot") == before
    assert not read_stage(setup.root, "web", missing_ok=True)
    assert "afx-test.service" not in systemd.active


def test_stop_failure_stays_staged_with_fixed_failure_reason(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    systemd.fail.add("stop")

    with pytest.raises(SystemdError, match="stop"):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "staged"
    assert saved["failure"] == "stop"


def test_disable_removes_last_unit_material_but_preserves_feature_key(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    digest = _entry(setup.root, Principal.OPS_BOT)["sha256"]

    result = setup.disable("bot")

    assert result.stage == "disabled"
    assert not setup.paths.template_path.exists()
    assert not setup.paths.dropin_path.exists()
    assert _entry(setup.root, Principal.OPS_BOT)["sha256"] == digest


def test_disabling_one_of_two_features_keeps_template_and_remaining_wants(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    setup.enable("web")

    setup.disable("bot")

    assert setup.paths.template_path.exists()
    assert setup.paths.dropin_path.read_text() == "[Unit]\nWants=afx-ops@web.service\n"
    assert read_stage(setup.root, "bot")["stage"] == "disabled"
    assert read_stage(setup.root, "web")["stage"] == "enabled"


def test_terminal_stage_rerun_does_not_rewrite_stage_or_unit_material(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    stage_path = setup.root / "data" / "ops" / "bot.stage.json"
    template = setup.paths.template_path
    dropin = setup.paths.dropin_path
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in (stage_path, template, dropin)]

    setup.enable("bot")

    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in (stage_path, template, dropin)] == before


def test_crash_after_enroll_keeps_committed_key_and_same_enable_resumes(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)

    def crash_after_enroll(name: str) -> None:
        if name == "after_enroll":
            raise SystemExit(70)

    setup.hook = crash_after_enroll
    with pytest.raises(SystemExit, match="70"):
        setup.enable("bot")

    assert read_stage(setup.root, "bot")["stage"] == "stopped"
    assert _entry(setup.root, Principal.OPS_BOT)["state"] == "active"
    setup.hook = lambda _name: None
    assert setup.enable("bot").stage == "enabled"


def test_stage_write_ignores_symlink_planted_at_predictable_temp_name(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    ops_dir = setup.root / "data" / "ops"
    ops_dir.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")
    (ops_dir / ".bot.stage.json.tmp").symlink_to(victim)

    assert setup.enable("bot").stage == "enabled"

    assert victim.read_bytes() == b"untouched"
    assert read_stage(setup.root, "bot")["stage"] == "enabled"
    assert not (ops_dir / "bot.stage.json").is_symlink()


def test_enable_rejects_key_dir_with_broken_permissions_before_stopping_afx(tmp_path, monkeypatch):
    from agentic_fx.ops.keys import KeySetError

    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    directory = key_dir(setup.root)
    directory.chmod(0o755)
    before = (directory / ".ready").read_bytes()
    try:
        with pytest.raises(KeySetError, match="key_dir_invalid"):
            setup.enable("bot")
    finally:
        directory.chmod(0o700)

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "staged"
    assert saved["failure"] == "key_dir_invalid"
    assert systemd.calls == []
    assert not enroll_marker_path(setup.root).exists()
    assert (directory / ".ready").read_bytes() == before


def test_enable_rejects_key_dir_reachable_from_sandbox_before_stopping_afx(tmp_path, monkeypatch):
    from agentic_fx.ops.keys import KeySetError

    setup, systemd, _home = _setup(tmp_path)
    inside = setup.root / "plugins" / "home"
    inside.mkdir(parents=True)
    _with_home(inside, monkeypatch)

    with pytest.raises(KeySetError, match="token_dir_in_sandbox"):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "staged"
    assert saved["failure"] == "token_dir_in_sandbox"
    assert systemd.calls == []
    assert not key_dir(setup.root).exists()


def _crash_at(point: str):
    def hook(name: str) -> None:
        if name == point:
            raise SystemExit(70)
    return hook


def _recording(names: list[str]):
    return names.append


def _stage_file(root: Path) -> Path:
    return root / "data" / "ops" / "bot.stage.json"


def test_resume_reinstalls_when_unit_template_was_deleted_after_installed(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.hook = _crash_at("after_stage_installed")
    with pytest.raises(SystemExit):
        setup.enable("bot")
    assert read_stage(setup.root, "bot")["stage"] == "installed"
    setup.paths.template_path.unlink()

    names: list[str] = []
    setup.hook = _recording(names)
    assert setup.enable("bot").stage == "enabled"

    assert "before_install" in names
    assert "before_stage_installed" in names
    assert "Type=notify" in setup.paths.template_path.read_text()


def test_resume_reinstalls_when_dropin_no_longer_wants_the_feature(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.hook = _crash_at("after_stage_installed")
    with pytest.raises(SystemExit):
        setup.enable("bot")
    setup.paths.dropin_path.write_text("[Unit]\n")

    names: list[str] = []
    setup.hook = _recording(names)
    assert setup.enable("bot").stage == "enabled"

    assert "before_install" in names
    assert setup.paths.dropin_path.read_text() == "[Unit]\nWants=afx-ops@bot.service\n"


def test_stage_edited_ahead_of_enroll_goes_back_and_enrolls(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.hook = _crash_at("after_stage_stopped")
    with pytest.raises(SystemExit):
        setup.enable("bot")
    saved = read_stage(setup.root, "bot")
    saved["stage"] = "installed"
    _stage_file(setup.root).write_text(json.dumps(saved))
    assert Principal.OPS_BOT.value not in json.loads((key_dir(setup.root) / ".ready").read_text())["principals"]

    names: list[str] = []
    setup.hook = _recording(names)
    assert setup.enable("bot").stage == "enabled"

    assert "before_enroll" in names
    assert _entry(setup.root, Principal.OPS_BOT)["state"] == "active"


def test_resume_from_started_with_inactive_ops_restarts_from_installed(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.hook = _crash_at("after_stage_started")
    with pytest.raises(SystemExit):
        setup.enable("bot")
    systemd.active.discard(setup.paths.feature_unit("bot"))

    names: list[str] = []
    setup.hook = _recording(names)
    assert setup.enable("bot").stage == "enabled"

    assert "before_start" in names
    assert "before_install" not in names
    assert setup.paths.feature_unit("bot") in systemd.active


def test_stage_file_with_unknown_stage_is_rejected_without_changes(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    path = _stage_file(setup.root)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"operation": "enable", "stage": "bogus", "feature": "bot"}))

    with pytest.raises(InvalidState, match="invalid_stage"):
        setup.enable("bot")

    assert json.loads(path.read_text())["stage"] == "bogus"
    assert systemd.calls == []


def test_disable_does_not_commit_while_target_ops_unit_is_still_active(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    setup.hook = _crash_at("after_stage_uninstalled")
    with pytest.raises(SystemExit):
        setup.disable("bot")
    systemd.active.add(setup.paths.feature_unit("bot"))
    setup.hook = lambda _name: None

    with pytest.raises(SystemdError, match="uninstall_incomplete"):
        setup.disable("bot")
    assert read_stage(setup.root, "bot")["stage"] == "enabled"

    assert setup.disable("bot").stage == "disabled"
    assert setup.paths.feature_unit("bot") not in systemd.active


def test_disable_does_not_commit_while_manager_still_wants_the_unit(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    setup.enable("web")
    setup.hook = _crash_at("after_stage_uninstalled")
    with pytest.raises(SystemExit):
        setup.disable("bot")
    # 資材は書き換わったが manager が古い Wants のまま (reload 未完了) の状態。
    systemd.loaded_wants.append(setup.paths.feature_unit("bot"))
    setup.hook = lambda _name: None

    with pytest.raises(SystemdError, match="uninstall_incomplete"):
        setup.disable("bot")
    assert read_stage(setup.root, "bot")["stage"] == "stopping-ops"

    assert setup.disable("bot").stage == "disabled"
    assert systemd.loaded_wants == [setup.paths.feature_unit("web")]


def test_disable_of_last_feature_requires_unit_to_be_unloaded(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    setup.hook = _crash_at("after_stage_uninstalled")
    with pytest.raises(SystemExit):
        setup.disable("bot")
    systemd.loaded_template = True
    setup.hook = lambda _name: None

    with pytest.raises(SystemdError, match="uninstall_incomplete"):
        setup.disable("bot")

    assert setup.disable("bot").stage == "disabled"
    assert systemd.show(setup.paths.feature_unit("bot"), "LoadState") == {"LoadState": "not-found"}


def test_status_line_shows_target_unit_active_state_and_result(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.ready = lambda _feature: False
    with pytest.raises(SystemdError):
        setup.enable("bot")
    # 実 manager では afx の停止で PartOf により ops も止まる。偽物ではここで写す。
    systemd.active.discard(setup.paths.feature_unit("bot"))
    systemd.results[setup.paths.feature_unit("bot")] = "exit-code"

    assert setup.status_line("bot") == "bot: enable installed (ops_not_ready) unit=inactive result=exit-code"


def _lock_free(root: Path) -> bool:
    try:
        acquire_instance_lock(root / "data").close()
    except InstanceAlreadyRunning:
        return False
    return True


def test_enable_orders_marker_stop_confirm_lock_enroll_unlock_and_marker_removal(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    systemd.active.add("afx-test.service")
    seen: dict[str, tuple[bool, bool]] = {}

    def hook(name: str) -> None:
        systemd.calls.append(name)
        seen[name] = (enroll_marker_path(setup.root).exists(), _lock_free(setup.root))

    setup.hook = hook
    assert setup.enable("bot").stage == "enabled"

    calls = systemd.calls
    upto = calls[:calls.index("before_stage_enrolled")]
    assert upto == [
        "show:afx-test.service",
        "before_marker", "after_marker",
        "before_stop", "stop:afx-test.service", "after_stop",
        "before_show", "show:afx-test.service", "after_show",
        "before_lock", "after_lock",
        "before_stage_stopped", "after_stage_stopped",
        "before_enroll", "after_enroll",
        "before_unlock", "after_unlock",
        "before_unmark", "after_unmark",
    ]
    # (marker が在る, flock が空いている)
    assert seen["before_marker"] == (False, True)
    assert seen["after_marker"] == (True, True)
    assert seen["after_lock"] == (True, False)
    assert seen["after_enroll"] == (True, False)
    assert seen["after_unlock"] == (True, True)
    assert seen["after_unmark"] == (False, True)
    assert not enroll_marker_path(setup.root).exists()


def test_marker_records_feature_and_start_time(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    content: dict = {}

    def hook(name: str) -> None:
        if name == "after_marker":
            content.update(json.loads(enroll_marker_path(setup.root).read_text()))

    setup.hook = hook
    setup.enable("bot")

    assert set(content) == {"feature", "started_at"}
    assert content["feature"] == "bot"
    assert content["started_at"].endswith("Z")
    assert enroll_marker_path(setup.root).parent == _stage_file(setup.root).parent


def test_afx_still_active_after_stop_stays_staged_and_keeps_marker(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    systemd.active.add("afx-test.service")
    systemd.stuck.add("afx-test.service")

    with pytest.raises(SystemdError, match="afx_still_running"):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "staged"
    assert saved["failure"] == "afx_still_running"
    assert enroll_marker_path(setup.root).exists()
    assert Principal.OPS_BOT.value not in json.loads((key_dir(setup.root) / ".ready").read_text())["principals"]

    systemd.stuck.clear()
    assert setup.enable("bot").stage == "enabled"
    assert not enroll_marker_path(setup.root).exists()


def test_flock_held_by_a_live_afx_stays_staged_and_keeps_marker(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    held = acquire_instance_lock(setup.root / "data")
    try:
        with pytest.raises(SystemdError, match="afx_still_running"):
            setup.enable("bot")
    finally:
        held.close()

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "staged"
    assert enroll_marker_path(setup.root).exists()
    assert setup.enable("bot").stage == "enabled"
    assert not enroll_marker_path(setup.root).exists()


def test_rerun_keeps_existing_marker_start_time(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    systemd.fail.add("stop")
    with pytest.raises(SystemdError):
        setup.enable("bot")
    first = enroll_marker_path(setup.root).read_bytes()
    seen: list[bytes] = []
    systemd.fail.clear()

    def hook(name: str) -> None:
        if name == "after_marker":
            seen.append(enroll_marker_path(setup.root).read_bytes())

    setup.hook = hook
    assert setup.enable("bot").stage == "enabled"
    assert seen == [first]


_FAKE_ENABLE_POINTS = [f"{side}_{step}" for step in
                       ("marker", "stop", "show", "lock", "enroll", "unlock", "unmark",
                        "install", "start", "check") for side in ("before", "after")]
_FAKE_ENABLE_POINTS += [f"{side}_stage_{stage}" for stage in
                        ("stopped", "enrolled", "installed", "started", "checked", "enabled")
                        for side in ("before", "after")]


@pytest.mark.parametrize("point", _FAKE_ENABLE_POINTS)
def test_enable_crash_at_each_point_resumes_and_clears_marker(tmp_path, monkeypatch, point):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    systemd.active.add("afx-test.service")
    setup.hook = _crash_at(point)
    with pytest.raises(SystemExit):
        setup.enable("bot")

    setup.hook = lambda _name: None
    assert setup.enable("bot").stage == "enabled"

    assert not enroll_marker_path(setup.root).exists()
    assert _lock_free(setup.root)
    assert "afx-test.service" in systemd.active
    assert _entry(setup.root, Principal.OPS_BOT)["generation"] == 1


@pytest.mark.parametrize("point", ["after_enroll", "before_unlock", "after_unlock", "before_unmark"])
def test_enrolled_with_marker_left_resumes_from_marker_removal(tmp_path, monkeypatch, point):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.hook = _crash_at(point)
    with pytest.raises(SystemExit):
        setup.enable("bot")
    assert read_stage(setup.root, "bot")["stage"] == "stopped"
    assert enroll_marker_path(setup.root).exists()
    digest = _entry(setup.root, Principal.OPS_BOT)["sha256"]

    names: list[str] = []
    setup.hook = _recording(names)
    assert setup.enable("bot").stage == "enabled"

    first = names[:names.index("before_stage_enrolled")]
    assert first == ["before_unmark", "after_unmark"]
    assert _entry(setup.root, Principal.OPS_BOT)["sha256"] == digest
    assert not enroll_marker_path(setup.root).exists()


@pytest.mark.parametrize("point", ["after_unmark", "before_stage_enrolled"])
def test_enrolled_without_marker_resumes_as_enrolled(tmp_path, monkeypatch, point):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.hook = _crash_at(point)
    with pytest.raises(SystemExit):
        setup.enable("bot")
    assert read_stage(setup.root, "bot")["stage"] == "stopped"
    assert not enroll_marker_path(setup.root).exists()

    names: list[str] = []
    setup.hook = _recording(names)
    assert setup.enable("bot").stage == "enabled"

    assert names[:2] == ["before_stage_enrolled", "after_stage_enrolled"]
    assert not enroll_marker_path(setup.root).exists()


def test_marker_left_beyond_enrolled_is_removed_on_rerun(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.hook = _crash_at("after_stage_installed")
    with pytest.raises(SystemExit):
        setup.enable("bot")
    path = enroll_marker_path(setup.root)
    path.write_text(json.dumps({"feature": "bot", "started_at": "2026-10-10T00:00:00Z"}))

    names: list[str] = []
    setup.hook = _recording(names)
    assert setup.enable("bot").stage == "enabled"

    assert "before_enroll" not in names
    assert "before_unmark" in names
    assert not path.exists()


def test_marker_of_another_feature_is_rejected_without_changes(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    path = enroll_marker_path(setup.root)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"feature": "web", "started_at": "2026-10-10T00:00:00Z"}))
    before = path.read_bytes()

    with pytest.raises(InvalidState, match="marker_foreign") as caught:
        setup.enable("bot")

    assert caught.value.reason == "marker_foreign"
    assert f"path={path}" in caught.value.detail
    assert "feature=web" in caught.value.detail
    assert "afx ops enable web" in caught.value.detail
    assert path.read_bytes() == before
    assert systemd.calls == []
    assert read_stage(setup.root, "bot", missing_ok=True) is None


def test_status_line_shows_left_marker(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.hook = _crash_at("after_lock")
    with pytest.raises(SystemExit):
        setup.enable("bot")
    since = json.loads(enroll_marker_path(setup.root).read_text())["started_at"]

    line = setup.status_line("bot")

    assert line.startswith("bot: enable staged unit=")
    assert line.endswith(f" marker=enroll_in_progress feature=bot since={since}"
                         f" path={enroll_marker_path(setup.root)}")
    setup.hook = lambda _name: None
    assert setup.enable("bot").stage == "enabled"
    assert "marker=" not in setup.status_line("bot")


def test_stage_ahead_of_enroll_without_marker_goes_back_and_enrolls(tmp_path, monkeypatch):
    # marker が無くても、principal が未登録なら enrolled 以降を信じない。
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.hook = _crash_at("after_stage_stopped")
    with pytest.raises(SystemExit):
        setup.enable("bot")
    enroll_marker_path(setup.root).unlink()
    saved = read_stage(setup.root, "bot")
    saved["stage"] = "installed"
    _stage_file(setup.root).write_text(json.dumps(saved))

    names: list[str] = []
    setup.hook = _recording(names)
    assert setup.enable("bot").stage == "enabled"

    assert "before_enroll" in names
    assert _entry(setup.root, Principal.OPS_BOT)["state"] == "active"


def test_resume_reinstalls_when_manager_does_not_load_the_unit(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.hook = _crash_at("after_stage_installed")
    with pytest.raises(SystemExit):
        setup.enable("bot")
    real_show = systemd.show

    def show(unit: str, *props: str) -> dict[str, str]:
        values = real_show(unit, *props)
        if unit == setup.paths.feature_unit("bot") and "LoadState" in values:
            values["LoadState"] = "bad-setting"
        return values

    systemd.show = show
    setup.hook = _crash_at("before_install")
    with pytest.raises(SystemExit):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "enrolled"
    assert saved["failure"] == "stage_mismatch"


def test_resume_reinstalls_when_dropin_lost_another_enabled_feature(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("web")
    setup.hook = _crash_at("after_stage_installed")
    with pytest.raises(SystemExit):
        setup.enable("bot")
    setup.paths.dropin_path.write_text("[Unit]\nWants=afx-ops@bot.service\n")

    names: list[str] = []
    setup.hook = _recording(names)
    assert setup.enable("bot").stage == "enabled"

    assert "before_install" in names
    assert setup.paths.dropin_path.read_text() == (
        "[Unit]\nWants=afx-ops@bot.service afx-ops@web.service\n")


@pytest.mark.parametrize("content", [
    b"", b"{not json", b"[]", b'{"started_at": "2026-10-10T00:00:00Z"}',
    b'{"feature": ["bot"], "started_at": "2026-10-10T00:00:00Z"}',
    b'{"feature": {"bot": 1}, "started_at": "2026-10-10T00:00:00Z"}',
    b'{"feature": "bot"}', b'{"feature": "bot", "started_at": ""}', b'{"feature": "bot", "started_at": 5}',
])
def test_marker_with_unknown_owner_is_rejected_without_changes(tmp_path, monkeypatch, content):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    path = enroll_marker_path(setup.root)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    with pytest.raises(InvalidState, match="marker_corrupt") as caught:
        setup.enable("bot")

    assert caught.value.reason == "marker_corrupt"
    assert f"path={path}" in caught.value.detail
    assert "手で削除" in caught.value.detail

    assert path.read_bytes() == content
    assert systemd.calls == []
    assert read_stage(setup.root, "bot", missing_ok=True) is None


def test_failure_does_not_record_commit(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    systemd.fail.add("stop")
    with pytest.raises(SystemdError):
        setup.enable("bot")
    assert read_stage(setup.root, "bot")["committed"] is False

    systemd.fail.clear()
    setup.ready = lambda _feature: False
    with pytest.raises(SystemdError, match="ops_not_ready"):
        setup.enable("bot")
    saved = read_stage(setup.root, "bot")
    assert saved["failure"] == "ops_not_ready"
    assert saved["committed"] is False


def test_disable_of_last_feature_does_not_commit_while_template_file_remains(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    template = setup.paths.template_path.read_bytes()
    setup.hook = _crash_at("after_stage_uninstalled")
    with pytest.raises(SystemExit):
        setup.disable("bot")
    # manager は reload 前なので not-found のまま、file だけが残っている。
    setup.paths.template_path.write_bytes(template)
    setup.hook = lambda _name: None

    with pytest.raises(SystemdError, match="uninstall_incomplete"):
        setup.disable("bot")
    assert read_stage(setup.root, "bot")["stage"] == "stopping-ops"


def test_disable_of_one_feature_does_not_commit_while_remaining_material_differs(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    setup.enable("web")
    setup.hook = _crash_at("after_stage_uninstalled")
    with pytest.raises(SystemExit):
        setup.disable("bot")
    setup.paths.template_path.write_text(setup.paths.template_path.read_text() + "# edited\n")
    setup.hook = lambda _name: None

    with pytest.raises(SystemdError, match="uninstall_incomplete"):
        setup.disable("bot")
    assert read_stage(setup.root, "bot")["stage"] == "stopping-ops"
    assert setup.disable("bot").stage == "disabled"
    assert "# edited" not in setup.paths.template_path.read_text()


def test_disable_stops_before_touching_units_while_ops_unit_stays_active(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    unit = setup.paths.feature_unit("bot")
    systemd.stuck.add(unit)
    names: list[str] = []
    setup.hook = _recording(names)

    with pytest.raises(SystemdError, match="ops_still_active"):
        setup.disable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "enabled"
    assert saved["failure"] == "ops_still_active"
    assert "before_uninstall" not in names
    assert setup.paths.template_path.exists()


def test_existing_marker_keeps_its_start_time(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    path = enroll_marker_path(setup.root)
    path.parent.mkdir(parents=True)
    first = json.dumps({"feature": "bot", "started_at": "2000-01-01T00:00:00Z"}).encode() + b"\n"
    path.write_bytes(first)
    seen: list[bytes] = []

    def hook(name: str) -> None:
        if name == "after_marker":
            seen.append(path.read_bytes())

    setup.hook = hook
    assert setup.enable("bot").stage == "enabled"
    assert seen == [first]


def test_marker_reappearing_at_enrolled_is_removed_on_rerun(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.hook = _crash_at("after_stage_enrolled")
    with pytest.raises(SystemExit):
        setup.enable("bot")
    path = enroll_marker_path(setup.root)
    path.write_text(json.dumps({"feature": "bot", "started_at": "2026-10-10T00:00:00Z"}))

    names: list[str] = []
    setup.hook = _recording(names)
    assert setup.enable("bot").stage == "enabled"

    assert "before_unmark" in names
    assert not path.exists()


def test_ops_start_failure_after_afx_started_stops_afx_again(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)

    def ensure_active(unit: str) -> None:
        raise SystemdError("start")

    systemd.ensure_active = ensure_active
    with pytest.raises(SystemdError, match="start"):
        setup.enable("bot")

    assert "start:afx-test.service" in systemd.calls
    assert "afx-test.service" not in systemd.active
    assert read_stage(setup.root, "bot")["stage"] == "installed"


def test_disable_rejects_while_another_feature_has_an_unfinished_enable(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    systemd.fail.add("start")
    with pytest.raises(SystemdError):
        setup.enable("web")
    systemd.fail.clear()
    before = read_stage(setup.root, "bot")
    systemd.calls.clear()

    with pytest.raises(InvalidState, match="invalid_state"):
        setup.disable("bot")

    assert read_stage(setup.root, "bot") == before
    assert systemd.calls == []


def test_status_line_reports_unknown_unit_when_manager_cannot_be_asked(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)

    def show(unit: str, *props: str) -> dict[str, str]:
        raise SystemdError("show")

    systemd.show = show
    assert setup.status_line("bot") == "bot: disable disabled unit=unknown result=unknown"


def _systemctl_run(fail: dict[str, tuple[int, str]]):
    """systemctl の外部呼出しだけを置き換え、指定 subcommand を rc と stderr 付きで失敗させる。"""
    import subprocess

    def run(args, **_kwargs):
        action = args[2]
        if action in fail:
            rc, err = fail[action]
            return subprocess.CompletedProcess(args, rc, stdout="", stderr=err)
        out = "ActiveState=inactive\nResult=success\nLoadState=loaded\n" if action == "show" else ""
        return subprocess.CompletedProcess(args, 0 if action != "is-active" else 3, stdout=out, stderr="")
    return run


def test_systemd_stderr_is_kept_in_stage_file_and_shown_by_status(tmp_path, monkeypatch):
    from agentic_fx.ops_setup.systemd import Systemd

    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.systemd = Systemd(run=_systemctl_run({"stop": (1, "Failed to connect to bus: No medium found\n")}))

    with pytest.raises(SystemdError, match="stop"):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["failure"] == "stop"
    assert saved["failure_detail"] == "rc=1 stderr=Failed to connect to bus: No medium found"
    assert setup.status_line("bot").startswith(
        "bot: enable staged (stop; rc=1 stderr=Failed to connect to bus: No medium found) unit=")


def test_reaching_a_stage_clears_the_previous_failure_detail(tmp_path, monkeypatch):
    from agentic_fx.ops_setup.systemd import Systemd

    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    real = setup.systemd
    setup.systemd = Systemd(run=_systemctl_run({"stop": (1, "boom\n")}))
    with pytest.raises(SystemdError):
        setup.enable("bot")
    setup.systemd = real

    assert setup.enable("bot").stage == "enabled"

    saved = read_stage(setup.root, "bot")
    assert "failure" not in saved and "failure_detail" not in saved


def test_template_path_turned_into_directory_is_a_stage_mismatch(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.hook = _crash_at("after_stage_installed")
    with pytest.raises(SystemExit):
        setup.enable("bot")
    setup.paths.template_path.unlink()
    setup.paths.template_path.mkdir()

    setup.hook = _crash_at("before_install")
    with pytest.raises(SystemExit):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "enrolled"
    assert saved["failure"] == "stage_mismatch"


def test_enable_writes_absolute_ops_exec_resolved_at_install(tmp_path, monkeypatch):
    import dataclasses

    import agentic_fx.ops_setup.units as units

    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    found = setup.paths.ops_exec
    setup.paths = dataclasses.replace(setup.paths, ops_exec=None)
    monkeypatch.setattr(units, "_which", lambda name: found if name == "afx-ops" else None)

    assert setup.enable("bot").stage == "enabled"

    assert f"ExecStart={found} %i\n" in setup.paths.template_path.read_text()


def test_enable_without_resolvable_ops_exec_stays_staged_before_touching_afx(tmp_path, monkeypatch):
    import dataclasses

    import agentic_fx.ops_setup.units as units

    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.paths = dataclasses.replace(setup.paths, ops_exec=None)
    monkeypatch.setattr(units, "_which", lambda _name: None)
    monkeypatch.setattr(units, "_find_spec", lambda _name: None)

    with pytest.raises(SystemdError, match="ops_exec_not_found"):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "staged"
    assert saved["failure"] == "ops_exec_not_found"
    assert systemd.calls == []
    assert not enroll_marker_path(setup.root).exists()
    assert not setup.paths.unit_dir.exists()


def _stop_fails_after_afx_started(systemd: FakeSystemd) -> None:
    real_stop = systemd.stop

    def stop(unit: str) -> None:
        if f"start:{systemd.paths.afx_unit}" in systemd.calls:
            systemd.calls.append(f"stop:{unit}")
            raise SystemdError("stop", "rc=1 stderr=busy")
        real_stop(unit)

    systemd.stop = stop


def test_start_failure_keeps_its_reason_when_stopping_afx_also_fails(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)

    def ensure_active(unit: str) -> None:
        raise SystemdError("start", "rc=1 stderr=boom")

    systemd.ensure_active = ensure_active
    _stop_fails_after_afx_started(systemd)

    with pytest.raises(SystemdError, match="start") as caught:
        setup.enable("bot")

    assert caught.value.reason == "start"
    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "installed"
    assert saved["failure"] == "start"
    assert saved["failure_detail"].startswith("rc=1 stderr=boom")
    assert "stop: rc=1 stderr=busy" in saved["failure_detail"]


def test_check_failure_keeps_its_reason_when_stopping_afx_also_fails(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.ready = lambda _feature: False
    _stop_fails_after_afx_started(systemd)

    with pytest.raises(SystemdError, match="ops_not_ready"):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "installed"
    assert saved["failure"] == "ops_not_ready"
    assert "stop: rc=1 stderr=busy" in saved["failure_detail"]


def _dropin_write_fails_with_enospc(setup: OpsSetup, monkeypatch) -> None:
    import errno

    import agentic_fx.ops_setup.units as units

    real_write = units.atomic_write

    def atomic_write(path: Path, data: bytes, mode: int) -> None:
        if path == setup.paths.dropin_path:
            raise OSError(errno.ENOSPC, "No space left on device")
        real_write(path, data, mode)

    monkeypatch.setattr(units, "atomic_write", atomic_write)


def test_install_failure_records_reason_rolls_back_and_reloads_the_manager(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    _dropin_write_fails_with_enospc(setup, monkeypatch)

    with pytest.raises(OSError, match="No space left"):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "enrolled"
    assert saved["failure"] == "install_failed"
    assert "No space left on device" in saved["failure_detail"]
    assert not setup.paths.template_path.exists()
    assert systemd.calls[-1] == "daemon-reload"
    assert systemd.loaded_template is False


def test_install_failure_whose_rollback_also_fails_is_still_recorded(tmp_path, monkeypatch):
    import agentic_fx.ops_setup.units as units

    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    _dropin_write_fails_with_enospc(setup, monkeypatch)

    def remove_durably(path: Path) -> None:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(units, "remove_durably", remove_durably)

    with pytest.raises(OSError, match="No space left"):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "enrolled"
    assert saved["failure"] == "install_failed"
    assert "No space left on device" in saved["failure_detail"]
    assert "Permission denied" in saved["failure_detail"]
    assert systemd.calls[-1] == "daemon-reload"


def test_status_of_every_feature_names_the_marker_owner_and_path(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    path = enroll_marker_path(setup.root)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"feature": "bot", "started_at": "2026-10-10T00:00:00Z"}))

    for feature in ("bot", "web", "notify"):
        assert setup.status_line(feature).endswith(
            f" marker=enroll_in_progress feature=bot since=2026-10-10T00:00:00Z path={path}")

    path.write_bytes(b"{not json")
    assert setup.status_line("web").endswith(
        f" marker=enroll_in_progress feature=unknown since=unknown path={path}")


def test_enable_with_afx_stopped_beforehand_does_not_start_it(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    systemd.active.discard("afx-test.service")

    assert setup.enable("bot").stage == "enabled"

    assert not any(call.startswith("start:") for call in systemd.calls)
    assert systemd.active == set()
    saved = read_stage(setup.root, "bot")
    assert saved["afx_before_stop"] == "inactive"
    assert saved["committed"] is True
    assert setup.status_line("bot").endswith(
        "enable enabled unit=inactive result=success afx_before_enable=inactive ops=starts_with_afx")
    assert setup.enable("bot").stage == "enabled"


def test_afx_state_seen_before_the_first_stop_survives_a_crash_after_stopping(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.hook = _crash_at("after_stop")
    with pytest.raises(SystemExit):
        setup.enable("bot")
    assert "afx-test.service" not in systemd.active

    setup.hook = lambda _name: None
    assert setup.enable("bot").stage == "enabled"

    assert read_stage(setup.root, "bot")["afx_before_stop"] == "active"
    assert "afx-test.service" in systemd.active
    assert setup.paths.feature_unit("bot") in systemd.active
    assert "afx_before_enable" not in setup.status_line("bot")


def test_resume_at_started_with_afx_stopped_beforehand_does_not_fall_back(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    systemd.active.discard("afx-test.service")
    setup.hook = _crash_at("after_stage_started")
    with pytest.raises(SystemExit):
        setup.enable("bot")

    names: list[str] = []
    setup.hook = _recording(names)
    assert setup.enable("bot").stage == "enabled"

    assert "before_install" not in names
    assert not any(call.startswith("start:") for call in systemd.calls)


def test_disable_does_not_leave_enabled_when_the_stop_cannot_be_confirmed(tmp_path, monkeypatch):
    from agentic_fx.ops_setup.systemd import Systemd

    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    names: list[str] = []
    setup.hook = _recording(names)
    setup.systemd = Systemd(run=_systemctl_run({"is-active": (1, "Failed to connect to bus\n")}))

    with pytest.raises(SystemdError, match="unavailable"):
        setup.disable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "enabled"
    assert saved["failure"] == "unavailable"
    assert saved["failure_detail"] == "rc=1 stderr=Failed to connect to bus"
    assert "before_uninstall" not in names
    assert setup.paths.template_path.exists()


_GOOD_STAGE = {"operation": "enable", "stage": "installed", "feature": "bot", "principal": "ops-bot",
               "registered": True, "artifacts": [], "wants": ["bot"], "ops_active": False,
               "committed": False, "afx_before_stop": "active"}


@pytest.mark.parametrize("field, value", [
    ("operation", ["enable"]), ("operation", "rename"), ("stage", ["installed"]), ("stage", None),
    ("feature", {"bot": 1}), ("committed", "yes"), ("committed", 1), ("afx_before_stop", 5),
    ("afx_before_stop", ["active"]), ("registered", "no"), ("ops_active", 1), ("artifacts", "x"),
    ("wants", [1]), ("principal", 3), ("failure", 3), ("failure_detail", ["x"]),
])
def test_stage_file_with_wrong_typed_field_is_invalid_stage_not_a_crash(tmp_path, monkeypatch, field, value):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    path = _stage_file(setup.root)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({**_GOOD_STAGE, field: value}))
    before = path.read_bytes()

    for action in (setup.enable, setup.disable, setup.status):
        with pytest.raises(InvalidState, match="invalid_stage") as caught:
            action("bot")
        assert field in caught.value.detail

    assert path.read_bytes() == before
    assert systemd.calls == []


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", "\xff\xfe"])
def test_stage_file_that_is_not_a_json_object_is_invalid_stage(tmp_path, monkeypatch, content):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    path = _stage_file(setup.root)
    path.parent.mkdir(parents=True)
    path.write_bytes(content.encode("latin-1"))

    with pytest.raises(InvalidState, match="invalid_stage") as caught:
        setup.enable("bot")

    assert caught.value.detail
    assert systemd.calls == []


def test_run_ops_command_prints_invalid_stage_with_its_detail(tmp_path, monkeypatch, capsys):
    from agentic_fx.ops_setup import stages

    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    path = _stage_file(setup.root)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({**_GOOD_STAGE, "committed": "yes"}))

    assert stages.run_ops_command(setup.root, "status", "bot") == 1
    assert "afx ops: invalid_stage (" in capsys.readouterr().err


@pytest.mark.parametrize("content", [b'{"feature": ["bot"], "started_at": "x"}', b'{"feature": "bot"}', b"{"])
def test_status_line_names_a_corrupt_marker_owner_as_unknown(tmp_path, monkeypatch, content):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    path = enroll_marker_path(setup.root)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    assert setup.status_line("bot").endswith(
        f" marker=enroll_in_progress feature=unknown since=unknown path={path}")


def _replace_token(root: Path, principal: Principal, token: str) -> None:
    path = key_dir(root) / f"{principal.value}.token"
    path.write_text(token + "\n")
    path.chmod(0o600)


def test_enable_stops_when_the_enrolled_key_file_does_not_match_the_manifest(tmp_path, monkeypatch):
    from agentic_fx.ops.keys import enroll

    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    enroll(key_dir(setup.root), Principal.OPS_BOT)
    entry = _entry(setup.root, Principal.OPS_BOT)
    _replace_token(setup.root, Principal.OPS_BOT, "ab" * 32)

    with pytest.raises(InvalidState, match="principal_key_mismatch") as caught:
        setup.enable("bot")

    assert "afx keys rotate ops-bot" in caught.value.detail
    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "staged"
    assert saved["failure"] == "principal_key_mismatch"
    assert "afx keys rotate ops-bot" in saved["failure_detail"]
    # 鍵は触らず (再 enroll も revoke もしない)、afx も止めない。
    assert _entry(setup.root, Principal.OPS_BOT) == entry
    assert not any(call.startswith("stop:") for call in systemd.calls)
    assert not enroll_marker_path(setup.root).exists()


def test_resumed_enable_stops_when_the_key_was_swapped_after_enroll(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.hook = _crash_at("after_stage_enrolled")
    with pytest.raises(SystemExit):
        setup.enable("bot")
    setup.hook = lambda _name: None
    _replace_token(setup.root, Principal.OPS_BOT, "cd" * 32)
    systemd.calls.clear()

    with pytest.raises(InvalidState, match="principal_key_mismatch"):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "enrolled"
    assert saved["failure"] == "principal_key_mismatch"
    assert not any(call.startswith(("stop:", "start:")) for call in systemd.calls)
    assert not setup.paths.template_path.exists()


@pytest.mark.parametrize("broken", ["missing", "mode"])
def test_enable_treats_an_unusable_key_file_of_an_active_principal_as_mismatch(tmp_path, monkeypatch, broken):
    from agentic_fx.ops.keys import enroll

    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    enroll(key_dir(setup.root), Principal.OPS_BOT)
    path = key_dir(setup.root) / "ops-bot.token"
    if broken == "missing":
        path.unlink()
    else:
        path.chmod(0o644)

    with pytest.raises(InvalidState, match="principal_key_mismatch"):
        setup.enable("bot")

    assert not any(call.startswith("stop:") for call in systemd.calls)


def _mtime(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), path.stat().st_mtime_ns


def test_enabled_rerun_with_the_template_deleted_goes_back_and_reinstalls(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    setup.paths.template_path.unlink()
    setup.hook = _crash_at("before_install")

    with pytest.raises(SystemExit):
        setup.enable("bot")
    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "enrolled"
    assert saved["failure"] == "stage_mismatch"
    assert saved["committed"] is False

    setup.hook = lambda _name: None
    assert setup.enable("bot").stage == "enabled"
    assert setup.paths.template_path.exists()
    assert read_stage(setup.root, "bot")["committed"] is True


def test_enabled_rerun_with_the_ops_unit_stopped_goes_back_to_installed_and_starts_it(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    unit = setup.paths.feature_unit("bot")
    systemd.active.discard(unit)
    setup.hook = _crash_at("before_start")

    with pytest.raises(SystemExit):
        setup.enable("bot")
    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "installed"
    assert saved["failure"] == "stage_mismatch"

    setup.hook = lambda _name: None
    assert setup.enable("bot").stage == "enabled"
    assert unit in systemd.active


def test_enabled_rerun_after_afx_was_started_later_starts_the_missing_ops(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    systemd.active.discard(setup.paths.afx_unit)
    setup.enable("bot")
    # 利用者が後から afx だけを起こし、ops が起きていない。
    systemd.active.add(setup.paths.afx_unit)

    assert setup.enable("bot").stage == "enabled"

    assert setup.paths.feature_unit("bot") in systemd.active


def test_enabled_rerun_with_afx_and_ops_both_stopped_changes_nothing(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    systemd.active.clear()
    stage_path = _stage_file(setup.root)
    before = _mtime(stage_path)
    systemd.calls.clear()

    assert setup.enable("bot").stage == "enabled"

    assert _mtime(stage_path) == before
    assert not any(call.startswith(("start:", "stop:")) for call in systemd.calls)


def test_enabled_rerun_with_the_manager_no_longer_wanting_the_unit_reinstalls(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    real_reload = systemd.daemon_reload

    def reload_without_wants() -> None:
        real_reload()
        if not reloads:
            systemd.loaded_wants = []
        reloads.append(1)
    reloads: list[int] = []
    systemd.daemon_reload = reload_without_wants
    setup.hook = _crash_at("before_install")

    with pytest.raises(SystemExit):
        setup.enable("bot")

    assert read_stage(setup.root, "bot")["stage"] == "enrolled"


def test_enabled_rerun_with_its_own_marker_left_removes_it(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    path = enroll_marker_path(setup.root)
    path.write_text(json.dumps({"feature": "bot", "started_at": "2026-10-10T00:00:00Z"}))

    assert setup.enable("bot").stage == "enabled"

    assert not path.exists()


def test_enabled_rerun_that_cannot_ask_the_manager_records_why_and_keeps_the_stage(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    systemd.fail.add("daemon-reload")

    with pytest.raises(SystemdError, match="daemon-reload"):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "enabled"
    assert saved["failure"] == "daemon-reload"
    # 問い合わせ失敗で資材を戻さない。
    assert setup.paths.template_path.exists()
    assert setup.paths.dropin_path.exists()


def test_disabled_rerun_with_wants_left_in_the_dropin_goes_back_to_stopping_ops(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    setup.disable("bot")
    setup.paths.dropin_path.parent.mkdir(parents=True, exist_ok=True)
    setup.paths.dropin_path.write_text("[Unit]\nWants=afx-ops@bot.service\n")
    setup.hook = _crash_at("before_uninstall")

    with pytest.raises(SystemExit):
        setup.disable("bot")
    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "stopping-ops"
    assert saved["failure"] == "stage_mismatch"
    assert saved["committed"] is False

    setup.hook = lambda _name: None
    assert setup.disable("bot").stage == "disabled"
    assert not setup.paths.dropin_path.exists()
    assert read_stage(setup.root, "bot")["committed"] is True


def test_disabled_rerun_with_the_ops_unit_running_goes_back_to_stop_it(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    setup.disable("bot")
    unit = setup.paths.feature_unit("bot")
    systemd.active.add(unit)

    assert setup.disable("bot").stage == "disabled"

    assert f"stop:{unit}" in systemd.calls
    assert unit not in systemd.active


def test_disabled_rerun_without_drift_changes_nothing(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    setup.disable("bot")
    before = _mtime(_stage_file(setup.root))
    systemd.calls.clear()

    assert setup.disable("bot").stage == "disabled"

    assert _mtime(_stage_file(setup.root)) == before
    assert not any(call.startswith(("stop:", "start:")) for call in systemd.calls)


def _enospc():
    import errno
    return OSError(errno.ENOSPC, "No space left on device")


def test_marker_that_cannot_be_written_is_recorded_before_stopping_afx(tmp_path, monkeypatch):
    from agentic_fx.ops_setup import stages

    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    real_write = stages.atomic_write

    def atomic_write(path: Path, data: bytes, mode: int) -> None:
        if path == enroll_marker_path(setup.root):
            raise _enospc()
        real_write(path, data, mode)
    monkeypatch.setattr(stages, "atomic_write", atomic_write)

    with pytest.raises(SystemdError, match="marker_write_failed"):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "staged"
    assert saved["failure"] == "marker_write_failed"
    assert "No space left on device" in saved["failure_detail"]
    assert not any(call.startswith("stop:") for call in systemd.calls)
    assert setup.paths.afx_unit in systemd.active


def test_marker_that_cannot_be_removed_after_enroll_is_recorded_and_retried(tmp_path, monkeypatch):
    from agentic_fx.ops_setup import stages

    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    real_remove = stages.remove_durably

    def remove_durably(path: Path) -> None:
        if path == enroll_marker_path(setup.root):
            raise PermissionError(13, "Permission denied", str(path))
        real_remove(path)
    monkeypatch.setattr(stages, "remove_durably", remove_durably)

    with pytest.raises(SystemdError, match="marker_remove_failed"):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "stopped"
    assert saved["failure"] == "marker_remove_failed"
    assert "Permission denied" in saved["failure_detail"]
    assert enroll_marker_path(setup.root).exists()

    monkeypatch.setattr(stages, "remove_durably", real_remove)
    assert setup.enable("bot").stage == "enabled"
    assert not enroll_marker_path(setup.root).exists()


def _stage_write_fails_once(setup: OpsSetup, monkeypatch, stage: str) -> None:
    from agentic_fx.ops_setup import stages

    real_write = stages.atomic_write
    failed: list[int] = []

    def atomic_write(path: Path, data: bytes, mode: int) -> None:
        if (not failed and path == _stage_file(setup.root)
                and json.loads(data)["stage"] == stage):
            failed.append(1)
            raise _enospc()
        real_write(path, data, mode)
    monkeypatch.setattr(stages, "atomic_write", atomic_write)


def test_enable_stage_write_failure_is_recorded_as_material_write_failed(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    _stage_write_fails_once(setup, monkeypatch, "installed")

    with pytest.raises(SystemdError, match="material_write_failed"):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["failure"] == "material_write_failed"
    assert "No space left on device" in saved["failure_detail"]


def test_disable_stage_write_failure_is_recorded_as_material_write_failed(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    setup.enable("bot")
    _stage_write_fails_once(setup, monkeypatch, "stopping-ops")

    with pytest.raises(SystemdError, match="material_write_failed"):
        setup.disable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["failure"] == "material_write_failed"
    assert "No space left on device" in saved["failure_detail"]


def test_install_failure_keeps_install_failed_and_is_not_overwritten(tmp_path, monkeypatch):
    setup, _systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    _dropin_write_fails_with_enospc(setup, monkeypatch)

    with pytest.raises(OSError):
        setup.enable("bot")

    assert read_stage(setup.root, "bot")["failure"] == "install_failed"


def test_enable_with_an_unsafe_ops_exec_on_path_stays_staged_and_says_why(tmp_path, monkeypatch):
    import dataclasses

    import agentic_fx.ops_setup.units as units

    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    spaced = tmp_path / "my bin" / "afx-ops"
    spaced.parent.mkdir()
    spaced.write_text("#!/bin/sh\n")
    spaced.chmod(0o755)
    setup.paths = dataclasses.replace(setup.paths, ops_exec=None)
    monkeypatch.setattr(units, "_which", lambda _name: str(spaced))
    monkeypatch.setattr(units, "_find_spec", lambda _name: object())

    with pytest.raises(SystemdError, match="ops_exec_not_found"):
        setup.enable("bot")

    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "staged"
    assert saved["failure"] == "ops_exec_not_found"
    assert "空白" in saved["failure_detail"]
    assert systemd.calls == []


@pytest.mark.parametrize("value", ["", "1", "SIGTERM"])
def test_enable_warns_but_finishes_when_afx_lacks_restart_prevent_75(tmp_path, monkeypatch, value):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    systemd.restart_prevent = value

    assert setup.enable("bot").stage == "enabled"

    saved = read_stage(setup.root, "bot")
    assert saved["warn"] == "afx_restart_prevent_missing"
    assert " warn=afx_restart_prevent_missing" in setup.status_line("bot")


@pytest.mark.parametrize("value", ["75", "1 75 SIGTERM"])
def test_enable_does_not_warn_when_afx_keeps_75_from_restarting(tmp_path, monkeypatch, value):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    systemd.restart_prevent = value

    assert setup.enable("bot").stage == "enabled"

    assert "warn" not in read_stage(setup.root, "bot")
    assert "warn=" not in setup.status_line("bot")


# --- staged の入力検証 (feature 専用の config / secret と疎通 probe) ---

_BOT_VALUES = {"guild_id": 10**16, "channel_id": 10**16 + 1, "allowlist_user_ids": [10**16 + 2],
               "mention_user_ids": [10**16 + 2], "token": "secret-token-value"}


def _checked_setup(tmp_path: Path, monkeypatch, probe_results=None):
    """入力検査と probe を実物の経路に戻した OpsSetup。probe の実行は記録する。"""
    from agentic_fx.ops_setup.stages import _config_complete
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    calls: list[tuple[str, str]] = []
    results = list(probe_results or [])

    def probe(ops_exec: str, feature: str) -> tuple[int, str]:
        calls.append((ops_exec, feature))
        return results.pop(0) if results else (0, "")

    setup.config_check = _config_complete
    setup.probe = probe
    return setup, systemd, calls


def _bot_base(tmp_path: Path) -> Path:
    return tmp_path / "xdg" / "agentic-fx" / "ops" / "bot"


def _break_missing_config(tmp_path: Path) -> None:
    (_bot_base(tmp_path) / "config" / "ops.toml").unlink()


def _break_other_instance(tmp_path: Path) -> None:
    config = _bot_base(tmp_path) / "config" / "ops.toml"
    config.write_text(config.read_text().replace(str(tmp_path / "instance"), str(tmp_path / "other")))


def _break_toml(tmp_path: Path) -> None:
    (_bot_base(tmp_path) / "config" / "ops.toml").write_text("guild_id = [\n")


def _break_secret_missing(tmp_path: Path) -> None:
    (_bot_base(tmp_path) / "secret" / "discord.token").unlink()


def _break_secret_mode(tmp_path: Path) -> None:
    (_bot_base(tmp_path) / "secret" / "discord.token").chmod(0o644)


def _break_config_mode(tmp_path: Path) -> None:
    (_bot_base(tmp_path) / "config" / "ops.toml").chmod(0o666)


@pytest.mark.parametrize("break_it", [_break_missing_config, _break_other_instance, _break_toml,
                                      _break_secret_missing, _break_secret_mode, _break_config_mode])
def test_staged_stays_with_invalid_feature_config_and_creates_nothing(tmp_path, monkeypatch, break_it):
    from agentic_fx.ops_setup.feature_config import write_inputs
    setup, systemd, calls = _checked_setup(tmp_path, monkeypatch)
    write_inputs(setup.root, "bot", dict(_BOT_VALUES))
    break_it(tmp_path)
    manifest = (key_dir(setup.root) / ".ready").read_bytes()

    with pytest.raises(SystemdError) as raised:
        setup.enable("bot")

    assert raised.value.reason == "feature_config_invalid"
    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "staged" and saved["failure"] == "feature_config_invalid"
    assert calls == []
    assert systemd.calls == []
    assert not setup.paths.template_path.exists() and not setup.paths.dropin_path.exists()
    assert not enroll_marker_path(setup.root).exists()
    assert (key_dir(setup.root) / ".ready").read_bytes() == manifest
    assert "secret-token-value" not in json.dumps(saved)


@pytest.mark.parametrize(("rc", "word"), [(71, "token_invalid"), (73, "probe_bind_failed")])
def test_staged_stays_when_probe_fails_then_the_same_enable_resumes(tmp_path, monkeypatch, rc, word):
    from agentic_fx.ops_setup.feature_config import write_inputs
    setup, systemd, calls = _checked_setup(tmp_path, monkeypatch, probe_results=[(rc, word)])
    write_inputs(setup.root, "bot", dict(_BOT_VALUES))

    with pytest.raises(SystemdError) as raised:
        setup.enable("bot")

    assert raised.value.reason == f"feature_probe_failed:{word}"
    saved = read_stage(setup.root, "bot")
    assert saved["stage"] == "staged" and saved["failure"] == f"feature_probe_failed:{word}"
    assert len(calls) == 1 and calls[0][1] == "bot"
    assert systemd.calls == []
    assert not setup.paths.template_path.exists() and not enroll_marker_path(setup.root).exists()

    assert setup.enable("bot").stage == "enabled"

    assert len(calls) == 2
    assert "failure" not in read_stage(setup.root, "bot")


def test_staged_runs_probe_once_with_the_resolved_exec_and_passes(tmp_path, monkeypatch):
    from agentic_fx.ops_setup.feature_config import write_inputs
    setup, _systemd, calls = _checked_setup(tmp_path, monkeypatch)
    write_inputs(setup.root, "bot", dict(_BOT_VALUES))

    assert setup.enable("bot").stage == "enabled"
    assert setup.enable("bot").stage == "enabled"

    assert calls == [(str(setup.paths.ops_exec), "bot")]


def test_probe_runner_keeps_only_fixed_reason_words(tmp_path):
    from agentic_fx.ops_setup.probe import run_probe
    script = tmp_path / "afx-ops"
    script.write_text('#!/bin/sh\ncase "$2" in\n  bot) echo "afx-ops: config_invalid" >&2; exit 71;;\n'
                      '  web) echo "boom /secret/path token=abc" >&2; exit 73;;\n  *) exit 0;;\nesac\n')
    script.chmod(0o755)

    assert run_probe(str(script), "bot") == (71, "config_invalid")
    assert run_probe(str(script), "web") == (73, "feature_probe_unreadable")
    assert run_probe(str(script), "notify")[0] == 0
    assert run_probe(str(tmp_path / "missing"), "bot") == (70, "runtime_unavailable")


def _assert_all_off(setup: OpsSetup, systemd: FakeSystemd) -> None:
    unit_dir = setup.paths.unit_dir
    assert not setup.paths.template_path.exists()
    assert not setup.paths.dropin_path.exists()
    assert not list(unit_dir.glob("afx-ops*"))
    assert not [name for name in systemd.active if name.startswith(setup.paths.ops_prefix)]
    assert setup._active_features() == set()


def test_all_off_has_no_template_dropin_or_feature_unit(tmp_path, monkeypatch):
    setup, systemd, home = _setup(tmp_path)
    _with_home(home, monkeypatch)
    # 新規 instance: enable を呼ばない (対話で「あとで」を選んだ場合も同じ)。
    _assert_all_off(setup, systemd)
    assert read_stage(setup.root, "bot", missing_ok=True) is None

    # 検証で止まった enable も資材を残さない。
    setup.config_check = lambda _root, _feature: False
    with pytest.raises(SystemdError):
        setup.enable("bot")
    _assert_all_off(setup, systemd)
    setup.config_check = lambda _root, _feature: True

    # 最後の feature を disable した後。feature の鍵 (principal) は消さない。
    setup.enable("bot")
    setup.enable("web")
    digest = _entry(setup.root, Principal.OPS_BOT)["sha256"]
    setup.disable("bot")
    assert setup.paths.template_path.exists()
    setup.disable("web")
    _assert_all_off(setup, systemd)
    assert _entry(setup.root, Principal.OPS_BOT)["sha256"] == digest
    # uninstall_feature を空の集合で呼んでも何も作らない。
    from agentic_fx.ops_setup.units import uninstall_feature
    uninstall_feature(setup.paths, set())
    _assert_all_off(setup, systemd)
