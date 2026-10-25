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
