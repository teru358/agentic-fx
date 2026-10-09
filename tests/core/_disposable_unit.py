"""使い捨ての user service を起こし、結果 JSON を読み、必ず止めて消す fixture。

unit admission と cgroup topology は「user service の main PID であること」が前提なので、
pytest process (login session の scope) の中では正の経路を観測できない。そこで
`systemd-run --user` で名前に一時 id を付けた service を起こし、その中で helper を走らせる。

触る unit は `afxtest-<nonce>-<case>.service` だけ。稼働中の afx の unit・他の unit には
systemctl を打たず、daemon-reload もしない。finally で unit 名指定の stop / reset-failed を
行い、`list-units --all 'afxtest-<nonce>-*'` が空であることを確かめる。
"""
from __future__ import annotations

import ctypes
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HELPER = Path(__file__).with_name("_unit_probe_helper.py")

#: admission が要求する停止の層と delegation。テストは個別に上書き・削除する。
BASE_PROPERTIES: dict[str, str] = {
    "Type": "exec",
    "Delegate": "pids memory",
    "NotifyAccess": "none",
    "KillMode": "control-group",
    "TimeoutStopSec": "5s",
    "SendSIGKILL": "yes",
    "FinalKillSignal": "9",
    "TimeoutStopFailureMode": "terminate",
    # 後始末に失敗しても unit が居残らない上限
    "RuntimeMaxSec": "60",
    "LimitCORE": "0",
}


def _run(argv: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def environment_skip_reason() -> str | None:
    """この host で使い捨て service の経路が成立しない理由。成立するなら None。"""
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return "systemd-run / systemctl が無い"
    if not os.environ.get("XDG_RUNTIME_DIR"):
        return "XDG_RUNTIME_DIR が無い (user manager に届かない)"
    try:
        r = _run(["systemctl", "--user", "is-system-running"], timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"systemctl --user is-system-running が失敗: {e!r}"
    # degraded は他の unit の失敗を表すだけで manager には届いている
    state = r.stdout.strip()
    if state not in ("running", "degraded", "starting"):
        return f"user manager に届かない (is-system-running={state!r}, rc={r.returncode})"
    if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        return "cgroup v2 が /sys/fs/cgroup に無い"
    uid = os.getuid()
    ctl = Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/cgroup.controllers")
    try:
        controllers = set(ctl.read_text().split())
    except OSError as e:
        return f"user@{uid}.service の cgroup.controllers が読めない: {e!r}"
    if not {"pids", "memory"} <= controllers:
        return f"user@{uid}.service に pids / memory が委任されていない ({sorted(controllers)})"
    try:
        ctypes.CDLL("libsystemd.so.0")
    except OSError as e:
        return f"libsystemd.so.0 が無い: {e!r}"
    return None


class DisposableUnits:
    """1 テスト分の使い捨て unit の起動と後始末。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.nonce = secrets.token_hex(6)
        self.pattern = f"afxtest-{self.nonce}-*"
        self.started: list[str] = []

    def unit_name(self, case: str) -> str:
        return f"afxtest-{self.nonce}-{case}.service"

    def _argv(self, case: str, properties: dict[str, str | None], command: list[str],
              wait: bool) -> list[str]:
        props = dict(BASE_PROPERTIES)
        for key, value in properties.items():
            if value is None:
                props.pop(key, None)
            else:
                props[key] = value
        unit = self.unit_name(case)
        argv = ["systemd-run", "--user", "--quiet", f"--unit={unit}", "--collect"]
        for key, value in props.items():
            argv += ["-p", f"{key}={value}"]
        argv += [f"--setenv=HOME={self.tmp_path}", "-p", f"WorkingDirectory={self.tmp_path}"]
        if wait:
            argv.append("--wait")
        argv += command
        return argv

    def helper_command(self, mode: str, out: Path, *extra: str) -> list[str]:
        return [sys.executable, str(HELPER), mode, str(out), *extra]

    def run_helper(self, case: str, mode: str, properties: dict[str, str | None] | None = None,
                   *, wrapper: bool = False, timeout: float = 90) -> dict:
        """helper を service の main として走らせ、終わるまで待って結果 JSON を返す。"""
        out = self.tmp_path / f"{case}.json"
        command = self.helper_command(mode, out)
        if wrapper:
            # shell が main PID になり、python は root cgroup に 2 個目の process として入る
            inner = " ".join(_shell_quote(a) for a in command)
            command = ["/bin/sh", "-c", f"{inner}; true"]
        unit = self.unit_name(case)
        self.started.append(unit)
        r = _run(self._argv(case, properties or {}, command, wait=True), timeout=timeout)
        if not out.exists():
            raise AssertionError(
                f"helper が結果を書かなかった (rc={r.returncode}): {r.stdout!r} {r.stderr!r}")
        return json.loads(out.read_text())

    def start(self, case: str, command: list[str],
              properties: dict[str, str | None] | None = None) -> str:
        """待たずに起こす (停止の層を外から測るため)。"""
        unit = self.unit_name(case)
        self.started.append(unit)
        r = _run(self._argv(case, properties or {}, command, wait=False))
        if r.returncode != 0:
            raise AssertionError(f"systemd-run が失敗: {r.stdout!r} {r.stderr!r}")
        return unit

    def stop(self, unit: str, timeout: float = 30) -> float:
        """unit 名指定で stop し、systemctl が返るまでの秒数を返す。"""
        assert unit in self.started
        t0 = time.monotonic()
        _run(["systemctl", "--user", "stop", "--", unit], timeout=timeout)
        return time.monotonic() - t0

    def show(self, unit: str, prop: str) -> str:
        assert unit in self.started
        r = _run(["systemctl", "--user", "show", "-p", prop, "--value", "--", unit])
        return r.stdout.strip()

    def listed(self) -> list[str]:
        r = _run(["systemctl", "--user", "list-units", "--all", "--no-legend", "--plain",
                  "--", self.pattern])
        return [line for line in r.stdout.splitlines() if line.strip()]

    def cleanup(self) -> None:
        for unit in self.started:
            try:
                _run(["systemctl", "--user", "stop", "--", unit], timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                # --collect 済みなら "not loaded" で失敗するが、それで良い
                _run(["systemctl", "--user", "reset-failed", "--", unit], timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                pass


def _shell_quote(arg: str) -> str:
    return "'" + arg.replace("'", "'\"'\"'") + "'"


@pytest.fixture
def disposable_units(tmp_path: Path):
    reason = environment_skip_reason()
    if reason is not None:
        pytest.skip(reason)
    units = DisposableUnits(tmp_path)
    try:
        yield units
    finally:
        units.cleanup()
    left = units.listed()
    assert left == [], f"使い捨て unit が残った: {left}"


@pytest.fixture
def systemd_environment():
    """使い捨て unit を作らない実 systemd 経路のテスト用の skip 判定。"""
    reason = environment_skip_reason()
    if reason is not None:
        pytest.skip(reason)
