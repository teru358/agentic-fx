"""The product ships no news sources: nothing seeds them and no module defines them."""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
FORBIDDEN = re.compile(r"seed_default_sources|DEFAULT_SOURCES|default_sources")


def test_no_default_source_symbols_exist():
    import agentic_fx.datafeed.news_collector as collector
    import agentic_fx.service as service

    for module in (collector, service):
        assert not hasattr(module, "seed_default_sources")
        assert not hasattr(module, "DEFAULT_SOURCES")
    assert "DEFAULT_SOURCES" not in getattr(collector, "__all__", ())
    assert "seed_default_sources" not in getattr(collector, "__all__", ())


def test_the_default_sources_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agentic_fx.datafeed.default_sources")


def test_the_source_tree_never_mentions_default_sources():
    hits = [str(path.relative_to(SRC)) for path in SRC.rglob("*.py")
            if FORBIDDEN.search(path.read_text(encoding="utf-8"))]
    assert hits == []
