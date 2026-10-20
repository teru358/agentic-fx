"""ロジック hash が落とすもの・落とさないものを 1 種類ずつ固定する。"""
from __future__ import annotations

import pytest

from agentic_fx.plugin.logic_identity import logic_hash_of

CONFIG = "kind: indicator\noutputs: [v]\n"


def _hash(source: str) -> str:
    value = logic_hash_of(source, CONFIG)
    assert value is not None
    return value


@pytest.mark.parametrize("left,right", [
    ("x = 0.5\n", "x = 0.75\n"),
    ("x = -0.5\n", "x = 2.25\n"),
    ("x = 3\n", "x = 12\n"),
    ("x = 1j\n", "x = 2j\n"),
    ("x = 1e-3\n", "x = 4e5\n"),
])
def test_numeric_literals_of_every_kind_are_dropped(left, right):
    assert _hash(left) == _hash(right)


def test_booleans_and_strings_are_kept():
    assert _hash("x = True\n") != _hash("x = False\n")
    assert _hash("x = 'a'\n") != _hash("x = 'b'\n")


@pytest.mark.parametrize("with_doc,without_doc", [
    ('"""module doc"""\nx = 1\n', "x = 1\n"),
    ('def f():\n    """doc"""\n    return 1\n', "def f():\n    return 1\n"),
    ('async def f():\n    """doc"""\n    return 1\n',
     "async def f():\n    return 1\n"),
    ('class C:\n    """doc"""\n    y = 1\n', "class C:\n    y = 1\n"),
    ('class C:\n    """doc"""\n', "class C:\n    pass\n"),
])
def test_docstrings_of_every_node_kind_are_dropped(with_doc, without_doc):
    assert _hash(with_doc) == _hash(without_doc)


@pytest.mark.parametrize("left,right", [
    ("f(rationale='a')\n", "f(rationale='b')\n"),
    ("f(reason='a')\n", "f(reason=f'b{1}')\n"),
    ("f(description='a')\n", "f(description='b')\n"),
    ("f(note='a')\n", "f(note='b')\n"),
    ("d = {'rationale': 'a'}\n", "d = {'rationale': 'b'}\n"),
    ("d = {'note': 'a'}\n", "d = {'note': 'b'}\n"),
])
def test_explanatory_strings_are_dropped(left, right):
    assert _hash(left) == _hash(right)


def test_other_keyword_strings_and_dict_values_are_kept():
    assert _hash("f(mode='a')\n") != _hash("f(mode='b')\n")
    assert _hash("d = {'mode': 'a'}\n") != _hash("d = {'mode': 'b'}\n")
