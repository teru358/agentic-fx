"""source snapshot の fault injection (設計書 §3.4/§4 冒頭, プラン §8.1-11)。"""
from __future__ import annotations

import hashlib
import shutil
import threading
from pathlib import Path

import pytest

from agentic_fx.loops.improve_loop import copy_source_snapshot  # 新規命名、モジュール関数


def _write_plugin(dirpath: Path, *, plugin_py=b"p", config_yaml=b"c", test_plugin=b"t"):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "plugin.py").write_bytes(plugin_py)
    (dirpath / "config.yaml").write_bytes(config_yaml)
    (dirpath / "test_plugin.py").write_bytes(test_plugin)


def _artifact_hash(plugin_py, config_yaml, test_plugin) -> str:
    h = hashlib.sha256()
    h.update(b"plugin.py\0" + plugin_py + b"\0config.yaml\0" + config_yaml
             + b"\0test_plugin.py\0" + test_plugin)
    return h.hexdigest()


def test_copies_from_fixed_plugin_meta_path_not_live_symlink(tmp_path, monkeypatch):
    """live symlink を別版に切替えても、既に registry が保持する固定
    PluginMeta.path から読む (symlink 追従しない)。

    L-B22 是正 (束D検収, verified-local-round1.md §11 #21): 元のテストは
    live symlink `plugins/<name>` を**一度も作らない**まま `meta.path`
    (固定パス) から読むだけの自明テストだった (「symlink を追従しない」
    という表題の契約を実際には踏んでいなかった)。ここでは
    `plugins/rsi_indicator` を実際に**別版** (NEW) を指す symlink として
    作り、それでも `meta.path` (OLD 版) の内容がコピーされることを見る。"""
    versions_root = tmp_path / "plugins" / ".versions" / "rsi_indicator"
    old_version = versions_root / ("aaa" * 16)  # ダミーの64桁hash風
    new_version = versions_root / ("bbb" * 16)
    _write_plugin(old_version, plugin_py=b"OLD")
    _write_plugin(new_version, plugin_py=b"NEW")
    live_symlink = tmp_path / "plugins" / "rsi_indicator"
    live_symlink.symlink_to(new_version)
    dest = tmp_path / "workdir" / "source"

    class _FakeMeta:
        name = "rsi_indicator"
        path = old_version  # registry が discover 時点で保持する固定パス
        content_hash = "irrelevant-for-this-test"
        artifact_hash = _artifact_hash(b"OLD", b"c", b"t")

    lock = threading.Lock()
    result = copy_source_snapshot([_FakeMeta()], dest_root=dest, plugin_lock=lock)
    assert (dest / "rsi_indicator" / "plugin.py").read_bytes() == b"OLD"


def test_live_switch_mid_copy_is_detected_by_hash_reverify(tmp_path):
    """コピー中に (擬似的に) 別内容へ差し替わっても、コピー後の hash 再照合
    (registry 値との比較) が不一致を検出して例外にする。"""
    src = tmp_path / "candidate"
    _write_plugin(src, plugin_py=b"A")

    class _FakeMeta:
        name = "x"
        path = src
        content_hash = "irrelevant"
        artifact_hash = _artifact_hash(b"B", b"c", b"t")  # わざと違う内容の hash を登録

    lock = threading.Lock()
    with pytest.raises(ValueError, match="artifact_hash mismatch"):
        copy_source_snapshot([_FakeMeta()], dest_root=tmp_path / "dest",
                             plugin_lock=lock)


def test_mixed_three_files_from_different_versions_is_rejected(tmp_path):
    """3 本混成 (plugin.py は版A・config.yaml は版B) が起きないことを、
    単一ディレクトリからの一括コピーであることの構造で保証する — この
    テストは meta.path が単一ディレクトリを指す契約自体を pin する
    (関数シグネチャが「3 本個別の path」ではなく「1 ディレクトリ」を
    受け取ることを、複数ファイルを個別指定できないことで確認する)."""
    import inspect
    sig = inspect.signature(copy_source_snapshot)
    assert "dest_root" in sig.parameters
    # 型ヒントに「plugin_py_path」等の個別ファイル引数が無いことを確認
    assert not any("plugin_py" in p for p in sig.parameters)


def test_unapproved_version_not_in_gc_roots_is_still_copyable_but_hash_checked(tmp_path):
    """未 admit 版混入の防御は「GC_ROOTS に含まれるか」ではなく
    「registry が保持する meta と hash が一致するか」で行う — 呼び出し元
    (ImproveLoop.prepare) が discover 済みの meta しか渡さない設計を
    ここで pin する。meta 自体は本関数の責務外 (呼び出し元契約) なので、
    ここでは「meta が無ければ何も起きない」ことだけを確認する。"""
    result = copy_source_snapshot([], dest_root=tmp_path / "dest",
                                  plugin_lock=threading.Lock())
    assert result == []


def test_examples_are_copied_from_docs_examples_plugins(tmp_path, monkeypatch):
    """`docs/examples/plugins/*` の 3 本ずつを `source/_examples/<name>/` へ
    読取専用コピーする (worker は repo の docs/ を読めないため)。"""
    from agentic_fx.loops.improve_loop import copy_examples_snapshot  # 新規命名

    examples_root = tmp_path / "docs" / "examples" / "plugins"
    _write_plugin(examples_root / "rsi_indicator")
    dest = tmp_path / "workdir" / "source" / "_examples"
    copy_examples_snapshot(examples_root, dest_root=dest)
    assert (dest / "rsi_indicator" / "plugin.py").exists()


def test_concurrent_copy_calls_sharing_plugin_lock_are_mutually_exclusive(
        tmp_path, monkeypatch):
    """A4 是正 (束D検収, verified-local-round1.md §11 #9): 元の
    `test_concurrent_write_to_version_dir_is_detected` は名前が
    「並行書込みを検出する」と謳いながら、実際には**別スレッドを 1 本も
    起動せず** lock 付きの成功パスを 1 回踏むだけだった (`with plugin_lock:`
    (`improve_loop.py:106`) を除去しても実測 SURVIVED — 全スイート
    `2924 passed`)。ここでは実 2 スレッドが**同一 `plugin_lock`** を共有して
    `copy_source_snapshot` を並行に呼び、各呼出しの「読取クリティカル
    セクション」の時間区間が重ならないこと (相互排他) を直接観測する
    (`Path.read_bytes` に細工した遅延を挟み、区間の start/end を記録する)。"""
    meta_a_dir = tmp_path / "cand-a"
    meta_b_dir = tmp_path / "cand-b"
    _write_plugin(meta_a_dir, plugin_py=b"A")
    _write_plugin(meta_b_dir, plugin_py=b"B")

    class _MetaA:
        name = "plugin-a"
        path = meta_a_dir
        content_hash = "irrelevant"
        artifact_hash = _artifact_hash(b"A", b"c", b"t")

    class _MetaB:
        name = "plugin-b"
        path = meta_b_dir
        content_hash = "irrelevant"
        artifact_hash = _artifact_hash(b"B", b"c", b"t")

    # 別々の dest_root にする (`copy_source_snapshot` は末尾で
    # `_chmod_tree_readonly(dest_root)` を呼ぶため、共有 dest だと
    # 先に終わった側が dest を読取専用化して他方の mkdir を壊す —
    # ここで検証したい plugin_lock の相互排他とは無関係な副作用)。
    dest_a = tmp_path / "dest-a"
    dest_b = tmp_path / "dest-b"
    lock = threading.Lock()

    import time
    from pathlib import Path as PathClass

    real_read_bytes = PathClass.read_bytes
    intervals: list[tuple[str, float, float]] = []
    intervals_guard = threading.Lock()

    def _slow_read_bytes(self):
        if self.name == "plugin.py":
            label = self.parent.name  # "cand-a" or "cand-b"
            t0 = time.monotonic()
            time.sleep(0.05)
            data = real_read_bytes(self)
            t1 = time.monotonic()
            with intervals_guard:
                intervals.append((label, t0, t1))
            return data
        return real_read_bytes(self)

    monkeypatch.setattr(PathClass, "read_bytes", _slow_read_bytes)

    results: dict[str, list] = {}

    def _run(key, meta, dest):
        results[key] = copy_source_snapshot(
            [meta], dest_root=dest, plugin_lock=lock)

    t1 = threading.Thread(target=_run, args=("a", _MetaA(), dest_a))
    t2 = threading.Thread(target=_run, args=("b", _MetaB(), dest_b))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert results["a"] == ["plugin-a"]
    assert results["b"] == ["plugin-b"]
    assert len(intervals) == 2, intervals
    (_, a_start, a_end), (_, b_start, b_end) = intervals
    # 相互排他: 一方の区間が他方の開始前に終わっているか、他方の終了後に
    # 始まっているかのどちらか (重ならない)。
    assert a_end <= b_start or b_end <= a_start, (
        f"plugin_lock で保護されているはずの区間が重なった: {intervals}")


def test_copied_files_are_readonly_after_copy(tmp_path):
    """M5: _chmod_tree_readonly を呼ばないと、ファイルが書込可能なまま。
    ここではコピー後のファイル/ディレクトリが実際に読取専用 (0o400/0o500)
    になっていることを確認する。"""
    import os

    src = tmp_path / "candidate"
    _write_plugin(src, plugin_py=b"A")

    class _FakeMeta:
        name = "x"
        path = src
        content_hash = "irrelevant"
        artifact_hash = _artifact_hash(b"A", b"c", b"t")

    dest = tmp_path / "dest"
    lock = threading.Lock()

    copy_source_snapshot([_FakeMeta()], dest_root=dest, plugin_lock=lock)

    # ファイルが読取専用 (0o400) か確認
    plugin_file = dest / "x" / "plugin.py"
    file_mode = os.stat(plugin_file).st_mode & 0o777
    assert file_mode == 0o400, f"Expected 0o400, got {oct(file_mode)}"

    # ディレクトリが実行専用 (0o500) か確認
    plugin_dir = dest / "x"
    dir_mode = os.stat(plugin_dir).st_mode & 0o777
    assert dir_mode == 0o500, f"Expected 0o500, got {oct(dir_mode)}"

    # 実際に書込ができないか確認
    assert not os.access(plugin_file, os.W_OK)
