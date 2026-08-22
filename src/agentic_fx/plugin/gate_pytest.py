"""Landlock ゲート pytest — 親側ヘルパ (プラン10 Task 6、設計書 §4.2-3d、
§8.1-10)。`submit_plugin`/`bless` の `pytest_runner=` 差し替え先。
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agentic_fx.core.landlock import is_available
from agentic_fx.plugin.loader import (
    REQUIRED_FILES, artifact_hash_bytes, content_hash as _content_hash,
)
from agentic_fx.plugin.loader import _MAX_FILE_BYTES
from agentic_fx.runners.launcher import build_launcher_argv

if TYPE_CHECKING:
    from agentic_fx.config import Settings

_STDOUT_TAIL_MAX_BYTES = 65_536


class CandidateSnapshotError(ValueError):
    pass


def check_candidate_snapshot(plugin_dir: Path) -> None:
    """`plugin_dir` 直下がちょうど 3 本の通常ファイル (REQUIRED_FILES) で
    あることを dirfd + O_NOFOLLOW で検査する。サブディレクトリ・symlink・
    hardlink (`st_nlink != 1`)・サイズ超過・欠落は全て拒否。

    **`dir_fd` は `O_DIRECTORY | O_RDONLY` で開く — `O_PATH` ではない。**
    `O_PATH` fd は `openat` 系の `dir_fd=` 引数としては使えるが、
    `os.listdir(fd)`(内部で `fdopendir` を呼ぶ)には使えず `OSError
    (EBADF)` になる (5-D の staging dirfd 検査は `fstat` だけを呼ぶので
    `O_PATH` のままでよいが、ここは `listdir` も要るため区別する)。
    """
    dir_fd = os.open(str(plugin_dir), os.O_DIRECTORY | os.O_RDONLY)
    try:
        names = os.listdir(dir_fd)
        unexpected = sorted(set(names) - set(REQUIRED_FILES))
        if unexpected:
            raise CandidateSnapshotError(
                f"unexpected entries in candidate dir: {unexpected}")
        missing = sorted(set(REQUIRED_FILES) - set(names))
        if missing:
            raise CandidateSnapshotError(f"missing required files: {missing}")
        for name in REQUIRED_FILES:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except OSError as exc:
                raise CandidateSnapshotError(
                    f"{name}: symlink or unreadable member "
                    f"in candidate dir ({exc})") from exc
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise CandidateSnapshotError(f"{name} is not a regular file")
                if st.st_nlink != 1:
                    raise CandidateSnapshotError(
                        f"{name} has nlink={st.st_nlink} (expected 1 — "
                        "hardlink sharing an external inode is not allowed)")
                if st.st_size > _MAX_FILE_BYTES:
                    raise CandidateSnapshotError(
                        f"{name} exceeds size limit ({_MAX_FILE_BYTES} bytes)")
            finally:
                os.close(fd)
    finally:
        os.close(dir_fd)


def hashes_of(plugin_dir: Path) -> tuple[str, str]:
    """`(content_hash, artifact_hash)` を返す。`content_hash` は
    `plugin/loader.content_hash` をそのまま再利用 (定義の二重実装をしない
    — loader.py が正)。`artifact_hash` は 3 本フルの
    `artifact_hash_bytes` を候補ディレクトリの現在の内容で計算する。"""
    plugin_py = (plugin_dir / "plugin.py").read_bytes()
    config_yaml = (plugin_dir / "config.yaml").read_bytes()
    test_plugin = (plugin_dir / "test_plugin.py").read_bytes()
    return (_content_hash(plugin_dir),
           artifact_hash_bytes(plugin_py, config_yaml, test_plugin))


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

    check_candidate_snapshot(plugin_dir)
    before_content, before_artifact = hashes_of(plugin_dir)

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

    after_content, after_artifact = hashes_of(plugin_dir)
    if after_content != before_content or after_artifact != before_artifact:
        return GateResult(passed=False, returncode=returncode,
                          stdout_tail=(stdout[-_STDOUT_TAIL_MAX_BYTES:] if stdout else "") + "\n[gate_pytest] candidate "
                                      "content changed during test run "
                                      "(hash mismatch) — rejecting",
                          duration_sec=duration)

    tail = stdout[-_STDOUT_TAIL_MAX_BYTES:] if stdout else ""
    return GateResult(passed=(returncode == 0), returncode=returncode,
                      stdout_tail=tail, duration_sec=duration)
