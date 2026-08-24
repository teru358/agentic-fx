"""bare 履歴リポジトリの blob-level plumbing (プラン 10 Task 11b、§5.2・§5.4)。"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from agentic_fx.plugin import history_git, version_store

PLUGIN_PY = b"def compute(df, params):\n    return {}\n"
CONFIG_YAML = b"kind: indicator\n"
TEST_PY = b"def test_x():\n    pass\n"


def _make_version(tmp_path: Path, name: str, test_bytes: bytes = TEST_PY):
    a = version_store.artifact_hash_bytes(PLUGIN_PY, CONFIG_YAML, test_bytes)
    d = version_store.create_version_dir(
        tmp_path, name, a, plugin_py=PLUGIN_PY, config_yaml=CONFIG_YAML,
        test_plugin=test_bytes, op_identity="1")
    c = version_store.content_hash_bytes(PLUGIN_PY, CONFIG_YAML)
    return d, a, c


def test_record_version_unborn_repo_creates_initial_commit(tmp_path):
    history_dir = tmp_path / "plugins" / ".history.git"
    d, a, c = _make_version(tmp_path / "plugins", "sma")
    sha = history_git.record_version(
        history_dir, name="sma", artifact_hash=a, content_hash=c,
        approval_id=1, version_dir=d)
    assert sha is not None
    log = subprocess.run(
        ["git", "--git-dir", str(history_dir), "log", "--oneline"],
        capture_output=True, text=True, check=True)
    assert "approve sma" in log.stdout


def test_record_version_no_op_when_tree_unchanged(tmp_path):
    """同じ 3 本を 2 回記録すると tree が一致し commit を作らない (write-tree
    == old^{tree})。"""
    history_dir = tmp_path / "plugins" / ".history.git"
    d, a, c = _make_version(tmp_path / "plugins", "sma")
    sha1 = history_git.record_version(
        history_dir, name="sma", artifact_hash=a, content_hash=c,
        approval_id=1, version_dir=d)
    sha2 = history_git.record_version(
        history_dir, name="sma", artifact_hash=a, content_hash=c,
        approval_id=2, version_dir=d)
    assert sha1 is not None
    assert sha2 is None


def test_record_version_second_version_replaces_prefix_entries(tmp_path):
    """新版の記録が旧版の同 prefix エントリを index から外す (`git rm --cached`)。
    tree に旧 test_plugin.py の内容が残らないこと。"""
    history_dir = tmp_path / "plugins" / ".history.git"
    plugins_root = tmp_path / "plugins"
    d1, a1, c1 = _make_version(plugins_root, "sma", TEST_PY)
    history_git.record_version(history_dir, name="sma", artifact_hash=a1,
                               content_hash=c1, approval_id=1, version_dir=d1)
    test_py_v2 = TEST_PY + b"# v2\n"
    d2, a2, c2 = _make_version(plugins_root, "sma", test_py_v2)
    history_git.record_version(history_dir, name="sma", artifact_hash=a2,
                               content_hash=c2, approval_id=2, version_dir=d2)
    show = subprocess.run(
        ["git", "--git-dir", str(history_dir), "show", "HEAD:sma/test_plugin.py"],
        capture_output=True, text=True, check=True)
    assert show.stdout.encode() == test_py_v2
    # 履歴には両バージョンが残る (git log --all で 2 commit)
    log = subprocess.run(
        ["git", "--git-dir", str(history_dir), "log", "--oneline"],
        capture_output=True, text=True, check=True)
    assert len(log.stdout.strip().splitlines()) == 2


def test_record_version_detached_head_raises_distinct_error(tmp_path):
    """symbolic-ref exit 1 (detached) は HistoryGitDetachedError — リポジトリ
    障害 (128) とは区別する (activity/通知の理由分け)。"""
    history_dir = tmp_path / "plugins" / ".history.git"
    d, a, c = _make_version(tmp_path / "plugins", "sma")
    history_git.record_version(history_dir, name="sma", artifact_hash=a,
                               content_hash=c, approval_id=1, version_dir=d)
    # detach する — HEAD ファイルを直接 SHA で上書き
    head_sha = subprocess.run(
        ["git", "--git-dir", str(history_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    (history_dir / "HEAD").write_text(head_sha + "\n")

    d2, a2, c2 = _make_version(tmp_path / "plugins", "sma", TEST_PY + b"#v2\n")
    with pytest.raises(history_git.HistoryGitDetachedError):
        history_git.record_version(history_dir, name="sma", artifact_hash=a2,
                                   content_hash=c2, approval_id=2, version_dir=d2)


def test_record_version_missing_repo_dir_is_not_a_repository_error(tmp_path):
    """git 不在 (`GIT_DIR` が git リポジトリでない — init が壊れた/権限で
    書けない環境) は HistoryGitError で fail closed。"""
    history_dir = tmp_path / "readonly_root" / ".history.git"
    history_dir.parent.mkdir(mode=0o500)
    d, a, c = _make_version(tmp_path / "plugins", "sma")
    with pytest.raises(history_git.HistoryGitError):
        history_git.record_version(history_dir, name="sma", artifact_hash=a,
                                   content_hash=c, approval_id=1, version_dir=d)
    history_dir.parent.chmod(0o700)  # tmp_path 掃除を壊さないため


def test_record_version_missing_identity_env_raises(tmp_path, monkeypatch):
    """commit identity が無いと commit-tree が失敗し永久 pending — サービス
    供給の identity env が無いケースを模す (`_IDENTITY_ENV` を握りつぶす)。"""
    monkeypatch.setattr(history_git, "_IDENTITY_ENV", {})
    history_dir = tmp_path / "plugins" / ".history.git"
    d, a, c = _make_version(tmp_path / "plugins", "sma")
    # commit-tree は GIT_AUTHOR_* が無いと環境の git config にフォールバック
    # しようとして失敗する (CI/sandbox には user.name/user.email が無い前提)
    with pytest.raises(history_git.HistoryGitError):
        history_git.record_version(history_dir, name="sma", artifact_hash=a,
                                   content_hash=c, approval_id=1, version_dir=d)


# precheck 2026-08-22 wave2: T11-B12
def test_record_version_cas_conflict_raises_distinct_error(tmp_path, monkeypatch):
    """update-ref の CAS 失敗 (並行 commit で old が古い) は
    HistoryGitCasConflictError — 呼び出し元が頭から再試行する対象。

    B-12 是正: 旧稿の `racing_run` は `args[:2] == ["update-ref", env.get(...)[:0]]`
    という常に False の比較で何もしておらず、ref も進めていなかった (書き損じ、
    実測で CAS は成功し例外が上がらないことを確認済み)。正しい注入は、実際の
    `update-ref <ref> <new> <old>` 呼び出しの直前に**無関係な commit で ref を
    横から進める** (well-known な空 tree sha を使い、`old` から生やして
    force update する) — これにより実呼び出しの `<old>` が既に古くなり
    CAS が確実に失敗する。ref 名はハードコードせず `symbolic-ref HEAD` の
    実測値 (`args[1]`) をそのまま使う。"""
    history_dir = tmp_path / "plugins" / ".history.git"
    d, a, c = _make_version(tmp_path / "plugins", "sma")
    history_git.record_version(history_dir, name="sma", artifact_hash=a,
                               content_hash=c, approval_id=1, version_dir=d)
    real_run = history_git._run
    _EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git の空 tree の既知 sha (全 git 共通の定数)

    def racing_run(args, *, env, check=True):
        if args and args[0] == "update-ref" and len(args) >= 4:
            ref, old = args[1], args[3]
            intruder = real_run(
                ["commit-tree", _EMPTY_TREE, "-p", old, "-m", "intruder"],
                env=env, check=True).stdout.strip()
            real_run(["update-ref", ref, intruder, old], env=env, check=True)
            return real_run(args, env=env, check=False)
        return real_run(args, env=env, check=check)

    monkeypatch.setattr(history_git, "_run", racing_run)
    d2, a2, c2 = _make_version(tmp_path / "plugins", "sma", TEST_PY + b"#v2\n")
    with pytest.raises(history_git.HistoryGitCasConflictError):
        history_git.record_version(history_dir, name="sma", artifact_hash=a2,
                                   content_hash=c2, approval_id=2, version_dir=d2)


def test_history_git_worktree_stays_clean_no_porcelain_used(tmp_path):
    """bare リポジトリなのでワークツリーが存在しないことの pin — `git status`
    がワークツリー無しエラーで失敗すること (ポーセリンを使っていない証跡)。"""
    history_dir = tmp_path / "plugins" / ".history.git"
    d, a, c = _make_version(tmp_path / "plugins", "sma")
    history_git.record_version(history_dir, name="sma", artifact_hash=a,
                               content_hash=c, approval_id=1, version_dir=d)
    status = subprocess.run(
        ["git", "--git-dir", str(history_dir), "status"],
        capture_output=True, text=True)
    assert status.returncode != 0
    assert "bare repository" in (status.stderr + status.stdout).lower() \
        or "this operation must be run in a work tree" in (status.stderr + status.stdout).lower()


@pytest.mark.parametrize("thread_name", ["scheduler", "scheduler-thread"])
def test_git_never_called_from_scheduler_thread(monkeypatch, tmp_path, thread_name):
    """§5.4: scheduler スレッドから git サブプロセスが呼ばれないこと。
    `record_version` 内部の `subprocess.run` 呼び出しを検査し、呼び出し元
    スレッド名が 'scheduler' なら AssertionError にするテスト用フック
    (実装は `history_git._assert_not_scheduler_thread()` を record_version
    冒頭に置き、テストはスレッド名から呼んで例外を assert する)。

    確定-8 是正 (2026-08-25、verified-local-round1.md): 旧稿は
    `'scheduler-thread'` という `startswith('scheduler')` にのみ引っかかる
    テスト用の名前でしか検証しておらず、本番の実スレッド名 `'scheduler'`
    (`service.py:1342` `threading.Thread(..., name="scheduler")`) を
    `_assert_not_scheduler_thread` の `startswith` から `==
    'scheduler-thread'` へ退行させる変異 (`a15_src_exact`) が SURVIVED
    だった (=「テストが通る最小実装」に退行させると本番のガードが完全に
    無効化される)。本番名を必ず parametrize に含める形に是正する
    (既存テストの修正だが、これは欠陥の是正 — 確定-8 の申告どおり)。"""
    d, a, c = _make_version(tmp_path / "plugins", "sma")
    errors = []

    def run_as_scheduler():
        try:
            history_git.record_version(
                tmp_path / "plugins" / ".history.git", name="sma",
                artifact_hash=a, content_hash=c, approval_id=1, version_dir=d)
        except history_git.SchedulerThreadForbiddenError as e:
            errors.append(e)

    t = threading.Thread(target=run_as_scheduler, name=thread_name)
    t.start()
    t.join()
    assert len(errors) == 1
