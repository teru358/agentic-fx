from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agentic_fx.ops_setup.units import UnitPaths, install_units, uninstall_feature

_EXEC = "/opt/afx/bin/afx-ops"


def test_install_units_renders_notify_template_and_aggregated_wants(tmp_path):
    paths = UnitPaths(unit_dir=tmp_path / "units", afx_unit="afx-sample.service",
                      ops_exec=_EXEC)

    install_units(paths, {"bot", "web"})

    template = paths.template_path.read_text()
    assert "Type=notify" in template
    assert "Requisite=afx-sample.service" in template
    assert "BindsTo" not in template
    assert "PartOf=afx-sample.service" in template
    assert "After=afx-sample.service" in template
    assert "Restart=on-failure" in template
    assert "RestartSec=5s" in template
    assert "StartLimitIntervalSec=60s" in template
    assert "StartLimitBurst=3" in template
    assert "NoNewPrivileges=yes" in template
    assert "KillMode=control-group" in template
    assert "/bin/sh" not in template
    unit_section, service_section = template.split("[Service]", maxsplit=1)
    assert "StartLimitIntervalSec=60s" in unit_section
    assert "StartLimitBurst=3" in unit_section
    assert "StartLimitIntervalSec" not in service_section
    assert "StartLimitBurst" not in service_section
    dropin = paths.dropin_path.read_text()
    assert "Wants=afx-ops@bot.service afx-ops@web.service" in dropin


def test_uninstall_last_feature_removes_template_and_dropin(tmp_path):
    paths = UnitPaths(unit_dir=tmp_path / "units", afx_unit="afx.service", ops_exec=_EXEC)
    install_units(paths, {"bot"})

    uninstall_feature(paths, set())

    assert not paths.template_path.exists()
    assert not paths.dropin_path.exists()


def test_unit_write_ignores_symlink_planted_at_predictable_temp_name(tmp_path):
    paths = UnitPaths(unit_dir=tmp_path / "units", afx_unit="afx-sample.service",
                      ops_exec=_EXEC)
    paths.unit_dir.mkdir()
    paths.dropin_path.parent.mkdir()
    victims = []
    for target in (paths.template_path, paths.dropin_path):
        victim = tmp_path / f"victim-{target.name}"
        victim.write_bytes(b"untouched")
        target.with_name(f".{target.name}.tmp").symlink_to(victim)
        victims.append(victim)

    install_units(paths, {"bot"})

    assert [v.read_bytes() for v in victims] == [b"untouched", b"untouched"]
    assert "Type=notify" in paths.template_path.read_text()
    assert paths.dropin_path.read_text() == "[Unit]\nWants=afx-ops@bot.service\n"
    assert not paths.template_path.is_symlink() and not paths.dropin_path.is_symlink()
    assert stat.S_IMODE(paths.template_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(paths.dropin_path.stat().st_mode) == 0o644


def test_template_is_package_data_of_ops_setup():
    from importlib import resources

    template = resources.files("agentic_fx.ops_setup").joinpath("afx-ops@.service.in").read_text()

    assert "Type=notify" in template
    assert "@AFX_UNIT@" in template and "@AFX_OPS_EXEC@" in template


def test_unit_write_fsyncs_the_file_and_its_directory(tmp_path, monkeypatch):
    import os

    import agentic_fx.ops_setup.units as units

    kinds: list[str] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        kinds.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(units.os, "fsync", fsync)
    paths = UnitPaths(unit_dir=tmp_path / "units", afx_unit="afx-sample.service",
                      ops_exec=_EXEC)

    install_units(paths, {"bot"})

    # template と drop-in のそれぞれで、中身を fsync してから rename 後の dir を fsync する。
    assert kinds == ["file", "dir", "file", "dir"]


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    import subprocess

    def run(args, **_kwargs):
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)
    return run


def test_systemd_show_keeps_missing_properties_empty_and_values_with_equals():
    from agentic_fx.ops_setup.systemd import Systemd

    systemd = Systemd(run=_completed("LoadState=loaded\nExecStart={ path=/x ; argv[]=/x a=b }\n"))

    assert systemd.show("u.service", "LoadState", "ActiveState", "ExecStart") == {
        "LoadState": "loaded", "ActiveState": "", "ExecStart": "{ path=/x ; argv[]=/x a=b }"}


def test_systemd_nonzero_exit_raises_with_the_action_as_reason():
    import pytest

    from agentic_fx.ops_setup.systemd import Systemd, SystemdError

    systemd = Systemd(run=_completed(returncode=1))
    for call, reason in ((lambda: systemd.stop("u.service"), "stop"),
                         (lambda: systemd.start("u.service"), "start"),
                         (systemd.daemon_reload, "daemon-reload"),
                         (lambda: systemd.show("u.service", "LoadState"), "show")):
        with pytest.raises(SystemdError) as caught:
            call()
        assert caught.value.reason == reason


def test_systemd_failure_keeps_fixed_reason_and_carries_return_code_and_stderr_tail():
    import pytest

    from agentic_fx.ops_setup.systemd import Systemd, SystemdError

    stderr = "x" * 300 + "\nFailed to start u.service: Unit u.service not found.\n"
    systemd = Systemd(run=_completed(returncode=5, stderr=stderr))
    with pytest.raises(SystemdError) as caught:
        systemd.start("u.service")

    assert caught.value.reason == "start"
    detail = caught.value.detail
    assert detail.startswith("rc=5 stderr=")
    tail = detail[len("rc=5 stderr="):]
    assert "\n" not in detail
    assert tail.endswith("Failed to start u.service: Unit u.service not found.")
    assert len(tail.encode()) <= 200


def test_systemd_stderr_tail_cut_inside_a_multibyte_character_does_not_raise():
    import pytest

    from agentic_fx.ops_setup.systemd import Systemd, SystemdError

    systemd = Systemd(run=_completed(returncode=1, stderr="あ" * 150))
    with pytest.raises(SystemdError) as caught:
        systemd.stop("u.service")

    assert caught.value.reason == "stop"
    assert len(caught.value.detail.split("stderr=", 1)[1].encode()) <= 200


def test_systemd_that_cannot_be_executed_becomes_unavailable(tmp_path, monkeypatch):
    import pytest

    from agentic_fx.ops_setup.systemd import Systemd, SystemdError

    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    systemd = Systemd()
    for call in (lambda: systemd.stop("u.service"), lambda: systemd.is_active("u.service"),
                 lambda: systemd.show("u.service", "LoadState"), systemd.daemon_reload):
        with pytest.raises(SystemdError) as caught:
            call()
        assert caught.value.reason == "unavailable"
        assert "FileNotFoundError" in caught.value.detail


def test_units_match_treats_unreadable_material_as_mismatch(tmp_path):
    from agentic_fx.ops_setup.units import units_match

    paths = UnitPaths(unit_dir=tmp_path / "units", afx_unit="afx-sample.service",
                      ops_exec=_EXEC)
    install_units(paths, {"bot"})
    paths.template_path.unlink()
    paths.template_path.mkdir()

    assert units_match(paths, {"bot"}) is False


def _executable(path: Path, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(mode)
    return path


def test_resolve_ops_exec_uses_absolute_path_found_on_path(tmp_path, monkeypatch):
    import agentic_fx.ops_setup.units as units

    found = _executable(tmp_path / "bin" / "afx-ops")
    monkeypatch.setattr(units, "_which", lambda name: str(found) if name == "afx-ops" else None)
    monkeypatch.setattr(units, "_find_spec", lambda _name: None)

    resolved = units.resolve_ops_exec(UnitPaths(unit_dir=Path("/u")))

    assert resolved.ops_exec == str(found)


def test_resolve_ops_exec_falls_back_to_running_python_with_module(monkeypatch):
    import sys

    import agentic_fx.ops_setup.units as units

    monkeypatch.setattr(units, "_which", lambda _name: None)
    monkeypatch.setattr(units, "_find_spec", lambda name: object() if name == "afx_ops" else None)

    resolved = units.resolve_ops_exec(UnitPaths(unit_dir=Path("/u")))

    assert resolved.ops_exec == f"{sys.executable} -m afx_ops"
    assert Path(sys.executable).is_absolute()


def test_resolve_ops_exec_fails_when_nothing_is_found_or_given_relative(tmp_path, monkeypatch):
    import pytest

    import agentic_fx.ops_setup.units as units

    monkeypatch.setattr(units, "_which", lambda _name: "/should/not/be/used")
    monkeypatch.setattr(units, "_find_spec", lambda _name: object())
    with pytest.raises(units.OpsExecNotFound):
        units.resolve_ops_exec(UnitPaths(unit_dir=Path("/u"), ops_exec="afx-ops"))
    monkeypatch.setattr(units, "_which", lambda _name: None)
    monkeypatch.setattr(units, "_find_spec", lambda _name: None)
    with pytest.raises(units.OpsExecNotFound):
        units.resolve_ops_exec(UnitPaths(unit_dir=Path("/u")))
    given = UnitPaths(unit_dir=Path("/u"), ops_exec=str(_executable(tmp_path / "x" / "afx-ops")))
    assert units.resolve_ops_exec(given) == given


def test_unresolved_ops_exec_is_never_rendered(tmp_path):
    import pytest

    from agentic_fx.ops_setup.units import OpsExecNotFound

    with pytest.raises(OpsExecNotFound):
        install_units(UnitPaths(unit_dir=tmp_path / "units"), {"bot"})
    assert not (tmp_path / "units").exists()


def test_is_stopped_only_when_the_manager_reports_a_stopped_unit():
    from agentic_fx.ops_setup.systemd import Systemd

    for stdout, rc in (("inactive\n", 3), ("failed\n", 3), ("unknown\n", 3), ("inactive\n", 4), ("", 3)):
        systemd = Systemd(run=_completed(stdout=stdout, returncode=rc))
        assert systemd.is_stopped("u.service") is True
        assert systemd.is_active("u.service") is False
    # 止まりきっていない unit は「止まった」とも「動いている」とも扱わない。
    for stdout in ("deactivating\n", "activating\n"):
        systemd = Systemd(run=_completed(stdout=stdout, returncode=3))
        assert systemd.is_stopped("u.service") is False
        assert systemd.is_active("u.service") is False
    systemd = Systemd(run=_completed(stdout="active\n", returncode=0))
    assert systemd.is_active("u.service") is True
    assert systemd.is_stopped("u.service") is False


def test_is_active_that_cannot_ask_the_manager_is_unavailable_not_stopped():
    import pytest

    from agentic_fx.ops_setup.systemd import Systemd, SystemdError

    systemd = Systemd(run=_completed(returncode=1, stderr="Failed to connect to bus: No medium found\n"))
    for call in (systemd.is_active, systemd.is_stopped):
        with pytest.raises(SystemdError) as caught:
            call("u.service")
        assert caught.value.reason == "unavailable"
        assert caught.value.detail == "rc=1 stderr=Failed to connect to bus: No medium found"


def test_systemd_stderr_tail_redacts_secret_looking_text_before_cutting():
    import pytest

    from agentic_fx.ops_setup.systemd import Systemd, SystemdError

    secret = "deadbeef" * 8
    # 長い前置きで token= の直前まで押し出し、切った後に秘密の値だけが残らないことも確かめる。
    stderr = "x" * 300 + f" unit parse error token={secret} at https://host/p?apikey={secret}\n"
    systemd = Systemd(run=_completed(returncode=1, stderr=stderr))
    with pytest.raises(SystemdError) as caught:
        systemd.stop("u.service")

    assert secret not in caught.value.detail
    assert "token=***" in caught.value.detail
    assert "<url>" in caught.value.detail


def test_systemd_stderr_tail_does_not_leave_a_bare_secret_when_the_prefix_is_cut():
    import pytest

    from agentic_fx.ops_setup.systemd import Systemd, SystemdError

    secret = "c0ffee00" * 24
    systemd = Systemd(run=_completed(returncode=1, stderr=f"token={secret}\n"))
    with pytest.raises(SystemdError) as caught:
        systemd.stop("u.service")

    assert "c0ffee00" not in caught.value.detail


def test_ensure_active_waits_for_an_activating_unit_through_start():
    import subprocess

    from agentic_fx.ops_setup.systemd import Systemd

    calls: list[str] = []

    def run(args, **_kwargs):
        calls.append(args[2])
        out = "activating\n" if args[2] == "is-active" else ""
        return subprocess.CompletedProcess(args, 3 if args[2] == "is-active" else 0, stdout=out, stderr="")

    Systemd(run=run).ensure_active("u.service")

    # Wants で先に引かれ起動途中の ops も、start (起動完了まで待つ) を通す。
    assert calls == ["is-active", "start"]


def _resolve_found(monkeypatch, found: Path | str):
    import agentic_fx.ops_setup.units as units

    monkeypatch.setattr(units, "_which", lambda name: str(found) if name == "afx-ops" else None)
    monkeypatch.setattr(units, "_find_spec", lambda _name: object())
    return units.resolve_ops_exec(UnitPaths(unit_dir=Path("/u")))


@pytest.mark.parametrize("name, word", [
    ("afx ops", "空白"), ("afx\nops", "空白"), ("afx\tops", "空白"), ("afx%iops", "%"), ("afx$HOME", "$"),
    ('afx"ops', "引用符"), ("afx'ops", "引用符"), ("afx\\ops", "\\"),
])
def test_resolve_ops_exec_rejects_paths_that_systemd_would_split_or_expand(tmp_path, monkeypatch, name, word):
    import agentic_fx.ops_setup.units as units

    found = _executable(tmp_path / name / "afx-ops")
    with pytest.raises(units.OpsExecNotFound) as caught:
        _resolve_found(monkeypatch, found)

    # PATH 上の怪しい afx-ops を見つけたら、module 起動へ黙って逃げず理由を返す。
    assert word in str(caught.value)


def test_resolve_ops_exec_rejects_a_file_others_can_write(tmp_path, monkeypatch):
    import agentic_fx.ops_setup.units as units

    found = _executable(tmp_path / "bin" / "afx-ops", 0o757)
    with pytest.raises(units.OpsExecNotFound, match="他者が書き込める"):
        _resolve_found(monkeypatch, found)


def test_resolve_ops_exec_accepts_a_group_writable_file_of_its_own(tmp_path, monkeypatch):
    found = _executable(tmp_path / "bin" / "afx-ops", 0o775)

    assert _resolve_found(monkeypatch, found).ops_exec == str(found)


def test_resolve_ops_exec_rejects_a_symlink_whose_target_belongs_to_another_uid(tmp_path, monkeypatch):
    import agentic_fx.ops_setup.units as units

    target = _executable(tmp_path / "other" / "afx-ops")
    link = tmp_path / "bin" / "afx-ops"
    link.parent.mkdir()
    link.symlink_to(target)
    # 実体 (tmp の file) を自 uid でも root でもない持ち主に見せる。
    foreign = os.getuid() + 4242
    monkeypatch.setattr(units.os, "getuid", lambda: foreign)
    assert target.stat().st_uid not in (foreign, 0)

    with pytest.raises(units.OpsExecNotFound, match="所有者"):
        _resolve_found(monkeypatch, link)


@pytest.mark.skipif(not Path("/usr/bin/python3").exists()
                    or Path("/usr/bin/python3").stat().st_uid != 0,
                    reason="root 所有の /usr/bin/python3 が無い")
def test_resolve_ops_exec_accepts_a_root_owned_system_python_for_the_module_form(monkeypatch):
    import sys

    import agentic_fx.ops_setup.units as units

    monkeypatch.setattr(units, "_which", lambda _name: None)
    monkeypatch.setattr(units, "_find_spec", lambda name: object() if name == "afx_ops" else None)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")

    resolved = units.resolve_ops_exec(UnitPaths(unit_dir=Path("/u")))

    assert resolved.ops_exec == "/usr/bin/python3 -m afx_ops"


def test_resolve_ops_exec_rejects_a_root_owned_file_others_can_write(tmp_path, monkeypatch):
    import agentic_fx.ops_setup.units as units

    found = _executable(tmp_path / "bin" / "afx-ops", 0o757)
    real_stat = units.os.stat

    def stat_as_root(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if Path(path) != found:
            return info
        # 一般 uid では root 所有の file を作れないので、持ち主だけを root に見せる。
        fields = list(info)
        fields[4] = 0
        return os.stat_result(fields)
    monkeypatch.setattr(units.os, "stat", stat_as_root)

    with pytest.raises(units.OpsExecNotFound, match="他者が書き込める"):
        _resolve_found(monkeypatch, found)


@pytest.mark.parametrize("kind", ["missing", "directory", "not_executable"])
def test_resolve_ops_exec_rejects_what_cannot_be_run(tmp_path, monkeypatch, kind):
    import agentic_fx.ops_setup.units as units

    found = tmp_path / "bin" / "afx-ops"
    if kind == "directory":
        found.mkdir(parents=True)
    elif kind == "not_executable":
        _executable(found, 0o644)
    with pytest.raises(units.OpsExecNotFound):
        _resolve_found(monkeypatch, found)


def test_resolve_ops_exec_checks_the_running_python_for_the_module_form(tmp_path, monkeypatch):
    import sys

    import agentic_fx.ops_setup.units as units

    monkeypatch.setattr(units, "_which", lambda _name: None)
    monkeypatch.setattr(units, "_find_spec", lambda name: object() if name == "afx_ops" else None)
    spaced = _executable(tmp_path / "my env" / "python")
    monkeypatch.setattr(sys, "executable", str(spaced))

    with pytest.raises(units.OpsExecNotFound, match="空白"):
        units.resolve_ops_exec(UnitPaths(unit_dir=Path("/u")))


@pytest.mark.parametrize("given", ["{exe} -m afx_ops", "{exe}"])
def test_resolve_ops_exec_checks_a_given_command_the_same_way(tmp_path, given):
    import pytest

    import agentic_fx.ops_setup.units as units

    good = _executable(tmp_path / "bin" / "python")
    paths = UnitPaths(unit_dir=Path("/u"), ops_exec=given.format(exe=good))
    assert units.resolve_ops_exec(paths) == paths

    writable = _executable(tmp_path / "w" / "python", 0o757)
    with pytest.raises(units.OpsExecNotFound):
        units.resolve_ops_exec(UnitPaths(unit_dir=Path("/u"), ops_exec=given.format(exe=writable)))
    for bad in (f"{good} -m afx_ops\nExecStartPre=/bin/sh", f"{good} %i", f"{good} -m other"):
        with pytest.raises(units.OpsExecNotFound):
            units.resolve_ops_exec(UnitPaths(unit_dir=Path("/u"), ops_exec=bad))


def test_rendering_refuses_an_exec_string_systemd_would_split(tmp_path):
    from agentic_fx.ops_setup.units import OpsExecNotFound

    with pytest.raises(OpsExecNotFound):
        install_units(UnitPaths(unit_dir=tmp_path / "units", ops_exec="/opt/afx/bin/afx-ops\nExecStartPre=/x"),
                      {"bot"})
    assert not (tmp_path / "units").exists()


def test_stages_comments_no_longer_claim_bindsto():
    # unit は Requisite= + PartOf= + After= で afx に結ぶ。段の説明が古い依存を前提にしない
    import agentic_fx.ops_setup.stages as stages
    test_stages = Path(__file__).with_name("test_stages.py")
    assert "BindsTo" not in Path(stages.__file__).read_text(encoding="utf-8")
    assert "BindsTo" not in test_stages.read_text(encoding="utf-8")
