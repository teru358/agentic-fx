"""tests/runners 共通 fixture。

`os.killpg` を test 中だけ guard する。pytest 自身の process group に向いた
シグナルは実行せず、test を失敗させる (runner の挙動は変えない)。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest


@pytest.fixture(autouse=True)
def _guard_killpg_against_own_group(monkeypatch):
    real_killpg = os.killpg
    own_pgid = os.getpgid(0)
    violations: list[tuple[int, int]] = []

    def guarded(pgid, sig):
        if pgid == own_pgid or pgid == 0:
            violations.append((pgid, int(sig)))
            return None
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", guarded)
    yield
    assert not violations, (
        f"os.killpg が pytest 自身の process group {own_pgid} を対象にした: "
        f"{violations} (FakeProc.pid に自 pid を使っていないか)"
    )


@pytest.fixture
def sacrificial_pid():
    """killpg の対象にされてもよい、別 session の犠牲用の子の pid を返す。

    終了時に必ず kill + wait する。"""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )
    try:
        yield proc.pid
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
