"""実 systemd user manager 上の disposable unit で enable / disable を観測する。

本物の afx unit には触れない。名前に一時 id を持つ偽 afx と偽 ops だけを作り、
終了時に必ず stop・削除する。偽 afx は本物と同じ起動時の契約 (marker があれば終了コード 75、
無ければ instance flock を握って常駐) を持つ。
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import uuid
import warnings
from pathlib import Path

import pytest

from agentic_fx.ops.keys import initialize, key_dir
from agentic_fx.ops_setup.stages import OpsSetup, read_stage
from agentic_fx.ops_setup.systemd import Systemd
from agentic_fx.ops_setup.units import UnitPaths
from agentic_fx.store.enroll_marker import enroll_marker_path
from agentic_fx.store.instance_lock import InstanceAlreadyRunning, acquire_instance_lock


def _manager_available() -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "show", "-p", "Version"], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _manager_available(), reason="user manager が無い")


class Crash(BaseException):
    """process 死を模す。except Exception では捕まらない。"""


def _ctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)


def _show(unit: str, *props: str) -> dict[str, str]:
    out = _ctl("show", unit, *[f"-p{p}" for p in props]).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


_FAKE_OPS = """#!{python}
import os, socket, sys, time
addr = os.environ["NOTIFY_SOCKET"]
if addr.startswith("@"):
    addr = "\\0" + addr[1:]
s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
s.connect(addr)
s.sendall(b"READY=1")
s.close()
time.sleep(600)
"""


_FAKE_AFX = """#!{python}
import resource, sys, time
from pathlib import Path
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
from agentic_fx.store.enroll_marker import EXIT_ENROLL_IN_PROGRESS, enroll_marker_present
from agentic_fx.store.instance_lock import acquire_instance_lock
root = Path(sys.argv[1])
if enroll_marker_present(root):
    print("afx: enroll_in_progress", file=sys.stderr)
    sys.exit(EXIT_ENROLL_IN_PROGRESS)
lock = acquire_instance_lock(root / "data")
time.sleep(600)
"""


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.afx = f"afx-test-{self.id}.service"
        self.prefix = f"afx-test-{self.id}-ops"
        runtime = Path(os.environ["XDG_RUNTIME_DIR"])
        self.unit_dir = runtime / "systemd" / "user"
        self.root = tmp_path / "instance"
        self.root.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        fake_ops = tmp_path / "fake-ops"
        fake_ops.write_text(_FAKE_OPS.format(python=sys.executable))
        fake_ops.chmod(fake_ops.stat().st_mode | stat.S_IXUSR)
        fake_afx = tmp_path / "fake-afx"
        fake_afx.write_text(_FAKE_AFX.format(python=sys.executable))
        fake_afx.chmod(fake_afx.stat().st_mode | stat.S_IXUSR)
        self.paths = UnitPaths(unit_dir=self.unit_dir, afx_unit=self.afx, ops_prefix=self.prefix,
                               ops_exec=str(fake_ops))
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        # 本番の afx.service と同じく、即終了 75 を再起動の対象から外す。
        (self.unit_dir / self.afx).write_text(
            "[Unit]\nDescription=disposable afx\n[Service]\n"
            f"ExecStart={fake_afx} {self.root}\n"
            "Restart=on-failure\nRestartSec=1s\nRestartPreventExitStatus=75\n")
        self.puller = f"afx-test-{self.id}-wants.service"
        (self.unit_dir / self.puller).write_text(
            f"[Unit]\nDescription=disposable puller\nWants={self.afx}\n"
            "[Service]\nType=oneshot\nExecStart=/bin/true\n")
        _ctl("daemon-reload")

    def ops(self, feature: str) -> str:
        return self.paths.feature_unit(feature)

    def setup(self, hook=lambda _name: None) -> OpsSetup:
        return OpsSetup(self.root, systemd=Systemd(), paths=self.paths, hook=hook,
                        config_check=lambda _root, _feature: True, probe=lambda _exec, _feature: (0, ""))

    def slice(self) -> str:
        # template の instance は app-<template 名>.slice に入り、service を止めても slice は残る。
        return "app-" + self.prefix.replace("-", "\\x2d") + ".slice"

    def cleanup(self) -> list[str]:
        """自分の id の unit と slice だけを止めて消す。後始末の失敗は返して呼び出し側が報告する。"""
        problems: list[str] = []
        units = [f"{self.prefix}@{f}.service" for f in ("bot", "web", "notify")]
        for unit in [self.afx, self.puller, *units, self.slice()]:
            result = _ctl("stop", unit)
            # rc 5 は「load されていない」。作らなかった instance では正常。
            if result.returncode not in (0, 5):
                problems.append(f"stop {unit}: rc={result.returncode} {result.stderr.strip()}")
        _ctl("daemon-reload")
        for path in sorted(self.unit_dir.glob(f"afx-test-{self.id}*"), reverse=True):
            if path.is_dir():
                for child in path.iterdir():
                    child.unlink()
                path.rmdir()
            else:
                path.unlink()
        _ctl("daemon-reload")
        result = _ctl("reset-failed", f"afx-test-{self.id}*")
        if result.returncode:
            problems.append(f"reset-failed: rc={result.returncode} {result.stderr.strip()}")
        return problems

    def leftovers(self) -> list[str]:
        """manager に残る自分の id の unit と slice (他の worktree や並走中の試験は見ない)。"""
        out = _ctl("list-units", "--all", "--no-legend", "--plain").stdout
        return [line for line in out.splitlines() if self.id in line]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    e = Env(tmp_path)
    try:
        initialize(key_dir(e.root), root=e.root)
        # 通常の enable は常駐中の afx に対して行う。flock を握るまで待つ。
        assert _ctl("start", e.afx).returncode == 0
        assert _wait(lambda: not _lock_free(e.root))
        yield e
    finally:
        problems = e.cleanup()
        if problems:
            warnings.warn("disposable unit の後始末で失敗: " + "; ".join(problems), stacklevel=1)
        # 自分の id 由来の unit と slice だけを確かめる。並走する他の試験の slice は見ない。
        assert e.leftovers() == []


def _wait(predicate, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


def test_enabled_unit_has_real_properties(env):
    result = env.setup().enable("bot")

    assert result.stage == "enabled"
    props = _show(env.ops("bot"), "Type", "Requisite", "BindsTo", "PartOf", "After", "Restart",
                  "RestartUSec", "StartLimitBurst", "StartLimitIntervalUSec", "ActiveState",
                  "NoNewPrivileges")
    assert props["Type"] == "notify"
    assert env.afx in props["Requisite"].split()
    assert props["BindsTo"] == ""
    assert env.afx in props["PartOf"].split()
    assert env.afx in props["After"].split()
    assert props["Restart"] == "on-failure"
    assert props["RestartUSec"] == "5s"
    assert props["StartLimitBurst"] == "3"
    assert props["StartLimitIntervalUSec"] == "1min"
    assert props["ActiveState"] == "active"
    afx = _show(env.afx, "Wants", "ActiveState", "LoadState")
    assert env.ops("bot") in afx["Wants"].split()
    assert afx["ActiveState"] == "active" and afx["LoadState"] == "loaded"
    # 偽 afx は RestartPreventExitStatus=75 を持つので、実 manager の答えでも警告は出ない。
    assert "warn" not in read_stage(env.root, "bot")


def _lock_free(root: Path) -> bool:
    try:
        acquire_instance_lock(root / "data").close()
    except InstanceAlreadyRunning:
        return False
    return True


def test_marker_is_placed_afx_inactive_and_flock_held_while_enrolling(env):
    seen: dict[str, object] = {}

    def hook(name: str) -> None:
        if name == "after_enroll":
            seen["afx"] = _show(env.afx, "LoadState", "ActiveState")
            seen["marker"] = enroll_marker_path(env.root).exists()
            seen["lock_free"] = _lock_free(env.root)

    assert _ctl("start", env.afx).returncode == 0
    assert _wait(lambda: not _lock_free(env.root))

    env.setup(hook).enable("bot")

    assert seen == {"afx": {"LoadState": "loaded", "ActiveState": "inactive"}, "marker": True,
                    "lock_free": False}
    assert not enroll_marker_path(env.root).exists()
    assert _show(env.afx, "LoadState", "ActiveState") == {"LoadState": "loaded", "ActiveState": "active"}


def _refused_without_restart(unit: str) -> bool:
    props = _show(unit, "ActiveState", "Result", "ExecMainStatus", "NRestarts")
    return (props["ActiveState"] in {"inactive", "failed"} and props["Result"] == "exit-code"
            and props["ExecMainStatus"] == "75" and props["NRestarts"] == "0")


def test_afx_started_during_enroll_exits_75_and_is_not_restarted(env):
    observed: dict[str, dict[str, str]] = {}

    def hook(name: str) -> None:
        if name != "after_enroll":
            return
        for how, args in (("manual", ("start", env.afx)),
                          ("no-block", ("start", "--no-block", env.afx)),
                          ("wants", ("start", env.puller))):
            assert _ctl(*args).returncode == 0, how
            assert _wait(lambda: _refused_without_restart(env.afx)), (how, _show(env.afx, "ActiveState",
                                                                             "Result", "ExecMainStatus",
                                                                             "NRestarts"))
            # RestartSec (1s) を十分越えても再起動されない。
            time.sleep(3)
            observed[how] = _show(env.afx, "ActiveState", "Result", "ExecMainStatus", "NRestarts")
            assert _refused_without_restart(env.afx), (how, observed[how])
        # afx は flock を取らずに終わっているので、enable の flock は保持されたまま。
        observed["lock_free"] = {"value": str(_lock_free(env.root))}

    assert env.setup(hook).enable("bot").stage == "enabled"

    assert set(observed) == {"manual", "no-block", "wants", "lock_free"}
    assert observed["lock_free"] == {"value": "False"}
    assert _show(env.afx, "ActiveState")["ActiveState"] == "active"
    assert _wait(lambda: not _lock_free(env.root))
    assert _show(env.ops("bot"), "ActiveState")["ActiveState"] == "active"


def test_status_shows_marker_left_by_crash_and_rerun_recovers_it(env):
    def hook(name: str) -> None:
        if name == "after_lock":
            raise Crash(name)

    with pytest.raises(Crash):
        env.setup(hook).enable("bot")
    assert "marker=enroll_in_progress feature=bot since=" in env.setup().status_line("bot")
    assert _ctl("start", env.afx).returncode == 0
    assert _wait(lambda: _refused_without_restart(env.afx))

    assert env.setup().enable("bot").stage == "enabled"

    assert "marker=" not in env.setup().status_line("bot")
    assert _show(env.afx, "ActiveState")["ActiveState"] == "active"


def test_enable_is_idempotent_and_leaves_running_afx_alone(env):
    setup = env.setup()
    setup.enable("bot")
    before = _show(env.afx, "ActiveEnterTimestampMonotonic")
    template = env.paths.template_path.stat().st_mtime_ns
    dropin = env.paths.dropin_path.stat().st_mtime_ns
    generation = json.loads((key_dir(env.root) / ".ready").read_text())

    assert setup.enable("bot").stage == "enabled"

    assert _show(env.afx, "ActiveEnterTimestampMonotonic") == before
    assert env.paths.template_path.stat().st_mtime_ns == template
    assert env.paths.dropin_path.stat().st_mtime_ns == dropin
    assert json.loads((key_dir(env.root) / ".ready").read_text()) == generation


_ENABLE_POINTS = [f"{side}_{step}" for step in
                  ("marker", "stop", "show", "lock", "enroll", "unlock", "unmark", "install", "start",
                   "check")
                  for side in ("before", "after")]
_ENABLE_POINTS += [f"{side}_stage_{stage}" for stage in
                   ("stopped", "enrolled", "installed", "started", "checked", "enabled")
                   for side in ("before", "after")]


@pytest.mark.parametrize("point", _ENABLE_POINTS)
def test_enable_resumes_after_crash_at_every_point(env, point):
    fired = []

    def hook(name: str) -> None:
        if name == point and not fired:
            fired.append(name)
            raise Crash(name)

    with pytest.raises(Crash):
        env.setup(hook).enable("bot")
    assert fired

    result = env.setup().enable("bot")

    assert result.stage == "enabled"
    assert read_stage(env.root, "bot")["stage"] == "enabled"
    assert _show(env.afx, "LoadState", "ActiveState") == {"LoadState": "loaded", "ActiveState": "active"}
    assert _show(env.ops("bot"), "ActiveState")["ActiveState"] == "active"
    manifest = json.loads((key_dir(env.root) / ".ready").read_text())
    assert manifest["principals"]["ops-bot"]["generation"] == 1
    assert "Wants=" + env.ops("bot") in env.paths.dropin_path.read_text()
    assert not enroll_marker_path(env.root).exists()


def test_second_feature_aggregates_wants_and_disable_reverses(env):
    setup = env.setup()
    setup.enable("bot")
    setup.enable("web")
    dropin = env.paths.dropin_path.read_text()
    assert env.ops("bot") in dropin and env.ops("web") in dropin

    assert setup.disable("bot").stage == "disabled"

    assert _show(env.ops("bot"), "ActiveState")["ActiveState"] == "inactive"
    assert env.ops("bot") not in env.paths.dropin_path.read_text()
    assert env.ops("web") in env.paths.dropin_path.read_text()
    assert env.paths.template_path.exists()
    assert _show(env.ops("web"), "ActiveState")["ActiveState"] == "active"
    assert _show(env.afx, "ActiveState")["ActiveState"] == "active"

    assert setup.disable("web").stage == "disabled"

    assert not env.paths.template_path.exists()
    assert not env.paths.dropin_path.exists()
    assert (env.unit_dir / env.afx).exists()
    assert _show(env.ops("web"), "LoadState")["LoadState"] == "not-found"
    assert _show(env.afx, "ActiveState")["ActiveState"] == "active"


_DISABLE_POINTS = [f"{side}_{step}" for step in ("stop_ops", "uninstall") for side in ("before", "after")]
_DISABLE_POINTS += [f"{side}_stage_{stage}" for stage in ("stopping-ops", "uninstalled", "disabled")
                    for side in ("before", "after")]


@pytest.mark.parametrize("point", _DISABLE_POINTS)
def test_disable_resumes_after_crash_at_every_point(env, point):
    env.setup().enable("bot")
    fired = []

    def hook(name: str) -> None:
        if name == point and not fired:
            fired.append(name)
            raise Crash(name)

    with pytest.raises(Crash):
        env.setup(hook).disable("bot")
    assert fired

    assert env.setup().disable("bot").stage == "disabled"

    assert read_stage(env.root, "bot")["stage"] == "disabled"
    assert not env.paths.template_path.exists()
    assert not env.paths.dropin_path.exists()
    assert _show(env.ops("bot"), "ActiveState")["ActiveState"] == "inactive"


_OPS_RUN = ("ActiveState", "SubState", "InvocationID", "MainPID", "NRestarts",
            "ActiveEnterTimestampMonotonic")


def test_ops_start_alone_fails_and_does_not_pull_in_afx(env):
    env.setup().enable("bot")
    assert _ctl("stop", env.afx).returncode == 0
    assert _wait(lambda: _show(env.ops("bot"), "ActiveState")["ActiveState"] == "inactive")
    assert _wait(lambda: _lock_free(env.root))

    result = _ctl("start", env.ops("bot"))

    assert result.returncode != 0
    assert _show(env.afx, "ActiveState")["ActiveState"] == "inactive"
    assert _show(env.ops("bot"), "ActiveState")["ActiveState"] not in {"active", "activating"}
    assert _lock_free(env.root)


def test_afx_start_pulls_in_ops_through_wants(env):
    env.setup().enable("bot")
    assert _ctl("stop", env.afx).returncode == 0
    assert _wait(lambda: _show(env.ops("bot"), "ActiveState")["ActiveState"] == "inactive")

    assert _ctl("start", env.afx).returncode == 0

    assert _wait(lambda: _show(env.ops("bot"), "ActiveState")["ActiveState"] == "active")


def test_afx_explicit_restart_restarts_ops(env):
    env.setup().enable("bot")
    before = _show(env.ops("bot"), *_OPS_RUN)
    assert before["ActiveState"] == "active"

    assert _ctl("restart", env.afx).returncode == 0

    assert _wait(lambda: (lambda p: p["ActiveState"] == "active"
                          and p["InvocationID"] != before["InvocationID"])(
        _show(env.ops("bot"), *_OPS_RUN)))
    after = _show(env.ops("bot"), *_OPS_RUN)
    assert after["MainPID"] != before["MainPID"]


# afx が crash しても、afx が死んでいる窓の間 ops は active のまま残る。afx が
# Restart=on-failure で戻る時の restart job が PartOf= で ops に伝わり、ops は stop → start
# される (InvocationID と MainPID が変わる)。ops 自身の自動再起動ではないので NRestarts は増えない。
def test_afx_crash_leaves_ops_up_then_afx_restart_job_restarts_ops(env):
    env.setup().enable("bot")
    before = _show(env.ops("bot"), *_OPS_RUN)
    afx_before = _show(env.afx, "NRestarts", "MainPID", "InvocationID")
    assert before["ActiveState"] == "active"

    assert _ctl("kill", "--kill-whom=main", "-s", "KILL", env.afx).returncode == 0

    # afx が死んでいる窓 (RestartSec 中) に ops がまだ active であることを観測する
    seen_up_while_afx_down = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        afx_now = _show(env.afx, "ActiveState", "NRestarts")
        if afx_now["ActiveState"] != "active":
            if _show(env.ops("bot"), "ActiveState", "InvocationID") == {
                    "ActiveState": "active", "InvocationID": before["InvocationID"]}:
                seen_up_while_afx_down = True
        elif afx_now["NRestarts"] != afx_before["NRestarts"]:
            break
        time.sleep(0.05)
    assert seen_up_while_afx_down
    assert _show(env.afx, "NRestarts")["NRestarts"] != afx_before["NRestarts"]

    # afx の restart job が伝播して ops が入れ替わる
    assert _wait(lambda: (lambda p: p["ActiveState"] == "active"
                          and p["InvocationID"] != before["InvocationID"])(
        _show(env.ops("bot"), *_OPS_RUN)), seconds=20)
    after = _show(env.ops("bot"), *_OPS_RUN)
    assert after["MainPID"] not in (before["MainPID"], "0")
    assert after["NRestarts"] == before["NRestarts"]


_SLOW_OPS = """#!{python}
import os, socket, time
time.sleep({delay})
addr = os.environ["NOTIFY_SOCKET"]
if addr.startswith("@"):
    addr = "\\0" + addr[1:]
s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
s.connect(addr)
s.sendall(b"READY=1")
s.close()
time.sleep(600)
"""

_DYING_OPS = """#!{python}
import sys
sys.exit(1)
"""


def _replace_ops(env, text: str) -> None:
    Path(env.paths.ops_exec).write_text(text.format(python=sys.executable, delay=2))


def test_enable_start_returns_only_after_ops_sends_ready(env):
    # ops は server 認証の後に READY=1 を送る。偽 ops は 2 秒待ってから送る
    _replace_ops(env, _SLOW_OPS)
    seen: dict[str, object] = {}

    def hook(name: str) -> None:
        if name == "before_start":
            seen["before"] = time.monotonic()
        elif name == "after_start":
            seen["after"] = time.monotonic()
            seen["ops"] = _show(env.ops("bot"), "ActiveState")["ActiveState"]

    assert env.setup(hook).enable("bot").stage == "enabled"

    assert seen["after"] - seen["before"] >= 2.0
    assert seen["ops"] == "active"


def test_enable_fails_at_started_when_ops_exits_before_ready(env):
    from agentic_fx.ops_setup.systemd import SystemdError
    _replace_ops(env, _DYING_OPS)

    with pytest.raises(SystemdError):
        env.setup().enable("bot")

    assert read_stage(env.root, "bot")["stage"] == "installed"
    assert _show(env.ops("bot"), "ActiveState")["ActiveState"] != "active"


def test_afx_stop_takes_ops_down_and_start_pulls_it_back(env):
    env.setup().enable("bot")

    assert _ctl("stop", env.afx).returncode == 0
    assert _wait(lambda: _show(env.ops("bot"), "ActiveState")["ActiveState"] == "inactive")

    assert _ctl("start", env.afx).returncode == 0
    assert _wait(lambda: _show(env.ops("bot"), "ActiveState")["ActiveState"] == "active")


def test_ops_crash_is_restarted_without_stopping_afx(env):
    env.setup().enable("bot")
    pid = _show(env.ops("bot"), "MainPID")["MainPID"]
    afx_before = _show(env.afx, "ActiveEnterTimestampMonotonic")
    os.kill(int(pid), 9)

    assert _wait(lambda: (lambda p: p["ActiveState"] == "active" and p["MainPID"] not in (pid, "0"))(
        _show(env.ops("bot"), "ActiveState", "MainPID")), seconds=20)
    assert _show(env.afx, "ActiveEnterTimestampMonotonic") == afx_before


def test_resume_after_unit_template_deleted_reinstalls_and_reaches_enabled(env):
    def hook(name: str) -> None:
        if name == "after_stage_installed":
            raise Crash(name)

    with pytest.raises(Crash):
        env.setup(hook).enable("bot")
    env.paths.template_path.unlink()
    names: list[str] = []

    assert env.setup(names.append).enable("bot").stage == "enabled"

    assert "before_install" in names
    assert env.paths.template_path.exists()
    assert _show(env.ops("bot"), "LoadState", "ActiveState") == {"LoadState": "loaded", "ActiveState": "active"}


def test_disable_leaves_unit_unloaded_and_status_reports_real_unit_state(env):
    setup = env.setup()
    setup.enable("bot")
    assert setup.status_line("bot").endswith("enable enabled unit=active result=success")

    assert setup.disable("bot").stage == "disabled"

    assert _show(env.ops("bot"), "LoadState", "ActiveState") == {"LoadState": "not-found",
                                                                 "ActiveState": "inactive"}
    assert env.ops("bot") not in _show(env.afx, "Wants")["Wants"].split()
    assert setup.status_line("bot").endswith("disable disabled unit=inactive result=success")


def test_disable_of_one_feature_removes_it_from_manager_wants_while_template_stays(env):
    setup = env.setup()
    setup.enable("bot")
    setup.enable("web")

    assert setup.disable("bot").stage == "disabled"

    wants = _show(env.afx, "Wants")["Wants"].split()
    assert env.ops("bot") not in wants and env.ops("web") in wants
    assert _show(env.ops("bot"), "ActiveState")["ActiveState"] == "inactive"


_CHILD = Path(__file__).with_name("_crash_child.py")


@pytest.mark.parametrize(("point", "left_at"), [
    ("after_marker", "staged"),
    ("after_lock", "staged"),
    ("after_enroll", "stopped"),
    ("after_unlock", "stopped"),
    ("after_unmark", "stopped"),
    ("after_install", "enrolled"),
    ("after_start", "installed"),
])
def test_enable_resumes_after_child_process_dies_at(env, point, left_at):
    spec = {"root": str(env.root), "unit_dir": str(env.unit_dir), "afx_unit": env.afx,
            "ops_prefix": env.prefix, "ops_exec": env.paths.ops_exec, "feature": "bot",
            "point": point}
    child = subprocess.run([sys.executable, str(_CHILD), json.dumps(spec)], capture_output=True,
                           text=True, timeout=120, env={**os.environ, "HOME": str(env.home)})

    assert child.returncode == 70, child.stderr
    assert read_stage(env.root, "bot")["stage"] == left_at

    assert env.setup().enable("bot").stage == "enabled"

    assert _show(env.afx, "LoadState", "ActiveState") == {"LoadState": "loaded", "ActiveState": "active"}
    assert _show(env.ops("bot"), "ActiveState")["ActiveState"] == "active"
    assert not enroll_marker_path(env.root).exists()
    manifest = json.loads((key_dir(env.root) / ".ready").read_text())
    assert manifest["principals"]["ops-bot"] == {**manifest["principals"]["ops-bot"],
                                                 "generation": 1, "state": "active"}


def test_enable_with_afx_stopped_beforehand_leaves_afx_and_ops_down(env):
    assert _ctl("stop", env.afx).returncode == 0
    assert _wait(lambda: _lock_free(env.root))

    assert env.setup().enable("bot").stage == "enabled"

    assert _show(env.afx, "ActiveState")["ActiveState"] == "inactive"
    assert _show(env.ops("bot"), "LoadState", "ActiveState") == {"LoadState": "loaded",
                                                                 "ActiveState": "inactive"}
    assert env.setup().status_line("bot").endswith("afx_before_enable=inactive ops=starts_with_afx")

    assert _ctl("start", env.afx).returncode == 0
    assert _wait(lambda: _show(env.ops("bot"), "ActiveState")["ActiveState"] == "active")


def test_reset_failed_clears_a_failed_unit_and_reports_an_unloaded_one(env):
    from agentic_fx.ops_setup.systemd import SystemdError

    unit = f"afx-test-{env.id}-rf.service"
    done = subprocess.run(["systemd-run", "--user", f"--unit={unit}", "--quiet", "--wait", "/bin/false"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode != 0
    assert _show(unit, "ActiveState")["ActiveState"] == "failed"

    Systemd().reset_failed(unit)

    assert _show(unit, "ActiveState")["ActiveState"] != "failed"
    with pytest.raises(SystemdError) as raised:
        Systemd().reset_failed(f"afx-test-{env.id}-absent.service")
    assert raised.value.reason == "reset-failed"


def _web_target(tmp_path: Path, port: int) -> str:
    target = tmp_path / f"target-{port}.toml"
    target.write_text(f"port = {port}\n")
    target.chmod(0o600)
    return str(target)


def _reconfigure(env, tmp_path, port: int):
    from agentic_fx.ops_setup.reconfigure import run_ops_reconfigure
    from agentic_fx.ops_setup.web_setup import WebOptions
    from web_support import ScriptIO

    io = ScriptIO(tty=False)
    rc = run_ops_reconfigure(env.root, "web", io=io, setup_factory=lambda _root: env.setup(),
                             probe_runner=lambda _feature: (0, ""),
                             options=WebOptions(non_interactive=True, config_file=_web_target(tmp_path, port),
                                                secret_action="absent"))
    return rc, io.out


def _tree_bytes(*bases: Path) -> dict[str, bytes]:
    return {str(path): path.read_bytes() for base in bases for path in sorted(base.rglob("*"))
            if path.is_file() and path.name != "setup.lock"}


def test_reconfigure_restarts_only_ops_web_and_leaves_afx_keys_and_the_stage_alone(env, tmp_path, monkeypatch):
    from agentic_fx.ops_setup.feature_config import collect, write_inputs

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    write_inputs(env.root, "web", {"port": 8788})
    assert env.setup().enable("web").stage == "enabled"
    assert _wait(lambda: _show(env.ops("web"), "ActiveState")["ActiveState"] == "active")
    pid_before = _show(env.ops("web"), "MainPID")["MainPID"]
    afx_before = _show(env.afx, "ActiveEnterTimestampMonotonic", "MainPID")
    before = _tree_bytes(key_dir(env.root), env.root / "data" / "ops")

    rc, out = _reconfigure(env, tmp_path, 9000)

    assert rc == 0 and out == ["web: reconfigured"]
    assert collect(env.root, "web").values == {"port": 9000}
    assert _show(env.ops("web"), "ActiveState")["ActiveState"] == "active"
    assert _show(env.ops("web"), "MainPID")["MainPID"] not in (pid_before, "0")
    assert _show(env.afx, "ActiveEnterTimestampMonotonic", "MainPID") == afx_before
    assert _tree_bytes(key_dir(env.root), env.root / "data" / "ops") == before


def test_reconfigure_with_afx_stopped_leaves_ops_web_down_until_afx_starts(env, tmp_path, monkeypatch):
    from agentic_fx.ops_setup.feature_config import collect, write_inputs

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    write_inputs(env.root, "web", {"port": 8788})
    assert env.setup().enable("web").stage == "enabled"
    assert _ctl("stop", env.afx).returncode == 0
    assert _wait(lambda: _lock_free(env.root))

    rc, out = _reconfigure(env, tmp_path, 9001)

    assert rc == 0 and out == ["web: reconfigured ops=starts_with_afx"]
    assert collect(env.root, "web").values == {"port": 9001}
    assert _show(env.afx, "ActiveState")["ActiveState"] == "inactive"
    assert _show(env.ops("web"), "ActiveState")["ActiveState"] == "inactive"

    assert _ctl("start", env.afx).returncode == 0
    assert _wait(lambda: _show(env.ops("web"), "ActiveState")["ActiveState"] == "active")


def test_reconfigure_refuses_when_the_unit_files_changed_after_the_last_reload(env, tmp_path, monkeypatch):
    from agentic_fx.ops_setup.feature_config import write_inputs

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    write_inputs(env.root, "web", {"port": 8788})
    assert env.setup().enable("web").stage == "enabled"
    unit_file = env.unit_dir / env.afx
    unit_file.write_text(unit_file.read_text() + "\n# edited after the last reload\n")
    afx_before = _show(env.afx, "ActiveEnterTimestampMonotonic", "NeedDaemonReload")
    assert afx_before["NeedDaemonReload"] == "yes"

    rc, out = _reconfigure(env, tmp_path, 9002)

    assert rc == 1 and out == []
    assert _show(env.afx, "ActiveEnterTimestampMonotonic", "NeedDaemonReload") == afx_before
