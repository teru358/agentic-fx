"""bare リポジトリへの履歴記録 (blob-level plumbing、設計書 §5.2)。

ポーセリン (`git add`/`git commit`) は使わない — bare にはワークツリーが
無く、`git commit -- <path>` はワークツリー内容を取り直すため検証後の
書き換えを拾ってしまう (codex 4 周目 C1)。blob を版ディレクトリの
ファイルから直接作り、専用 index に `<name>/<file>` のパス名で置く。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from pathlib import Path

from agentic_fx.plugin.version_store import artifact_hash_bytes, content_hash_bytes

_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "agentic-fx", "GIT_AUTHOR_EMAIL": "noreply@localhost",
    "GIT_COMMITTER_NAME": "agentic-fx", "GIT_COMMITTER_EMAIL": "noreply@localhost",
}


class HistoryGitError(Exception):
    """git 不在・identity 欠如・リポジトリ障害。呼び出し元は pending のまま
    に留め本番には触れない (§5.1 手順 5)。"""


class HistoryGitDetachedError(HistoryGitError):
    """symbolic-ref が exit 1 (detached HEAD — 人間が履歴操作中)。"""


class HistoryGitCasConflictError(HistoryGitError):
    """update-ref の CAS 失敗 (並行 commit)。呼び出し元は頭から再試行する。"""


class SchedulerThreadForbiddenError(Exception):
    """§5.4: git サブプロセスは scheduler スレッドから呼ばれてはならない。"""


# precheck 2026-08-22 wave2: T11-B9 / T11-B10
def _env(history_git_dir: Path) -> dict[str, str]:
    # B-10: HOME をそのまま子へ渡すと `~/.gitconfig` の global identity が
    # commit-tree の失敗テストを無効化してしまう (この環境で実測: global
    # user.name/user.email が設定済みのため raise しない)。HOME を落とし
    # GIT_CONFIG_GLOBAL/GIT_CONFIG_NOSYSTEM で外部設定の混入を遮断する
    # (本番でもユーザ設定 (core.hooksPath 等) の混入を防ぐ意味で正しい)。
    env = {"PATH": os.environ.get("PATH", ""),
           "GIT_DIR": str(history_git_dir),
           "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1"}
    env.update(_IDENTITY_ENV)
    return env


def _run(args: list[str], *, env: dict[str, str],
         check: bool = True) -> subprocess.CompletedProcess:
    # B-9: `tests/test_subprocess_stdin_policy.py` は src/**/*.py の
    # subprocess.run|Popen 呼び出し全数に stdin= を要求する (fd 0 継承防止の
    # リポジトリ規約)。allowlist は backtest_runs.py の 1 件のみ。
    return subprocess.run(["git", *args], env=env, capture_output=True,
                          text=True, check=check, stdin=subprocess.DEVNULL)


def _run_bytes(args: list[str], *, env: dict[str, str]) -> bytes:
    """round2 #4 是正 (2026-08-29、verified-round2.md #4): `_run` は
    `text=True` で subprocess の stdout をロケール既定 (実質 UTF-8) で
    decode してから返す。`cat-file blob` の 3 呼び出し (`:name/plugin.py`
    等) はそのバイト列を再 `.encode()` して content_hash/artifact_hash を
    取り直す検証に使うため、text 経由の往復は非可逆 — CRLF は
    `HistoryGitError: index blob hash mismatch` (probe 実測)、非 UTF-8 は
    `UnicodeDecodeError` が subprocess の decode 段で未捕捉のまま漏れる
    (probe 実測、`except subprocess.CalledProcessError` を素通りする)。
    `cat-file blob` の 3 回だけをバイナリで走らせる専用ヘルパ
    (`_run` 全体を binary 化すると `.strip()` している呼び出し元を
    全部触ることになるため最小形にする)。stdin= 規約は維持する。"""
    return subprocess.run(["git", *args], env=env, capture_output=True,
                          check=True, stdin=subprocess.DEVNULL).stdout


def _assert_not_scheduler_thread() -> None:
    if threading.current_thread().name.startswith("scheduler"):
        raise SchedulerThreadForbiddenError(
            "plugins/.history.git: git subprocess must not run on the "
            "scheduler thread (§5.4 — approval decisions run on the "
            "shell/reconcile thread only)")


# precheck 2026-08-22 wave2: T11-B11
def ensure_bare_repo(history_git_dir: Path) -> None:
    """`git init --bare` を lazy に (§5.2 初期化)。

    B-11: 親ディレクトリが書込不能 (0500 等) の場合 `mkdir` は
    `PermissionError` (OSError) を送出する。`HistoryGitError` 系だけを
    捕える呼び出し元 (11d/11g) にとって OSError は素通りしてしまうため、
    ここで `HistoryGitError` に変換し fail closed の意味論を統一する。
    """
    if not history_git_dir.is_dir():
        try:
            history_git_dir.mkdir(parents=True)
            subprocess.run(["git", "init", "--bare", "-q", str(history_git_dir)],
                           check=True, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)  # B-9: stdin ポリシー規約
        except OSError as exc:
            raise HistoryGitError(
                f"plugins/.history.git: failed to initialize bare repo at "
                f"{history_git_dir}: {exc}") from exc


def record_version(history_git_dir: Path, *, name: str, artifact_hash: str,
                   content_hash: str, approval_id: int,
                   version_dir: Path) -> str | None:
    _assert_not_scheduler_thread()
    ensure_bare_repo(history_git_dir)
    env = _env(history_git_dir)

    try:
        # ref = git symbolic-ref HEAD (unborn でも ref 名は返る)
        ref_proc = _run(["symbolic-ref", "HEAD"], env=env, check=False)
        if ref_proc.returncode == 1 or "not a symbolic ref" in ref_proc.stderr:
            raise HistoryGitDetachedError(
                "plugins/.history.git: HEAD is detached (a human may be "
                "operating the history repo — retry later)")
        if ref_proc.returncode != 0:
            raise HistoryGitError(
                f"plugins/.history.git: symbolic-ref failed rc={ref_proc.returncode}: "
                f"{ref_proc.stderr}")
        ref = ref_proc.stdout.strip()

        # old = git rev-parse --verify <ref> (失敗 = unborn)
        old_proc = _run(["rev-parse", "--verify", ref], env=env, check=False)
        unborn = old_proc.returncode != 0
        old = "" if unborn else old_proc.stdout.strip()

        src = version_dir
        b_py = _run(["hash-object", "-w", str(src / "plugin.py")], env=env).stdout.strip()
        b_cfg = _run(["hash-object", "-w", str(src / "config.yaml")], env=env).stdout.strip()
        b_test = _run(["hash-object", "-w", str(src / "test_plugin.py")], env=env).stdout.strip()

        with tempfile.TemporaryDirectory(prefix="afx-history-index-") as tmp:
            index_path = str(Path(tmp) / "index")
            idx_env = {**env, "GIT_INDEX_FILE": index_path}

            if unborn:
                _run(["read-tree", "--empty"], env=idx_env)
            else:
                _run(["read-tree", old], env=idx_env)

            # 旧版の同 prefix エントリを専用 index から外す
            _run(["rm", "--cached", "-r", "-q", "--ignore-unmatch", "--", f"{name}/"],
                env=idx_env)

            for blob, relpath in ((b_py, "plugin.py"), (b_cfg, "config.yaml"),
                                  (b_test, "test_plugin.py")):
                _run(["update-index", "--add", "--cacheinfo",
                     f"100644,{blob},{name}/{relpath}"], env=idx_env)

            # 検証: index の blob から hash を取り直し、payload と一致することを確認
            # round2 #4 是正: text=True 往復での CRLF 破壊/UnicodeDecodeError
            # を避けるため、この 3 回だけバイナリで読む (_run_bytes)。
            py_bytes = _run_bytes(["cat-file", "blob", f":{name}/plugin.py"],
                                  env=idx_env)
            cfg_bytes = _run_bytes(["cat-file", "blob", f":{name}/config.yaml"],
                                   env=idx_env)
            test_bytes = _run_bytes(["cat-file", "blob", f":{name}/test_plugin.py"],
                                    env=idx_env)
            recomputed_content = content_hash_bytes(py_bytes, cfg_bytes)
            recomputed_artifact = artifact_hash_bytes(py_bytes, cfg_bytes, test_bytes)
            if recomputed_content != content_hash or recomputed_artifact != artifact_hash:
                raise HistoryGitError(
                    f"plugins/.history.git: index blob hash mismatch for {name!r} "
                    "(candidate may have been swapped between snapshot and record)")

            tree = _run(["write-tree"], env=idx_env).stdout.strip()

            if not unborn:
                old_tree = _run(["rev-parse", f"{old}^{{tree}}"], env=env).stdout.strip()
                if tree == old_tree:
                    return None  # 変化なし = commit を作らず成功

            msg = (f"approve {name} content={content_hash} "
                  f"artifact={artifact_hash} (approval #{approval_id})")
            if unborn:
                commit_proc = _run(["commit-tree", tree, "-m", msg], env=env)
            else:
                commit_proc = _run(["commit-tree", tree, "-p", old, "-m", msg], env=env)
            new_sha = commit_proc.stdout.strip()

            update_ref_proc = _run(["update-ref", ref, new_sha, old], env=env, check=False)
            if update_ref_proc.returncode != 0:
                raise HistoryGitCasConflictError(
                    f"plugins/.history.git: update-ref CAS failed for {ref} "
                    f"(expected old={old!r}): {update_ref_proc.stderr}")
            return new_sha
    except subprocess.CalledProcessError as e:
        raise HistoryGitError(
            f"plugins/.history.git: git command failed: {e}") from e
