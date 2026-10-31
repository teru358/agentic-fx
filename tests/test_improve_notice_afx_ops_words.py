"""daemon が許す (result_code, reason_code) と、afx-ops の見出しの表の語の一致。

afx-ops は import せず、`ast` でリテラルを読むだけにする。"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agentic_fx import improve_notice_codes as codes

RENDER = Path(__file__).resolve().parents[1] / "afx-ops" / "afx_ops" / "gate" / "render.py"


def _heading_table(path: Path = RENDER) -> dict | None:
    """(result_code, reason_code) を鍵にした辞書リテラルのうち、最大のものを返す。"""
    if not path.is_file():
        return None
    best: dict | None = None
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if not isinstance(value, ast.Dict) or not value.keys:
            continue
        try:
            table = {ast.literal_eval(k): None for k in value.keys if k is not None}
        except (ValueError, SyntaxError):
            continue
        if len(table) == len(value.keys) and all(
                isinstance(k, tuple) and len(k) == 2 and isinstance(k[0], str)
                and (k[1] is None or isinstance(k[1], str)) for k in table):
            if best is None or len(table) > len(best):
                best = table
    return best


def test_table_reader_finds_the_largest_pair_keyed_dict(tmp_path):
    sample = tmp_path / "render.py"
    sample.write_text(
        "OTHER = {('a', 'b'): 1}\n"
        "HEADINGS = {('submitted', None): 'x', ('gate_failed', 'unprofitable'): 'y'}\n"
        "NOT_PAIRS = {'k': 1, 'j': 2, 'i': 3}\n", encoding="utf-8")
    assert set(_heading_table(sample)) == {("submitted", None),
                                           ("gate_failed", "unprofitable")}
    assert _heading_table(tmp_path / "missing.py") is None


_TABLE = _heading_table()
_NEEDS_TABLE = pytest.mark.skipif(
    _TABLE is None, reason="afx-ops の見出しの表が T2 で入るまで")


@_NEEDS_TABLE
def test_every_key_of_the_afx_ops_table_is_an_allowed_pair():
    assert set(_TABLE) <= set(codes.ALLOWED_PAIRS)


@_NEEDS_TABLE
def test_every_pair_with_a_dedicated_heading_is_in_the_afx_ops_table():
    assert set(codes.PAIRS_WITH_DEDICATED_HEADING) <= set(_TABLE)


def _heading_texts(path: Path = RENDER) -> dict:
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if (isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(value, ast.Dict)
                and any(isinstance(t, ast.Name) and t.id == "IMPROVE_HEADINGS"
                        for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))):
            return {ast.literal_eval(k): ast.literal_eval(v)
                    for k, v in zip(value.keys, value.values)}
    raise AssertionError("IMPROVE_HEADINGS not found")


@_NEEDS_TABLE
@pytest.mark.parametrize("notice,fixed_words", [
    (codes.NOTICE_NOT_IMPROVED, "配備中の版を上回らず見送り"),
    (codes.NOTICE_SAME_NAME_CONTRACT_CHANGED, "同名更新の条件変更"),
])
def test_the_two_comparison_words_are_allowed_and_have_their_fixed_headings(notice, fixed_words):
    pair = (notice.result_code, notice.reason_code)
    assert pair in codes.ALLOWED_PAIRS and pair in codes.PAIRS_WITH_DEDICATED_HEADING
    assert fixed_words in _heading_texts()[pair]
