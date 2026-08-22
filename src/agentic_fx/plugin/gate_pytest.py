"""Landlock ゲート pytest — 親側ヘルパ (プラン10 Task 6、設計書 §4.2-3d、
§8.1-10)。`submit_plugin`/`bless` の `pytest_runner=` 差し替え先。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agentic_fx.core.landlock import is_available
from agentic_fx.runners.launcher import build_launcher_argv

if TYPE_CHECKING:
    from agentic_fx.config import Settings

_STDOUT_TAIL_MAX_BYTES = 65_536


@dataclass(frozen=True)
class GateResult:
    passed: bool
    returncode: int
    stdout_tail: str
    duration_sec: float


def run_gate_pytest(plugin_dir: Path, *, settings: "Settings") -> GateResult:
    if not is_available():
        raise RuntimeError(
            "Landlock is not available on this kernel/architecture — "
            "gate pytest refuses to run without it (fail closed, "
            "設計書 §4.2-3d)")

    import time
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="afx-gate-") as workdir_s:
        workdir = Path(workdir_s)
        pyc_dir = workdir / "pyc"
        pyc_dir.mkdir()
        rlimits = {
            "RLIMIT_AS": (settings.plugin.sandbox_memory_mb * 1024 * 1024,) * 2,
            "RLIMIT_NOFILE": (settings.plugin.sandbox_nofile,) * 2,
            "RLIMIT_FSIZE": (settings.plugin.sandbox_fsize_mb * 1024 * 1024,) * 2,
        }
        argv = build_launcher_argv(
            os.getpid(),
            [sys.executable, "-m", "agentic_fx.plugin.gate_pytest_worker",
             str(plugin_dir), str(workdir)],
            rlimits=rlimits)
        env = {"PATH": "/usr/bin:/bin",
              "PYTHONPYCACHEPREFIX": str(pyc_dir)}
        proc = subprocess.Popen(argv, cwd=str(workdir), env=env,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)
        try:
            stdout, _ = proc.communicate(timeout=settings.plugin.pytest_timeout_sec)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, __import__("signal").SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            stdout, _ = proc.communicate()
            returncode = -1
        duration = time.monotonic() - started
        tail = stdout[-_STDOUT_TAIL_MAX_BYTES:] if stdout else ""
        return GateResult(passed=(returncode == 0), returncode=returncode,
                          stdout_tail=tail, duration_sec=duration)
