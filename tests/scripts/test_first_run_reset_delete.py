from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from test_first_run_reset import (
    _git_baseline,
    _instance_id,
    _populate_owned_and_foreign,
    _run,
    _run_tty,
    reset_environment,
)

OWN_UNITS = {"afx.service", "afx-ops@web.service"}

_SYSTEMCTL = """#!/bin/sh
state="$FAKE_STATE"
shift
sub="$1"
shift
unit=""
for argument in "$@"; do
    case "$argument" in -*) ;; *) unit="$argument" ;; esac
done
if [ -e "$state/hook-$sub" ]; then sh "$state/hook-$sub"; rm -f "$state/hook-$sub"; fi
dir="$state/unit"
activity=absent
[ -e "$dir/$unit.active" ] && activity=$(cat "$dir/$unit.active")
enablement=absent
[ -e "$dir/$unit.enabled" ] && enablement=$(cat "$dir/$unit.enabled")
if [ "$sub" = is-active ]; then
    if [ "$activity" = absent ]; then echo inactive; exit 3; fi
    echo "$activity"
    [ "$activity" = active ]
    exit $?
fi
if [ "$sub" = is-enabled ]; then
    if [ "$enablement" = absent ]; then echo "unit file does not exist" >&2; exit 1; fi
    echo "$enablement"
    [ "$enablement" = enabled ]
    exit $?
fi
present=no
[ -e "$FAKE_UNIT_DIR/afx.service" ] && present=yes
echo "$sub $* unit_file=$present" >> "$state/systemctl"
[ -e "$state/fail-$sub" ] && exit 1
case "$sub" in
    stop)
        case "$activity" in active|activating|reloading|deactivating) echo inactive > "$dir/$unit.active" ;; esac ;;
    disable)
        if [ "$enablement" = absent ]; then echo "unit file does not exist" >&2; exit 1; fi
        echo disabled > "$dir/$unit.enabled" ;;
    reset-failed)
        [ "$activity" = failed ] && echo inactive > "$dir/$unit.active" ;;
esac
exit 0
"""

_RM = """#!/bin/sh
echo "$*" >> "$FAKE_STATE/rm"
if [ -e "$FAKE_STATE/fail-rm" ]; then
    needle=$(cat "$FAKE_STATE/fail-rm")
    for arg in "$@"; do
        case "$arg" in "$needle"|*"/$needle") exit 1 ;; esac
    done
fi
exec %s "$@"
"""

_LOGINCTL = """#!/bin/sh
state="$FAKE_STATE"
record=absent
[ -e "$FAKE_ROOT/data/state/setup.json" ] && record=present
echo "$1 record=$record" >> "$state/loginctl"
[ -e "$state/fail-loginctl" ] && exit 1
exit 0
"""

_PYTHON = """#!/bin/sh
if [ "$1" = -I ] && [ -e "$FAKE_STATE/fail-helper-$3" ]; then exit 1; fi
exec %s "$@"
"""


class Fakes:
    def __init__(self, env: dict[str, str], tmp_path: Path, root: Path) -> None:
        self.state = tmp_path / "fake-state"
        (self.state / "unit").mkdir(parents=True)
        fake_bin = Path(env["PATH"].split(":")[0])
        real_python = shutil.which("python3", path=os.environ["PATH"])
        assert real_python
        real_rm = shutil.which("rm", path=os.environ["PATH"])
        assert real_rm
        for name, body in (("systemctl", _SYSTEMCTL), ("loginctl", _LOGINCTL), ("python3", _PYTHON % real_python), ("rm", _RM % real_rm)):
            executable = fake_bin / name
            executable.write_text(body)
            executable.chmod(0o755)
        env.update({"FAKE_STATE": str(self.state), "FAKE_ROOT": str(root), "FAKE_UNIT_DIR": str(Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user")})

    def unit(self, name: str, *, active: str = "active", enabled: str = "enabled") -> None:
        (self.state / "unit" / f"{name}.active").write_text(active + "\n")
        (self.state / "unit" / f"{name}.enabled").write_text(enabled + "\n")

    def active(self, *units: str) -> None:
        for unit in units:
            self.unit(unit)

    def condition(self, name: str) -> tuple[str, str]:
        def read(suffix: str) -> str:
            path = self.state / "unit" / f"{name}.{suffix}"
            return path.read_text().strip() if path.exists() else "absent"
        return read("active"), read("enabled")

    def hook(self, verb: str, script: str) -> None:
        (self.state / f"hook-{verb}").write_text(script)

    def removed(self) -> list[str]:
        return [line.split()[-1] for line in self.lines("rm")]

    def inject(self, name: str) -> None:
        (self.state / f"fail-{name}").write_text("")

    def clear(self, name: str) -> None:
        (self.state / f"fail-{name}").unlink()

    def lines(self, name: str) -> list[str]:
        path = self.state / name
        return path.read_text().splitlines() if path.exists() else []

    def systemctl(self, verb: str | None = None) -> list[str]:
        return [line for line in self.lines("systemctl") if verb is None or line.split()[0] == verb]

    def changing_calls(self) -> list[str]:
        return [line for line in self.systemctl() if line.split()[0] in {"stop", "disable"}]


@pytest.fixture
def populated(reset_environment, tmp_path):
    clone, env, script = reset_environment
    fakes = Fakes(env, tmp_path, clone)
    (clone / "plugins").mkdir()
    (clone / "plugins" / ".gitkeep").write_text("")
    baseline = _git_baseline(clone)
    foreign = _populate_owned_and_foreign(clone, env, tmp_path)
    for tool in (".pytest_cache", ".ruff_cache"):
        (clone / tool).mkdir()
        (clone / tool / ".gitignore").write_text("*\n")
        (clone / tool / "state").write_text("cache\n")
    (clone / ".venv" / "bin").mkdir(parents=True)
    (clone / ".venv" / "bin" / "python").write_text("interpreter\n")
    (clone / "afx-ops" / ".venv").mkdir(parents=True, exist_ok=True)
    (clone / "afx-ops" / ".venv" / "marker").write_text("ops venv\n")
    (clone / "src" / "agentic_fx" / "__pycache__").mkdir(parents=True)
    (clone / "src" / "agentic_fx" / "__pycache__" / "wizard.cpython-313.pyc").write_bytes(b"cache")
    (clone / "tmp").mkdir()
    (clone / "tmp" / "x").write_text("generated\n")
    return clone, env, script, fakes, foreign, baseline


def _tree(*bases: Path) -> dict[Path, tuple]:
    result: dict[Path, tuple] = {}
    for base in bases:
        if not (base.exists() or base.is_symlink()):
            continue
        for directory, directories, files in os.walk(base, followlinks=False):
            for name in directories + files:
                path = Path(directory) / name
                mode = stat.S_IMODE(path.lstat().st_mode)
                if path.is_symlink():
                    result[path] = ("link", os.readlink(path), mode)
                elif path.is_dir():
                    result[path] = ("dir", mode)
                else:
                    result[path] = ("file", path.read_bytes(), mode)
    return result


def _config_trees(env: dict[str, str], tmp_path: Path) -> dict[Path, tuple]:
    return _tree(Path(env["HOME"]), Path(env["XDG_CONFIG_HOME"]), tmp_path / "foreign-root")


def _owned_config_paths(clone: Path, env: dict[str, str]) -> list[Path]:
    unit_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
    return [
        unit_dir / "afx.service",
        unit_dir / "afx-ops@.service",
        unit_dir / "afx.service.d",
        Path(env["HOME"]) / ".config" / "agentic-fx" / "api" / _instance_id(clone),
        Path(env["XDG_CONFIG_HOME"]) / "agentic-fx" / "ops" / "web",
    ]


def _is_within(path: Path, bases: list[Path]) -> bool:
    return any(path == base or base in path.parents for base in bases)


def _reset(script: Path, clone: Path, env: dict[str, str], tmp_path: Path, *flags: str):
    return _run([str(script), "--root", str(clone), "--yes", *flags], cwd=tmp_path, env=env)


@contextlib.contextmanager
def _lock_holder(lock_path: Path):
    code = "import fcntl, sys, time\nhandle = open(sys.argv[1], 'a')\nfcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\nprint('ready', flush=True)\ntime.sleep(120)\n"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([sys.executable, "-c", code, str(lock_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None and process.stdout.readline().strip() == "ready"
        yield process
    finally:
        process.kill()
        process.wait()
        if process.stdout:
            process.stdout.close()


def _repo_owned_remains(clone: Path) -> list[str]:
    return [
        relative
        for relative in ("config/settings.yaml", "config/settings.yaml.bak-old", "data", "logs", "policy/directives.md", "plugins/generated.py")
        if (clone / relative).exists()
    ]


def test_reset_deletes_owned_assets_and_preserves_foreign_assets(reset_environment, tmp_path):
    clone, env, script = reset_environment
    fakes = Fakes(env, tmp_path, clone)
    foreign = _populate_owned_and_foreign(clone, env, tmp_path)
    (clone / "plugins" / ".gitkeep").write_text("")
    fakes.active("afx.service", "afx-ops@web.service")

    result = _run([str(script), "--root", str(clone), "--yes", "--stop-running", "--include-env", "--include-linger"], cwd=tmp_path, env=env)

    assert result.returncode == 0, result.stderr
    for relative in ("config/settings.yaml", "data", "logs", "policy/directives.md", ".env"):
        assert not (clone / relative).exists()
    assert (clone / "plugins" / ".gitkeep").is_file()
    assert all(path.exists() for path in foreign)


def test_busy_manual_lock_refuses_reset_without_removing_files(reset_environment, tmp_path):
    clone, env, script = reset_environment
    (clone / "data").mkdir()
    (clone / "data" / "ops").mkdir()
    lock_path = clone / "data" / "instance.lock"
    with _lock_holder(lock_path):
        result = _run([str(script), "--root", str(clone), "--yes", "--stop-running"], cwd=tmp_path, env=env)

    assert result.returncode == 2
    assert lock_path.exists()
    assert "手動起動" in result.stderr


@pytest.mark.parametrize("include_env", [False, True])
def test_product_contract_removes_owned_state_and_keeps_everything_else(populated, tmp_path, include_env):
    clone, env, script, fakes, foreign, _ = populated
    fakes.active("afx.service", "afx-ops@web.service")
    tracked_before = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=no"], cwd=clone, capture_output=True, text=True, check=True).stdout
    flags = ["--stop-running", *(["--include-env"] if include_env else [])]

    result = _reset(script, clone, env, tmp_path, *flags)

    assert result.returncode == 0, result.stderr
    assert _repo_owned_remains(clone) == []
    assert (clone / ".env").exists() is (not include_env)
    assert (clone / "plugins").is_dir() and sorted(path.name for path in (clone / "plugins").iterdir()) == [".gitkeep"]
    assert (clone / "docs" / "examples" / "plugins" / "ema" / "plugin.py").is_file()
    assert (clone / ".venv" / "bin" / "python").is_file()
    assert (clone / "afx-ops" / ".venv" / "marker").is_file()
    assert (clone / "config" / "settings.yaml.example").is_file()
    tracked_after = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=no"], cwd=clone, capture_output=True, text=True, check=True).stdout
    assert tracked_after == tracked_before
    for path in _owned_config_paths(clone, env):
        assert not path.exists(), path
    assert all(path.exists() for path in foreign)
    assert fakes.systemctl("daemon-reload") and fakes.systemctl("reset-failed")
    assert fakes.lines("loginctl") == []
    assert "git status --porcelain --ignored:" in result.stdout


@pytest.mark.parametrize("include_env", [False, True])
def test_git_status_returns_to_baseline_except_fixed_tool_entries(populated, tmp_path, include_env):
    clone, env, script, fakes, _, baseline = populated
    allowed = (b"!! .env", b"!! .pytest_cache/", b"!! .ruff_cache/", b"!! .venv/", b"!! afx-ops/.venv/", b"!! src/agentic_fx/__pycache__/", b"!! tmp/")
    populated_state = set(_git_baseline(clone).split(b"\0"))
    assert {b"!! data/", b"!! logs/", b"!! plugins/", b"!! config/settings.yaml"} <= populated_state

    result = _reset(script, clone, env, tmp_path, "--stop-running", *(["--include-env"] if include_env else []))

    assert result.returncode == 0, result.stderr
    before = set(baseline.split(b"\0"))
    after = set(_git_baseline(clone).split(b"\0"))
    assert {entry for entry in after - before if not entry.startswith(allowed)} == set()
    assert before - after == set()
    assert (b"!! .env" in after) is not include_env


def test_reset_removes_readonly_adopted_plugin_version_and_returns_to_baseline(populated, tmp_path):
    clone, env, script, fakes, _, baseline = populated
    version = clone / "plugins" / ".versions" / "ema" / "d34db33f"
    version.mkdir(parents=True)
    (version / "plugin.py").write_text("PLUGIN = 'ema'\n")
    (version / "plugin.py").chmod(0o400)
    version.chmod(0o500)

    result = _reset(script, clone, env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert not version.exists()
    before = set(baseline.split(b"\0"))
    after = set(_git_baseline(clone).split(b"\0"))
    allowed = (b"!! .env", b"!! .pytest_cache/", b"!! .ruff_cache/", b"!! .venv/", b"!! afx-ops/.venv/", b"!! src/agentic_fx/__pycache__/", b"!! tmp/")
    assert {entry for entry in after - before if not entry.startswith(allowed)} == set()
    assert before - after == set()


def test_only_owned_config_paths_change_and_foreign_assets_stay_identical(populated, tmp_path):
    clone, env, script, fakes, foreign, _ = populated
    fakes.active("afx.service")
    before = _config_trees(env, tmp_path)

    result = _reset(script, clone, env, tmp_path, "--stop-running", "--include-env", "--include-linger")

    assert result.returncode == 0, result.stderr
    after = _config_trees(env, tmp_path)
    owned = _owned_config_paths(clone, env)
    assert set(after) - set(before) == set()
    assert {path for path in before if path not in after and not _is_within(path, owned)} == set()
    assert {path: after[path] for path in after if path in before and after[path] != before[path]} == {}
    assert all(path in after for path in before if not _is_within(path, owned))
    assert all(path in after for path in foreign)


def test_without_stop_confirmation_a_running_instance_is_left_untouched(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    fakes.active("afx.service", "afx-ops@web.service")
    with _lock_holder(clone / "data" / "instance.lock"):
        before = _config_trees(env, tmp_path)

        result = _reset(script, clone, env, tmp_path, "--include-linger")

        assert result.returncode == 2
        assert "--stop-running" in result.stderr
        assert _repo_owned_remains(clone) == ["config/settings.yaml", "config/settings.yaml.bak-old", "data", "logs", "policy/directives.md", "plugins/generated.py"]
        assert fakes.changing_calls() == [] and fakes.lines("loginctl") == []
        assert _config_trees(env, tmp_path) == before


def test_stop_running_stops_then_disables_only_this_instances_units_before_deleting(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    fakes.active("afx.service", "afx-ops@web.service")
    (Path(env["XDG_CONFIG_HOME"]) / "agentic-fx" / "ops" / "foreign").joinpath("marker").write_text("")
    with _lock_holder(clone / "data" / "instance.lock") as holder:
        fakes.hook("stop", f"kill {holder.pid}\n")
        result = _reset(script, clone, env, tmp_path, "--stop-running")

    assert result.returncode == 0, result.stderr
    calls = fakes.systemctl()
    verbs = [line.split()[0] for line in calls]
    assert verbs[:2] == ["stop", "stop"] and verbs[2:4] == ["disable", "disable"]
    assert {line.split()[-2] for line in calls[:4]} == OWN_UNITS
    assert all(line.split()[-2] in OWN_UNITS for line in calls if line.split()[0] != "daemon-reload")
    assert all("unit_file=yes" in line for line in calls[:4])
    assert verbs[4:] == ["daemon-reload", "reset-failed", "reset-failed"]
    assert "unit_file=no" in calls[4]
    assert _repo_owned_remains(clone) == []


def test_interactive_stop_confirmation_is_required_and_a_wrong_answer_stops_nothing(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    fakes.active("afx.service")

    rc, _ = _run_tty([str(script), "--root", str(clone)], cwd=tmp_path, env=env, input_text="reset\nno\n")
    assert rc == 2
    assert fakes.changing_calls() == []
    assert (clone / "data").is_dir()

    rc, _ = _run_tty([str(script), "--root", str(clone)], cwd=tmp_path, env=env, input_text="reset\nstop\n")
    assert rc == 0
    assert [line.split()[0] for line in fakes.changing_calls()] == ["stop", "stop", "disable", "disable"]
    assert not (clone / "data").exists()


@pytest.mark.parametrize("stop_flag", [[], ["--stop-running"]])
def test_manual_start_holder_without_active_unit_blocks_everything(populated, tmp_path, stop_flag):
    clone, env, script, fakes, _, _ = populated
    with _lock_holder(clone / "data" / "instance.lock"):
        before = _config_trees(env, tmp_path)

        result = _reset(script, clone, env, tmp_path, *stop_flag)

        assert result.returncode == 2
        assert "手動起動" in result.stderr
        assert fakes.changing_calls() == []
        assert len(_repo_owned_remains(clone)) == 6
        assert _config_trees(env, tmp_path) == before


@pytest.mark.parametrize(
    ("record", "flags", "calls"),
    [
        ({"state": "owned_by_wizard"}, ["--include-linger"], 1),
        ({"state": "owned_by_wizard"}, [], 0),
        ({"state": "already_enabled"}, ["--include-linger"], 0),
        ({"state": "failed"}, ["--include-linger"], 0),
        ({"state": "declined"}, ["--include-linger"], 0),
        (None, ["--include-linger"], 0),
        ("broken", ["--include-linger"], 0),
    ],
)
def test_linger_is_disabled_only_for_an_owned_record_with_explicit_consent(populated, tmp_path, record, flags, calls):
    clone, env, script, fakes, _, _ = populated
    setup = clone / "data" / "state" / "setup.json"
    if record is None:
        setup.unlink()
    elif record == "broken":
        setup.write_text("{not json")
    else:
        setup.write_text(json.dumps({"linger": record}))

    result = _reset(script, clone, env, tmp_path, *flags)

    assert result.returncode == 0, result.stderr
    assert fakes.lines("loginctl") == (["disable-linger record=present"] * calls)
    assert not (clone / "data").exists()


def test_repeated_populate_and_reset_cycles_return_to_the_same_state(populated, tmp_path):
    clone, env, script, fakes, foreign, baseline = populated
    gitkeep = clone / "plugins" / ".gitkeep"
    for cycle in range(3):
        fakes.active("afx.service")
        result = _reset(script, clone, env, tmp_path, "--stop-running", "--include-env", "--include-linger")
        assert result.returncode == 0, result.stderr
        assert len(fakes.lines("loginctl")) == cycle + 1
        assert gitkeep.is_file()
        assert all(path.exists() for path in foreign)
        again = _reset(script, clone, env, tmp_path, "--stop-running", "--include-env", "--include-linger")
        assert again.returncode == 0, again.stderr
        assert len(fakes.lines("loginctl")) == cycle + 1
        before = _config_trees(env, tmp_path)
        extra = set(_git_baseline(clone).split(b"\0")) - set(baseline.split(b"\0"))
        assert {entry for entry in extra if not entry.startswith((b"!! .pytest_cache/", b"!! .ruff_cache/", b"!! .venv/", b"!! afx-ops/.venv/", b"!! src/agentic_fx/__pycache__/", b"!! tmp/"))} == set()
        assert _config_trees(env, tmp_path) == before
        _populate_again(clone, env, tmp_path)


def _populate_again(clone: Path, env: dict[str, str], tmp_path: Path) -> None:
    for relative in ("config/settings.yaml", "policy/directives.md", ".env"):
        (clone / relative).write_text("again\n")
    (clone / "data" / "state").mkdir(parents=True)
    (clone / "data" / "ops").mkdir()
    (clone / "data" / "state" / "setup.json").write_text(json.dumps({"linger": {"state": "owned_by_wizard"}}))
    (clone / "logs").mkdir()
    (clone / "logs" / "activity.log").write_text("log\n")
    (clone / "plugins" / "generated.py").write_text("generated\n")
    unit_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "afx.service").write_text(f"[Service]\nWorkingDirectory={clone}\n")
    key = Path(env["HOME"]) / ".config" / "agentic-fx" / "api" / _instance_id(clone)
    key.mkdir(parents=True, exist_ok=True)
    (key / "operator.token").write_text("owned\n")


@pytest.mark.parametrize(
    ("fault", "active", "deleted_by_first_run"),
    [
        ("stop", ["afx.service"], False),
        ("loginctl", [], False),
        ("helper-hold-locks", [], False),
    ],
)
def test_interrupted_reset_converges_when_rerun_and_never_touches_foreign(populated, tmp_path, fault, active, deleted_by_first_run):
    clone, env, script, fakes, foreign, _ = populated
    fakes.active(*active)
    foreign_before = _tree(*foreign)
    fakes.inject(fault)

    first = _reset(script, clone, env, tmp_path, "--stop-running", "--include-linger")

    assert first.returncode != 0
    assert (not (clone / "plugins" / "generated.py").exists() or (clone / "data").exists())
    if fault == "loginctl":
        assert _repo_owned_remains(clone) == ["data"]
    elif not deleted_by_first_run:
        assert len(_repo_owned_remains(clone)) == 6
    assert _tree(*foreign) == foreign_before
    fakes.clear(fault)
    fakes.active(*active)

    second = _reset(script, clone, env, tmp_path, "--stop-running", "--include-linger")

    assert second.returncode == 0, second.stderr
    assert _repo_owned_remains(clone) == []
    for path in _owned_config_paths(clone, env):
        assert not path.exists(), path
    assert _tree(*foreign) == foreign_before
    third = _reset(script, clone, env, tmp_path, "--stop-running", "--include-linger")
    assert third.returncode == 0, third.stderr
    assert _tree(*foreign) == foreign_before


def test_empty_root_succeeds_repeatedly_and_leaves_foreign_assets(reset_environment, tmp_path):
    clone, env, script = reset_environment
    fakes = Fakes(env, tmp_path, clone)
    foreign = _populate_owned_and_foreign(clone, env, tmp_path)
    bare = tmp_path / "bare"
    (bare / "config").mkdir(parents=True)
    shutil.copy2(clone / "config" / "settings.yaml.example", bare / "config" / "settings.yaml.example")
    subprocess.run(["git", "init", "--quiet", str(bare)], check=True)
    before = _tree(*foreign)
    configs = _config_trees(env, tmp_path)

    for _ in range(2):
        result = _run([str(script), "--root", str(bare), "--yes", "--include-env", "--include-linger"], cwd=tmp_path, env=env)
        assert result.returncode == 0, result.stderr

    assert _tree(*foreign) == before
    assert _config_trees(env, tmp_path) == configs
    assert fakes.changing_calls() == [] and fakes.lines("loginctl") == []


def _lock_files(clone: Path) -> tuple[Path, Path]:
    (clone / "data" / "ops").mkdir(parents=True, exist_ok=True)
    setup, instance = clone / "data" / "ops" / "setup.lock", clone / "data" / "instance.lock"
    setup.touch()
    instance.touch()
    return setup, instance


def _both_locks_are_free(*paths: Path) -> bool:
    import fcntl

    handles = []
    try:
        for path in paths:
            handle = open(path, "a")
            handles.append(handle)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
        return True
    finally:
        for handle in handles:
            handle.close()


def test_setup_lock_held_by_another_process_refuses_and_removes_nothing(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    setup, _instance = _lock_files(clone)
    with _lock_holder(setup):
        before = _config_trees(env, tmp_path)

        result = _reset(script, clone, env, tmp_path, "--stop-running", "--include-linger")

        assert result.returncode == 2
        assert "afx init" in result.stderr
        assert fakes.changing_calls() == [] and fakes.lines("loginctl") == [] and fakes.removed() == []
        assert len(_repo_owned_remains(clone)) == 6
        assert _config_trees(env, tmp_path) == before


def test_both_locks_stay_held_for_the_whole_deletion_and_are_released_after(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    setup, instance = _lock_files(clone)
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import fcntl, sys\n"
        "result = []\n"
        "for path in sys.argv[2:]:\n"
        "    handle = open(path, 'a')\n"
        "    try:\n"
        "        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "        result.append('free')\n"
        "    except BlockingIOError:\n"
        "        result.append('blocked')\n"
        "open(sys.argv[1], 'a').write(' '.join(result) + chr(10))\n"
    )
    outcome = tmp_path / "probe.out"
    for verb in ("disable", "daemon-reload"):
        fakes.hook(verb, f"python3 {probe} {outcome} {setup} {instance}\n")
    fakes.unit("afx.service", active="inactive", enabled="enabled")

    result = _reset(script, clone, env, tmp_path, "--stop-running")

    assert result.returncode == 0, result.stderr
    assert outcome.read_text().splitlines() == ["blocked blocked", "blocked blocked"]


def test_locks_are_released_when_the_script_exits_on_a_failure(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    setup, instance = _lock_files(clone)
    fakes.active("afx.service")
    fakes.inject("stop")

    result = _reset(script, clone, env, tmp_path, "--stop-running")

    assert result.returncode != 0
    assert len(_repo_owned_remains(clone)) == 6
    assert _both_locks_are_free(setup, instance)


def test_enabled_but_inactive_units_are_disabled_without_being_stopped(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    fakes.unit("afx.service", active="inactive", enabled="enabled")
    fakes.unit("afx-ops@web.service", active="inactive", enabled="enabled")

    result = _reset(script, clone, env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert fakes.systemctl("stop") == []
    assert {line.split()[-2] for line in fakes.systemctl("disable")} == OWN_UNITS
    assert fakes.condition("afx.service") == ("inactive", "disabled")
    assert fakes.condition("afx-ops@web.service") == ("inactive", "disabled")


def test_failed_unit_is_disabled_and_its_failure_is_cleared(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    fakes.unit("afx.service", active="failed", enabled="enabled")

    result = _reset(script, clone, env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert fakes.systemctl("stop") == []
    assert fakes.condition("afx.service") == ("inactive", "disabled")
    assert fakes.condition("afx-ops@web.service") == ("absent", "absent")


def test_activating_unit_is_stopped_like_an_active_one(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    fakes.unit("afx.service", active="activating", enabled="enabled")

    result = _reset(script, clone, env, tmp_path, "--stop-running")

    assert result.returncode == 0, result.stderr
    assert [line.split()[-2] for line in fakes.systemctl("stop")] == ["afx.service", "afx-ops@web.service"]
    assert fakes.condition("afx.service")[1] == "disabled"


@pytest.mark.parametrize("state", ["reloading", "deactivating"])
def test_transitional_running_unit_is_stopped_to_inactive(populated, tmp_path, state):
    clone, env, script, fakes, _, _ = populated
    fakes.unit("afx.service", active=state, enabled="enabled")

    result = _reset(script, clone, env, tmp_path, "--stop-running")

    assert result.returncode == 0, result.stderr
    assert fakes.condition("afx.service") == ("inactive", "disabled")


def test_data_without_ops_directory_is_reset_and_instance_lock_is_still_held(reset_environment, tmp_path):
    clone, env, script = reset_environment
    fakes = Fakes(env, tmp_path, clone)
    (clone / "config" / "settings.yaml").write_text("owned\n")
    (clone / "data").mkdir()
    (clone / "data" / "agentic.db").write_text("db\n")

    result = _reset(script, clone, env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "lock を取れません" not in result.stderr
    assert not (clone / "data").exists()
    assert not (clone / "config" / "settings.yaml").exists()


def test_data_as_regular_file_refuses_without_deleting(reset_environment, tmp_path):
    clone, env, script = reset_environment
    fakes = Fakes(env, tmp_path, clone)
    (clone / "config" / "settings.yaml").write_text("owned\n")
    (clone / "data").write_text("not a directory\n")

    result = _reset(script, clone, env, tmp_path)

    assert result.returncode == 2
    assert "data/ の経路に symlink があるため lock を取れません。何も消していません" in result.stderr
    assert (clone / "config" / "settings.yaml").exists()
    assert fakes.removed() == []


@pytest.mark.parametrize("linked", ["data", "data/ops"])
def test_lock_refuses_symlinked_data_path_without_creating_external_locks(reset_environment, tmp_path, linked):
    clone, env, script = reset_environment
    fakes = Fakes(env, tmp_path, clone)
    (clone / "config" / "settings.yaml").write_text("owned\n")
    outside = tmp_path / "outside"
    (outside / "ops").mkdir(parents=True)
    (outside / "keep").write_text("external\n")
    if linked == "data":
        (clone / "data").symlink_to(outside, target_is_directory=True)
    else:
        (clone / "data").mkdir()
        (clone / "data" / "ops").symlink_to(outside / "ops", target_is_directory=True)

    result = _reset(script, clone, env, tmp_path)

    assert result.returncode == 2
    assert "data/ の経路に symlink があるため lock を取れません。何も消していません" in result.stderr
    assert not (outside / "ops" / "setup.lock").exists()
    assert not (outside / "instance.lock").exists()
    assert (outside / "keep").read_text() == "external\n"
    assert (clone / "config" / "settings.yaml").exists()
    assert fakes.removed() == []


@pytest.mark.parametrize(
    ("kind", "target", "fault"),
    [
        ("ops_feature", "ops/web", "rm"),
        ("api_key", "api/{instance_id}", "rm"),
        ("ops_unit", "afx-ops@.service", "rm"),
        ("ops_dropin", "afx-ops.conf", "rm"),
        ("unit", "afx.service", "rm"),
        ("repo_settings", "settings.yaml", "rm"),
        ("repo_plugins", "generated.py", "rm"),
        ("repo_logs", "logs", "rm"),
        ("linger", "", "loginctl"),
        ("repo_data", "data", "rm"),
        ("repo_env", ".env", "rm"),
    ],
)
def test_each_failed_removal_converges_on_the_next_run(populated, tmp_path, kind, target, fault):
    clone, env, script, fakes, foreign, baseline = populated
    if fault == "rm":
        (fakes.state / "fail-rm").write_text(target.format(instance_id=_instance_id(clone)))
    else:
        fakes.inject(fault)

    first = _reset(script, clone, env, tmp_path, "--include-env", "--include-linger")

    assert first.returncode != 0
    assert f"失敗した kind: {kind}" in first.stderr
    assert all(path.exists() for path in foreign)
    unit_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
    if kind in {"ops_unit", "ops_dropin"}:
        assert (unit_dir / "afx.service").exists()
        assert "primary unit は所有の証拠として残しました。再実行で続きを消します" in first.stderr
    if fault == "rm":
        (fakes.state / "fail-rm").unlink()
    else:
        fakes.clear(fault)

    second = _reset(script, clone, env, tmp_path, "--include-env", "--include-linger")

    assert second.returncode == 0, second.stderr
    assert _repo_owned_remains(clone) == []
    assert all(not path.exists() for path in _owned_config_paths(clone, env))
    assert all(path.exists() for path in foreign)
    before = set(baseline.split(b"\0"))
    after = set(_git_baseline(clone).split(b"\0"))
    allowed = (b"!! .pytest_cache/", b"!! .ruff_cache/", b"!! .venv/", b"!! afx-ops/.venv/", b"!! src/agentic_fx/__pycache__/", b"!! tmp/")
    assert {entry for entry in after - before if not entry.startswith(allowed)} == set()
    assert before - after == set()


def test_paths_created_after_the_plan_was_made_are_not_removed(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    fakes.hook("is-active", f"touch {clone}/plugins/keep-me {clone}/config/settings.yaml.bak-new\n")

    result = _reset(script, clone, env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert (clone / "plugins" / "keep-me").exists()
    assert (clone / "config" / "settings.yaml.bak-new").exists()
    assert not (clone / "plugins" / "generated.py").exists()
    assert (clone / "plugins" / ".gitkeep").exists()


def test_removal_follows_the_fixed_order_of_kinds(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    fakes.active("afx.service", "afx-ops@web.service")
    unit_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
    expected = [
        str(Path(env["XDG_CONFIG_HOME"]) / "agentic-fx" / "ops" / "web"),
        str(Path(env["HOME"]) / ".config" / "agentic-fx" / "api" / _instance_id(clone)),
        str(unit_dir / "afx-ops@.service"),
        str(unit_dir / "afx.service.d" / "afx-ops.conf"),
        str(unit_dir / "afx.service"),
        str(clone / "config" / "settings.yaml"),
        str(clone / "plugins" / "generated.py"),
        str(clone / "logs"),
        str(clone / "data"),
        str(clone / ".env"),
    ]

    result = _reset(script, clone, env, tmp_path, "--stop-running", "--include-env")

    assert result.returncode == 0, result.stderr
    removed = fakes.removed()
    positions = [removed.index(path) for path in expected]
    assert positions == sorted(positions)
    verbs = [line.split()[0] for line in fakes.systemctl()]
    assert verbs.index("daemon-reload") < len(verbs)


def test_linger_needs_its_own_typed_answer_on_a_terminal(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated

    rc, _ = _run_tty([str(script), "--root", str(clone), "--include-linger"], cwd=tmp_path, env=env, input_text="reset\nno\n")
    assert rc == 2
    assert fakes.lines("loginctl") == []
    assert (clone / "data").is_dir()

    rc, _ = _run_tty([str(script), "--root", str(clone), "--include-linger"], cwd=tmp_path, env=env, input_text="reset\nlinger\n")
    assert rc == 0
    assert fakes.lines("loginctl") == ["disable-linger record=present"]
    assert not (clone / "data").exists()


def test_failing_reset_failed_and_daemon_reload_do_not_stop_the_deletion(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    fakes.inject("reset-failed")
    fakes.inject("daemon-reload")

    result = _reset(script, clone, env, tmp_path, "--include-linger")

    assert result.returncode == 0, result.stderr
    assert "daemon-reload" in result.stderr
    assert _repo_owned_remains(clone) == []
    assert fakes.lines("loginctl") == ["disable-linger record=present"]


def test_clearing_failed_state_is_limited_to_a_primary_unit_this_root_owns(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    unit_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
    (unit_dir / "afx.service").write_text("[Service]\nWorkingDirectory=/elsewhere\n")
    (unit_dir / "afx.service.bak-1").write_text(f"[Service]\nWorkingDirectory={clone}\n")

    result = _reset(script, clone, env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert not (unit_dir / "afx.service.bak-1").exists()
    assert (unit_dir / "afx.service").exists()
    assert all(line.split()[-2:-1] != ["afx.service"] for line in fakes.systemctl("reset-failed"))
    assert fakes.systemctl("disable") == [] or all("afx.service" not in line.split()[1:] for line in fakes.systemctl("disable"))


def test_a_running_feature_unit_is_stopped_even_without_the_primary_unit(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    unit_dir = Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
    (unit_dir / "afx.service").unlink()
    fakes.active("afx-ops@web.service")
    feature = Path(env["XDG_CONFIG_HOME"]) / "agentic-fx" / "ops" / "web"

    result = _reset(script, clone, env, tmp_path, "--stop-running")

    assert result.returncode == 0, result.stderr
    assert [line.split()[-2] for line in fakes.systemctl("stop")] == ["afx-ops@web.service"]
    assert [line.split()[-2] for line in fakes.systemctl("disable")] == ["afx-ops@web.service"]
    assert not feature.exists()
    assert fakes.condition("afx-ops@web.service")[1] == "disabled"


def test_missing_lock_files_are_created_and_held(tmp_path):
    helper = Path(__file__).resolve().parents[2] / "scripts" / "first_run_reset_helper.py"
    (tmp_path / "data" / "ops").mkdir(parents=True)
    setup_lock = tmp_path / "data" / "ops" / "setup.lock"
    instance_lock = tmp_path / "data" / "instance.lock"
    process = subprocess.Popen([sys.executable, "-I", str(helper), "hold-locks", str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None and process.stdout.readline().strip() == "ok"
        assert setup_lock.exists() and instance_lock.exists()
        code = "import fcntl, sys\nfor name in sys.argv[1:]:\n    handle = open(name, 'a')\n    try:\n        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\n    except BlockingIOError:\n        continue\n    sys.exit(1)\n"
        assert subprocess.run([sys.executable, "-c", code, str(setup_lock), str(instance_lock)]).returncode == 0
    finally:
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=10)
        if process.stdout:
            process.stdout.close()


def test_a_manual_holder_that_outlives_the_stopped_unit_is_reported_as_such(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    fakes.active("afx.service")
    with _lock_holder(clone / "data" / "instance.lock"):
        result = _reset(script, clone, env, tmp_path, "--stop-running")

    assert result.returncode == 2
    assert "手動起動" in result.stderr and "unit を止めました" in result.stderr
    assert len(_repo_owned_remains(clone)) == 6


def test_a_failing_kind_is_reported_and_the_other_kinds_are_still_removed(populated, tmp_path):
    clone, env, script, fakes, _, _ = populated
    (fakes.state / "fail-rm").write_text(f"{clone}/logs")

    result = _reset(script, clone, env, tmp_path, "--include-linger")

    assert result.returncode == 1
    assert "失敗した kind: repo_logs linger" in result.stderr
    assert "消せた kind:" in result.stdout and "repo_settings" in result.stdout and "repo_logs" not in result.stdout.split("消せた kind:")[1].splitlines()[0]
    assert (clone / "logs").exists() and (clone / "data").exists()
    assert not (clone / "config" / "settings.yaml").exists() and not (clone / "plugins" / "generated.py").exists()
    assert fakes.lines("loginctl") == []
