"""検収是正 C3 (test-hygiene 設計書 2026-09-12):
`tests/conftest.py::_new_untracked` の単体 pin。

`_guard_repo_root_has_no_new_untracked_files` は元々 `before != after` で
判定していたため、テスト中に untracked ファイルが**減った**場合にも
fail していた (文言「新規…残った」と矛盾する偽陽性)。判定本体を
`_new_untracked(before, after)` として切り出し、ここで単体検証する。
"""
from __future__ import annotations

from tests.conftest import _new_untracked


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
