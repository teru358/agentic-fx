"""unit の生成・設置・差分・退避と linger。実 systemd には触れない。"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_fx.config import load_settings
from agentic_fx.setup import unit as unit_mod
from agentic_fx.setup.preflight import CheckReport
from agentic_fx.setup.unit import (
    InstallResult, PlacementResult, UnitBlocked, UnitValues, default_unit_dir, detect_unit_values,
    enable_linger, install_unit, linger_state, place_unit, render_unit,
    run_check_in_unit_env, systemctl_available, unit_record_values,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "afx.service.direct"
EXAMPLE = REPO / "config" / "settings.yaml.example"
AUTHOR_HOME = "/home/teru358"


CLEAN_REPORT = json.dumps({"fatal": [], "warning": []})


def report_json(fatal=(), warning=()) -> str:
    return json.dumps({"fatal": list(fatal), "warning": list(warning)})


class Calls:
    """引数を記録し、argv の先頭ごとに返す結果を差し替えられる fake の runner。"""

    def __init__(self, results=None, stdout=None):
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.results = results or {}
        self.stdout = stdout or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        key = " ".join(argv[:2])
        rc = self.results.get(key, 0)
        if isinstance(rc, Exception):
            raise rc
        default = CLEAN_REPORT if key == "env -i" else ""
        return SimpleNamespace(returncode=rc, stdout=self.stdout.get(key, default), stderr="")

    def systemctl_calls(self):
        return [c for c in self.calls if c[0] == "systemctl"]


@pytest.fixture
def settings():
    return load_settings(EXAMPLE)


def make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("")
    return root


def settings_with(settings, **backends):
    runner = settings.runner.model_copy(update={
        "trade": settings.runner.trade.model_copy(update={"backend": backends.get("trade", "local")}),
        "improve": settings.runner.improve.model_copy(update={"backend": backends.get("improve", "local")}),
    })
    return settings.model_copy(update={"runner": runner})


def values_for(root, **kw):
    return UnitValues(root, root / ".venv" / "bin" / "python",
                      kw.get("path_dirs", ("/usr/local/bin", "/usr/bin", "/bin")),
                      kw.get("lang", "C.UTF-8"), kw.get("bridge", False))


# --- 固定行 ---

def _lines(text: str, *, skip_prefixes=()):
    return [l for l in text.splitlines()
            if l.strip() and not l.startswith("#") and not l.startswith(skip_prefixes)]


_VALUE_LINES = ("WorkingDirectory=", "ExecStart=", "Environment=LANG=", "Environment=PATH=",
                "RestartPreventExitStatus=")


def test_fixed_lines_match_the_provisional_production_unit(tmp_path):
    root = make_repo(tmp_path)
    rendered = render_unit(root, root / ".venv" / "bin" / "python",
                           ["/x/bin", "/usr/bin"], "ja_JP.UTF-8", True)
    assert (_lines(rendered, skip_prefixes=_VALUE_LINES)
            == _lines(FIXTURE.read_text(encoding="utf-8"), skip_prefixes=_VALUE_LINES))
    assert "RestartPreventExitStatus=75 76" in rendered


def test_unit_has_the_required_lines_and_no_screen_remnants(tmp_path):
    root = make_repo(tmp_path)
    python = root / ".venv" / "bin" / "python"
    text = render_unit(root, python, ["/usr/bin"], "C.UTF-8", False)
    for line in ("Type=exec", f"ExecStart={python} main.py --daemon", "Delegate=pids memory",
                 "RestartPreventExitStatus=75 76", "TimeoutStopSec=630",
                 f"WorkingDirectory={root}"):
        assert line in text.splitlines()
    for forbidden in ("ExecStop", "screen", "afx-start", "EnvironmentFile"):
        assert forbidden not in text


def test_bridge_lines_only_when_the_bridge_unit_exists(tmp_path):
    root = make_repo(tmp_path)
    python = root / ".venv" / "bin" / "python"
    without = render_unit(root, python, ["/usr/bin"], "C.UTF-8", False)
    assert "After=network-online.target\n" in without and "mt5-bridge" not in without
    assert "After=network-online.target mt5-bridge.service\n" in render_unit(
        root, python, ["/usr/bin"], "C.UTF-8", True)


# --- 値の検出 (AC-15) ---

def test_values_contain_no_author_path_or_nvm_version(tmp_path, settings):
    root = make_repo(tmp_path)
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("")
    s = settings_with(settings, trade="claude")
    values = detect_unit_values(root, s, which=lambda n: str(claude) if n == "claude" else None,
                                env={"LANG": "ja_JP.UTF-8"}, unit_dir=tmp_path / "units")
    assert isinstance(values, UnitValues)
    text = render_unit(values.root, values.python, values.path_dirs, values.lang,
                       values.bridge_unit_present)
    assert AUTHOR_HOME not in text and "v24" not in text and ".nvm" not in text
    assert f"WorkingDirectory={root}" in text.splitlines()
    path_line = next(l for l in text.splitlines() if l.startswith("Environment=PATH="))
    assert path_line == f"Environment=PATH={claude.parent}:/usr/local/bin:/usr/bin:/bin"
    assert values.attributes == ()
    assert values.lang == "ja_JP.UTF-8"


def test_only_selected_backends_contribute_path_dirs(tmp_path, settings):
    root = make_repo(tmp_path)
    seen: list[str] = []

    def which(name):
        seen.append(name)
        return f"/opt/{name}/bin/{name}"

    values = detect_unit_values(root, settings_with(settings, improve="codex"), which=which,
                                env={}, unit_dir=tmp_path / "u")
    assert isinstance(values, UnitValues)
    assert values.path_dirs[0].startswith("/") and "claude" not in " ".join(values.path_dirs)
    assert values.lang == "C.UTF-8"


def test_nvm_path_is_a_note_and_not_hardcoded(tmp_path, settings):
    root = make_repo(tmp_path)
    nvm_bin = "/h/.nvm/versions/node/v99.0.0/bin"
    values = detect_unit_values(root, settings_with(settings, trade="claude"),
                                which=lambda n: f"{nvm_bin}/claude", env={},
                                unit_dir=tmp_path / "u")
    assert values.attributes == ("nvm_path",)
    assert nvm_bin in values.path_dirs


def test_bridge_unit_is_detected_in_the_unit_dir(tmp_path, settings):
    root = make_repo(tmp_path)
    units = tmp_path / "u"
    units.mkdir()
    (units / "mt5-bridge.service").write_text("")
    values = detect_unit_values(root, settings, env={}, unit_dir=units)
    assert values.bridge_unit_present is True


def test_default_unit_dir_honours_xdg_config_home(tmp_path):
    assert default_unit_dir({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "systemd" / "user"
    assert default_unit_dir({"HOME": str(tmp_path)}) == tmp_path / ".config" / "systemd" / "user"


# --- 置かない条件 (AC-22) ---

def test_missing_venv_python_blocks_without_systemctl(tmp_path, settings):
    root = tmp_path / "repo"
    root.mkdir()
    blocked = detect_unit_values(root, settings, env={}, unit_dir=tmp_path / "u")
    assert isinstance(blocked, UnitBlocked) and blocked.reason_code == "python_missing"
    assert "uv sync" in blocked.message
    run, check = Calls(), Calls()
    result = place_unit(root, settings, unit_dir=tmp_path / "u", confirm=lambda d: True,
                        run=run, check_run=check, env={})
    assert result.state == "blocked"
    assert run.calls == [] and check.calls == []
    assert not (tmp_path / "u" / "afx.service").exists()


@pytest.mark.parametrize("name", ["with space", "q'uote", 'd"q', "per%cent", "dol$lar", "back\\slash", "ctl\x01"])
def test_unsafe_characters_in_root_block_the_unit(tmp_path, settings, name):
    root = tmp_path / name
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("")
    blocked = detect_unit_values(root, settings, env={}, unit_dir=tmp_path / "u")
    assert isinstance(blocked, UnitBlocked) and blocked.reason_code == "unsafe_path_chars"
    run = Calls()
    result = place_unit(root, settings, unit_dir=tmp_path / "u", confirm=lambda d: True,
                        run=run, check_run=Calls(), env={})
    assert result.state == "blocked" and run.calls == []
    assert not (tmp_path / "u").exists()


# --- afx check と設置 (AC-16 / AC-16a) ---

@pytest.mark.parametrize("fatal", ["settings_invalid", "mission_ceiling_violation",
                                   "cli_not_elf", "secret_env_present"])
def test_fatal_check_leaves_no_unit_and_calls_no_systemctl(tmp_path, settings, fatal):
    root = make_repo(tmp_path)
    units = tmp_path / "u"
    run = Calls()
    check = Calls(stdout={"env -i": report_json(fatal=[fatal])}, results={"env -i": 1})
    result = place_unit(root, settings, unit_dir=units, confirm=lambda d: True,
                        run=run, check_run=check, env={"HOME": str(tmp_path)})
    assert result.state == "check_failed" and result.reason_code == fatal
    assert run.systemctl_calls() == []
    assert not (units / "afx.service").exists()


@pytest.mark.parametrize("warning", ["bin_not_found", "credentials_missing", "deferred_by_user",
                                     "user_declined_probe", "model_not_found"])
def test_warning_only_check_still_installs_the_unit(tmp_path, settings, warning):
    root = make_repo(tmp_path)
    units = tmp_path / "u"
    run = Calls()
    check = Calls(stdout={"env -i": report_json(warning=[warning])})
    result = place_unit(root, settings, unit_dir=units, confirm=lambda d: True,
                        run=run, check_run=check, env={"HOME": str(tmp_path)})
    assert result.state == "installed" and result.report.warning == [warning]
    assert (units / "afx.service").is_file()
    assert [c[:3] for c in run.systemctl_calls()] == [
        ["systemctl", "--user", "show-environment"], ["systemctl", "--user", "daemon-reload"]]


def test_unit_is_not_enabled_or_started(tmp_path, settings):
    root = make_repo(tmp_path)
    run = Calls()
    place_unit(root, settings, unit_dir=tmp_path / "u", confirm=lambda d: True, run=run,
               check_run=Calls(), env={"HOME": str(tmp_path)})
    verbs = {c[2] for c in run.systemctl_calls()}
    assert verbs <= {"show-environment", "daemon-reload"}


def test_check_runs_in_the_unit_environment(tmp_path):
    root = make_repo(tmp_path)
    values = values_for(root, path_dirs=("/p/bin", "/usr/bin"), lang="ja_JP.UTF-8")
    run = Calls(stdout={"env -i": report_json(warning=["bin_not_found"])})
    report = run_check_in_unit_env(values, run, env={"HOME": "/h"})
    assert run.calls == [["env", "-i", "HOME=/h", "PATH=/p/bin:/usr/bin", "LANG=ja_JP.UTF-8",
                          "PYTHONUNBUFFERED=1", str(root / ".venv/bin/python"), "main.py", "check",
                          "--json", "--unit-env"]]
    assert run.kwargs[0]["cwd"] == str(root)
    assert report.warning == ["bin_not_found"] and report.fatal == []


def test_a_crashing_check_counts_as_fatal(tmp_path):
    values = values_for(make_repo(tmp_path))
    report = run_check_in_unit_env(values, Calls(results={"env -i": 1}), env={"HOME": "/h"})
    assert report.fatal == ["startup_check_failed"]


@pytest.mark.parametrize("stdout", ["", "warning: bin_not_found: x\n", "[]", '{"fatal": 1, "warning": []}',
                                    '{"fatal": [], "warning": [3]}'])
def test_unreadable_check_output_counts_as_fatal(tmp_path, stdout):
    values = values_for(make_repo(tmp_path))
    report = run_check_in_unit_env(values, Calls(stdout={"env -i": stdout}), env={"HOME": "/h"})
    assert report.fatal == ["startup_check_failed"]


def test_multiline_message_text_cannot_fake_a_code(tmp_path):
    values = values_for(make_repo(tmp_path))
    payload = json.dumps({"fatal": [], "warning": ["bin_not_found"],
                          "note": "x\nfatal: cli_not_elf: y"})
    report = run_check_in_unit_env(values, Calls(stdout={"env -i": payload}), env={"HOME": "/h"})
    assert report.fatal == [] and report.warning == ["bin_not_found"]


def test_colon_in_a_path_directory_blocks_the_unit(tmp_path, settings):
    root = make_repo(tmp_path)
    blocked = detect_unit_values(
        root, settings_with(settings, trade="claude"), which=lambda name: "/opt/a:b/bin/claude",
        env={}, unit_dir=tmp_path / "u")
    assert isinstance(blocked, UnitBlocked) and blocked.reason_code == "unsafe_path_chars"
    assert "PATH" in blocked.message


def test_no_systemd_gives_manual_start_guidance_and_places_nothing(tmp_path, settings):
    root = make_repo(tmp_path)
    run = Calls(results={"systemctl --user": FileNotFoundError("systemctl")})
    result = place_unit(root, settings, unit_dir=tmp_path / "u", confirm=lambda d: True,
                        run=run, check_run=Calls(),
                        env={"HOME": str(tmp_path), "XDG_RUNTIME_DIR": "/run/user/1"})
    assert result.state == "no_systemd"
    assert "uv run afx" in result.message
    assert not (tmp_path / "u").exists()


def test_systemctl_available_is_false_when_the_user_manager_fails():
    assert systemctl_available(Calls(results={"systemctl --user": 1})) is False
    assert systemctl_available(Calls()) is True


def test_no_runtime_directory_explains_missing_user_session(tmp_path, settings):
    root = make_repo(tmp_path)
    run = Calls(results={"systemctl --user": 1})
    result = place_unit(root, settings, unit_dir=tmp_path / "u", confirm=lambda d: True,
                        run=run, check_run=Calls(), env={"HOME": str(tmp_path)})
    assert result.reason_code == "user_session_missing"
    assert "XDG_RUNTIME_DIR が無い" in result.message
    assert "su` ではなく" in result.message


def test_runtime_directory_keeps_generic_systemd_reason(tmp_path, settings):
    root = make_repo(tmp_path)
    run = Calls(results={"systemctl --user": 1})
    result = place_unit(root, settings, unit_dir=tmp_path / "u", confirm=lambda d: True,
                        run=run, check_run=Calls(),
                        env={"HOME": str(tmp_path), "XDG_RUNTIME_DIR": "/run/user/1"})
    assert result.reason_code == "systemd_unavailable"


# --- 差分・退避 (AC-18) ---

def test_existing_unit_shows_diff_and_no_keeps_it_unchanged(tmp_path):
    units = tmp_path / "u"
    units.mkdir()
    target = units / "afx.service"
    target.write_text("[Service]\nRestartPreventExitStatus=75\n")
    seen: list[str] = []
    run = Calls()
    result = install_unit(units, "[Service]\nRestartPreventExitStatus=75 76\n",
                          confirm=lambda diff: seen.append(diff) or False, run=run)
    assert result.state == "declined"
    assert "-RestartPreventExitStatus=75\n" in seen[0] and "+RestartPreventExitStatus=75 76" in seen[0]
    assert target.read_text() == "[Service]\nRestartPreventExitStatus=75\n"
    assert list(units.glob("*.bak-*")) == [] and run.calls == []


def test_existing_unit_yes_keeps_a_backup_and_reloads(tmp_path):
    units = tmp_path / "u"
    units.mkdir()
    (units / "afx.service").write_text("old\n")
    run = Calls()
    result = install_unit(units, "new\n", confirm=lambda d: True, run=run, now=lambda: "20261013T010203Z")
    assert result.state == "installed"
    backup = units / "afx.service.bak-20261013T010203Z"
    assert result.backup == backup and backup.read_text() == "old\n"
    assert (units / "afx.service").read_text() == "new\n"
    assert run.calls == [["systemctl", "--user", "daemon-reload"]]


def test_backup_write_failure_returns_failed_and_leaves_the_unit(tmp_path, monkeypatch):
    units = tmp_path / "u"
    units.mkdir()
    (units / "afx.service").write_text("old\n")
    real = unit_mod.atomic_write

    def failing(path, data, mode):
        if ".bak-" in str(path):
            raise PermissionError("denied")
        return real(path, data, mode)

    monkeypatch.setattr(unit_mod, "atomic_write", failing)
    run = Calls()
    result = install_unit(units, "new\n", confirm=lambda d: True, run=run)
    assert result.state == "failed" and result.reason_code == "backup_failed"
    assert result.backup is None
    assert (units / "afx.service").read_text() == "old\n"
    assert run.calls == []
    assert unit_record_values(result) == {"unit": "installed", "reason_code": "backup_failed"}


def test_default_backup_name_is_utc_stamped(tmp_path):
    units = tmp_path / "u"
    units.mkdir()
    (units / "afx.service").write_text("old\n")
    install_unit(units, "new\n", confirm=lambda d: True, run=Calls())
    names = [p.name for p in units.glob("afx.service.bak-*")]
    assert len(names) == 1 and re.fullmatch(r"afx\.service\.bak-\d{8}T\d{6}Z", names[0])


def test_identical_unit_asks_nothing_and_only_reloads(tmp_path):
    units = tmp_path / "u"
    units.mkdir()
    (units / "afx.service").write_text("same\n")
    run = Calls()
    result = install_unit(units, "same\n", confirm=lambda d: pytest.fail("asked"), run=run)
    assert result.state == "unchanged"
    assert [c[2] for c in run.systemctl_calls()] == ["daemon-reload"]
    assert (units / "afx.service").read_text() == "same\n"


def test_rerun_with_identical_unit_retries_a_failed_reload(tmp_path):
    units = tmp_path / "u"
    first = install_unit(units, "x\n", confirm=lambda d: True,
                         run=Calls(results={"systemctl --user": 1}))
    assert first.state == "placed_not_reloaded"
    run = Calls()
    second = install_unit(units, "x\n", confirm=lambda d: pytest.fail("asked"), run=run)
    assert second.state == "unchanged" and len(run.systemctl_calls()) == 1
    failing = install_unit(units, "x\n", confirm=lambda d: pytest.fail("asked"),
                           run=Calls(results={"systemctl --user": 1}))
    assert failing.state == "placed_not_reloaded" and failing.reason_code == "daemon_reload_failed"
    assert unit_record_values(failing)["unit"] == "placed_not_reloaded"


@pytest.mark.parametrize("kind", ["binary", "directory"])
def test_unreadable_existing_unit_returns_failed(tmp_path, kind):
    units = tmp_path / "u"
    units.mkdir()
    if kind == "binary":
        (units / "afx.service").write_bytes(b"\xff\xfe\x00bad")
    else:
        (units / "afx.service").mkdir()
    result = install_unit(units, "new\n", confirm=lambda d: pytest.fail("asked"), run=Calls())
    assert result.state == "failed" and result.reason_code == "read_failed"
    assert unit_record_values(result) == {
        "unit": "installed" if kind == "binary" else "not_installed", "reason_code": "read_failed"}


def test_path_includes_the_real_dir_and_the_symlink_dir(tmp_path, settings):
    root = make_repo(tmp_path)
    real_dir = tmp_path / "h" / ".nvm" / "versions" / "node" / "v1" / "bin"
    real_dir.mkdir(parents=True)
    (real_dir / "claude").write_text("")
    link_dir = tmp_path / "h" / ".local" / "bin"
    link_dir.mkdir(parents=True)
    (link_dir / "claude").symlink_to(real_dir / "claude")
    values = detect_unit_values(root, settings_with(settings, trade="claude"),
                                which=lambda n: str(link_dir / "claude"), env={},
                                unit_dir=tmp_path / "u")
    assert str(real_dir.resolve()) in values.path_dirs and str(link_dir) in values.path_dirs
    assert values.attributes == ("nvm_path",)


def test_screen_era_unit_adds_a_notice(tmp_path):
    units = tmp_path / "u"
    units.mkdir()
    (units / "afx.service").write_text("ExecStart=/x/afx-start.sh\n")
    result = install_unit(units, "new\n", confirm=lambda d: True, run=Calls())
    assert any("afx ctl" in n for n in result.notes)


def test_daemon_reload_failure_reports_placed_not_reloaded_with_hints(tmp_path):
    units = tmp_path / "u"
    result = install_unit(units, "x\n", confirm=lambda d: True,
                          run=Calls(results={"systemctl --user": 1}))
    assert (units / "afx.service").read_text() == "x\n"
    assert result.state == "placed_not_reloaded" and result.reason_code == "daemon_reload_failed"
    assert "systemctl --user daemon-reload" in result.hint and "journalctl" in result.hint
    assert unit_record_values(result) == {"unit": "placed_not_reloaded",
                                          "reason_code": "daemon_reload_failed"}


def test_unit_record_values_map_every_state(tmp_path):
    p = tmp_path / "afx.service"
    assert unit_record_values(InstallResult("installed", p)) == {"unit": "installed", "reason_code": None}
    assert unit_record_values(InstallResult("unchanged", p))["unit"] == "installed"
    assert unit_record_values(InstallResult("declined", p)) == {
        "unit": "not_installed", "reason_code": "declined"}
    failed = unit_record_values(InstallResult("failed", p, reason_code="write_failed"))
    assert failed == {"unit": "not_installed", "reason_code": "write_failed"}


def test_unit_record_values_follow_the_unit_left_on_disk(tmp_path):
    p = tmp_path / "afx.service"
    p.write_text("old\n")
    assert unit_record_values(InstallResult("declined", p)) == {
        "unit": "installed", "reason_code": "declined"}
    for code in ("write_failed", "backup_failed"):
        assert unit_record_values(InstallResult("failed", p, reason_code=code)) == {
            "unit": "installed", "reason_code": code}
    placement = PlacementResult("declined", None, install=InstallResult("declined", p))
    assert unit_record_values(placement) == {"unit": "installed", "reason_code": "declined"}


def test_backup_of_the_same_second_does_not_overwrite_the_earlier_one(tmp_path):
    units = tmp_path / "u"
    units.mkdir()
    target = units / "afx.service"
    target.write_text("first\n")
    stamp = lambda: "20261013T010203Z"  # noqa: E731
    install_unit(units, "second\n", confirm=lambda d: True, run=Calls(), now=stamp)
    install_unit(units, "third\n", confirm=lambda d: True, run=Calls(), now=stamp)
    backups = {p.name: p.read_text() for p in units.glob("afx.service.bak-*")}
    assert backups == {"afx.service.bak-20261013T010203Z": "first\n",
                       "afx.service.bak-20261013T010203Z-2": "second\n"}
    assert target.read_text() == "third\n"


def test_unit_record_values_map_unplaced_placement_states():
    for state, code in (("blocked", "python_missing"), ("check_failed", "cli_not_elf"),
                        ("no_systemd", "systemd_unavailable")):
        assert unit_record_values(PlacementResult(state, code)) == {
            "unit": "not_installed", "reason_code": code}
    assert unit_record_values(PlacementResult("installed")) == {"unit": "installed", "reason_code": None}


def test_nvm_note_is_returned_by_placement(tmp_path, settings):
    root = make_repo(tmp_path)
    s = settings_with(settings, trade="claude")
    result = place_unit(root, s, unit_dir=tmp_path / "u", confirm=lambda d: True, run=Calls(),
                        check_run=Calls(), which=lambda n: "/h/.nvm/versions/node/v1/bin/claude",
                        env={"HOME": str(tmp_path)})
    assert any("afx init" in n for n in result.notes)


# --- linger の 4 状態 ---

def _loginctl(state):
    return Calls(stdout={"loginctl show-user": f"Linger={state}\n"})


def test_linger_already_enabled_changes_nothing():
    run = _loginctl("yes")
    result = enable_linger(run, confirm=lambda: pytest.fail("asked"), user="u")
    assert result.state == "already_enabled"
    assert run.calls == [["loginctl", "show-user", "u", "-p", "Linger"]]


def test_linger_declined_does_not_run_enable():
    run = _loginctl("no")
    assert enable_linger(run, confirm=lambda: False, user="u").state == "declined"
    assert all(c[1] != "enable-linger" for c in run.calls)


def test_linger_owned_by_wizard_only_after_reread_says_yes():
    class Seq(Calls):
        def __call__(self, argv, **kw):
            self.calls.append(list(argv))
            shown = sum(1 for c in self.calls if c[1] == "show-user")
            enabled = any(c[1] == "enable-linger" for c in self.calls)
            out = "Linger=yes\n" if enabled else "Linger=no\n"
            return SimpleNamespace(returncode=0, stdout=out if argv[1] == "show-user" else "", stderr="")

    run = Seq()
    result = enable_linger(run, confirm=lambda: True, user="u")
    assert result.state == "owned_by_wizard"
    assert [c[1] for c in run.calls] == ["show-user", "enable-linger", "show-user"]


def test_linger_failed_when_the_command_fails_or_is_not_confirmed():
    run = Calls(results={"loginctl enable-linger": 1}, stdout={"loginctl show-user": "Linger=no\n"})
    result = enable_linger(run, confirm=lambda: True, user="u")
    assert result.state == "failed" and result.reason_code == "unknown"
    # コマンドは成功したが再読込が no のまま: 所有を主張しない
    run = Calls(stdout={"loginctl show-user": "Linger=no\n"})
    assert enable_linger(run, confirm=lambda: True, user="u").state == "failed"


def test_linger_unreadable_state_does_not_run_enable():
    run = Calls(results={"loginctl show-user": 1})
    assert linger_state(run, user="u") == "unknown"
    assert enable_linger(run, confirm=lambda: pytest.fail("asked"), user="u").state == "failed"
    assert all(c[1] != "enable-linger" for c in run.calls)


def test_linger_reason_codes_are_valid_record_values():
    from agentic_fx.store.setup_record import REASON_CODES
    for code in ("unknown", "timeout"):
        assert code in REASON_CODES
    run = Calls(results={"loginctl enable-linger": subprocess.TimeoutExpired("x", 1)},
                stdout={"loginctl show-user": "Linger=no\n"})
    assert enable_linger(run, confirm=lambda: True, user="u").reason_code == "timeout"


def test_place_unit_reports_placed_not_reloaded_when_daemon_reload_fails(tmp_path, settings):
    root = make_repo(tmp_path)
    units = tmp_path / "u"

    class ReloadFails(Calls):
        def __call__(self, argv, **kwargs):
            result = super().__call__(argv, **kwargs)
            if argv[:3] == ["systemctl", "--user", "daemon-reload"]:
                result.returncode = 1
            return result

    result = place_unit(root, settings, unit_dir=units, confirm=lambda d: True,
                        run=ReloadFails(), check_run=Calls(), env={"HOME": str(tmp_path)})
    assert (units / "afx.service").is_file()
    assert result.state == "placed_not_reloaded"
    assert "systemctl --user daemon-reload" in result.message


def test_setup_record_accepts_each_unit_value(tmp_path):
    from agentic_fx.store.setup_record import read_setup_record, write_setup_record
    from tests.setup.test_preflight import record
    for value in ("installed", "placed_not_reloaded", "not_installed"):
        root = tmp_path / value
        (root / "data").mkdir(parents=True)
        write_setup_record(root, record(unit=value))
        assert read_setup_record(root).state == "ok", value
