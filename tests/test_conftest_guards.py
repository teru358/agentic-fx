"""検収是正 C3 (test-hygiene 設計書 2026-09-12):
`tests/conftest.py::_new_untracked` の単体 pin。

`_guard_repo_root_has_no_new_untracked_files` は元々 `before != after` で
判定していたため、テスト中に untracked ファイルが**減った**場合にも
fail していた (文言「新規…残った」と矛盾する偽陽性)。判定本体を
`_new_untracked(before, after)` として切り出し、ここで単体検証する。
"""
from __future__ import annotations

from tests.conftest import _new_untracked, _top_level_untracked


def test_new_untracked_returns_empty_set_when_a_file_is_removed():
    """減少ケース (before にあり after に無い) は「新規」ではないので
    空集合になる — before != after の旧判定はここで誤って fail していた。"""
    before = {"a.txt", "b.txt"}
    after = {"a.txt"}
    assert _new_untracked(before, after) == set()


def test_new_untracked_returns_added_names_only():
    before = {"a.txt"}
    after = {"a.txt", "b.txt"}
    assert _new_untracked(before, after) == {"b.txt"}


def test_new_untracked_returns_empty_set_when_unchanged():
    before = {"a.txt"}
    after = {"a.txt"}
    assert _new_untracked(before, after) == set()


def test_new_untracked_handles_simultaneous_add_and_remove():
    """同時に増減した場合は「増えた分」だけを見る — 減った分は無視する。"""
    before = {"a.txt", "b.txt"}
    after = {"a.txt", "c.txt"}
    assert _new_untracked(before, after) == {"c.txt"}


def test_new_untracked_is_empty_when_either_side_is_none():
    """git が使えず検査不能 (`_sig` が `None` を返す) な場合は「検査対象
    外」— 空集合を返し、呼び出し側で fail させない。"""
    assert _new_untracked(None, {"a.txt"}) == set()
    assert _new_untracked({"a.txt"}, None) == set()
    assert _new_untracked(None, None) == set()


# --- ローカル 1 周目 pin (P1, test-hygiene 2026-09-12):
# `_top_level_untracked` の porcelain 解析 pin ------------------------

def test_top_level_untracked_excludes_subdirectory_entries():
    """サブディレクトリ配下の untracked (`??` 行の path に `/` を含む)
    は非再帰の対象外 — 意図した一時ファイルまで拾って過検出にしない。"""
    assert _top_level_untracked("?? sub/x.txt\n") == set()


def test_top_level_untracked_unquotes_and_includes_quoted_non_ascii_path():
    """git がクォートした path (空白・非 ASCII を含む場合) は前後の
    クォートだけを外して top-level エントリとして含める。"""
    assert _top_level_untracked('?? "日本 語.txt"\n') == {"日本 語.txt"}


def test_top_level_untracked_excludes_tracked_changes_and_ignored_paths():
    """`??` (untracked) 以外の行 — 追跡済みファイルへの変更 (` M`) や
    `.gitignore` 済み (`!!`、`--ignored=` 指定時のみ現れる) — は対象外。"""
    porcelain = " M tracked.py\n!! ignored\n"
    assert _top_level_untracked(porcelain) == set()


def test_top_level_untracked_includes_new_top_level_directory():
    """`?? newdir/` (新規ディレクトリそのもの、末尾 `/` のみで内部の `/`
    を含まない) は top-level エントリとして含める。"""
    assert _top_level_untracked("?? newdir/\n") == {"newdir/"}
