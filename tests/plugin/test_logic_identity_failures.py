"""計算が失敗する候補は例外にせず None に畳む。"""
from __future__ import annotations

import pytest

from agentic_fx.plugin.logic_identity import identity_of_dir, logic_hash_of

_CONFIG = "kind: indicator\nparams: {}\n"


def _chain(terms: int) -> str:
    return "x = " + " + ".join(["a"] * terms) + "\n"


@pytest.mark.parametrize("terms", [1500, 5000, 100000])
def test_deep_expression_yields_none_not_an_exception(terms):
    assert logic_hash_of("a = 1\n" + _chain(terms), _CONFIG) is None


def test_moderately_deep_expression_still_hashes():
    assert logic_hash_of("a = 1\n" + _chain(50), _CONFIG) is not None


def test_identity_of_dir_with_a_deep_expression_is_none(tmp_path):
    (tmp_path / "plugin.py").write_text("a = 1\n" + _chain(5000))
    (tmp_path / "config.yaml").write_text(_CONFIG)
    logic, family = identity_of_dir(tmp_path, name="x")
    assert logic is None


@pytest.mark.parametrize("exc", [RecursionError, MemoryError, ValueError])
def test_any_computation_failure_collapses_to_none(monkeypatch, exc):
    def boom(*a, **k):
        raise exc("x")
    monkeypatch.setattr("agentic_fx.plugin.logic_identity.ast.dump", boom)
    assert logic_hash_of("a = 1\n", _CONFIG) is None
