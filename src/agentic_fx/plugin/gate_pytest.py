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
from agentic_fx.plugin.sandbox import _SINGLE_THREAD_ENV
from agentic_fx.runners.launcher import build_launcher_argv

if TYPE_CHECKING:
    from agentic_fx.config import Settings

_STDOUT_TAIL_MAX_BYTES = 65_536


class CandidateSnapshotError(ValueError):
    pass


# <!-- precheck 2026-08-22: T6-B2 --> 無視リスト: `submit_plugin` の後段
# (`_validate_kind` → kind=strategy/signal で `sandbox.PluginSession` が
# 候補ディレクトリを cwd に plugin.py を import する — `sandbox._build_env`
# は `PYTHONDONTWRITEBYTECODE`/`PYTHONPYCACHEPREFIX` を設定しないため
# `__pycache__/*.pyc` が候補ディレクトリに残る) 由来で生じ得る、承認判断に
# 無関係な副産物のみを許容する。REQUIRED_FILES の完全性検査 (symlink・
# hardlink・サイズ) はこれらのエントリには一切適用しない (無条件に無視
# するだけで「信頼する」わけではない)。
_IGNORED_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache"})


def _is_ignored_entry(name: str, dir_fd: int) -> bool:
    if name.endswith(".pyc"):
        return True
    if name in _IGNORED_DIR_NAMES:
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            return False
        return stat.S_ISDIR(st.st_mode)
    return False


def check_candidate_snapshot(plugin_dir: Path) -> None:
    """`plugin_dir` 直下がちょうど 3 本の通常ファイル (REQUIRED_FILES) で
    あることを dirfd + O_NOFOLLOW で検査する。サブディレクトリ・symlink・
    hardlink (`st_nlink != 1`)・サイズ超過・欠落は全て拒否。ただし
    `__pycache__/`・`*.pyc`・`.pytest_cache/` (`_IGNORED_DIR_NAMES` 判定は
    ディレクトリであることを確認したうえで無視する) は判定対象から除く
    (2026-08-22 検収是正 T6-B2 — `PluginSession` 実行後の再ゲートが
    恒久的に失敗する不具合の修正)。

    **`dir_fd` は `O_DIRECTORY | O_RDONLY` で開く — `O_PATH` ではない。**
    `O_PATH` fd は `openat` 系の `dir_fd=` 引数としては使えるが、
    `os.listdir(fd)`(内部で `fdopendir` を呼ぶ)には使えず `OSError
    (EBADF)` になる (5-D の staging dirfd 検査は `fstat` だけを呼ぶので
    `O_PATH` のままでよいが、ここは `listdir` も要るため区別する)。
    """
    # I2 是正 (verified-codex-round1.md / 設計 §2.3): 候補ディレクトリ自身が
    # 外部ディレクトリへの symlink の場合、`O_NOFOLLOW` 無しでは追従して
    # しまう。`O_DIRECTORY | O_NOFOLLOW` を symlink に当てたときの errno は
    # 実測では ENOTDIR (ELOOP ではない) — 呼び出し元の `match=` は
    # errno 文字列に依存させないこと。
    try:
        dir_fd = os.open(str(plugin_dir),
                         os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise CandidateSnapshotError(
            f"candidate dir is not a regular directory: {plugin_dir} ({exc})"
        ) from exc
    try:
        names = os.listdir(dir_fd)
        relevant_names = {n for n in names if not _is_ignored_entry(n, dir_fd)}
        unexpected = sorted(relevant_names - set(REQUIRED_FILES))
        if unexpected:
            raise CandidateSnapshotError(
                f"unexpected entries in candidate dir: {unexpected}")
        missing = sorted(set(REQUIRED_FILES) - relevant_names)
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
        basetemp_dir = workdir / "basetemp"
        basetemp_dir.mkdir()
        # gate worker がリポジトリの pyproject.toml を config-file 探索で
        # 開こうとして EACCES になるのを避けるため、空の ini を workdir
        # (read_write allowlist 内) に置いて `-c` で明示指定する — これで
        # pytest の implicit inifile 探索 (親ディレクトリを辿って
        # pyproject.toml/setup.cfg/tox.ini を探す) 自体を止める。
        ini_path = workdir / "pytest.ini"
        ini_path.write_text("[pytest]\n")
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
        # TMPDIR を gate workdir へ向ける — WorkerRunner の workdir
        # (認証コピーを含む) は tempfile.TemporaryDirectory = /tmp 配下
        # なので、同 uid のゲート worker に /tmp を read させると他
        # Mission の資格情報が読めてしまう。gate worker からは /tmp を
        # 一切 allowlist しない (前任の逸脱を撤回)。
        # BLAS/OpenMP のマルチスレッド初期化は RLIMIT_AS (仮想アドレス
        # 空間) をコア数分事前確保して食い潰す — `sandbox.py` の
        # `_SINGLE_THREAD_ENV` と同じ理由・同じ値で固定する (実測: これが
        # 無いと `import pandas` 自体が RLIMIT_AS=512MB を超えて失敗する
        # — マルチコア機で再現、CPU 台数非依存にするため固定で潰す)。
        env = {"PATH": "/usr/bin:/bin",
              "PYTHONPYCACHEPREFIX": str(pyc_dir),
              "TMPDIR": str(workdir),
              **_SINGLE_THREAD_ENV}
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
