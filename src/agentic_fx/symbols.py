"""Configured market-symbol membership shared by read and reference paths."""
from __future__ import annotations


def readable_symbols(settings) -> list[str]:
    """Return trading pairs followed by watch symbols, preserving settings order."""
    return [*settings.pairs, *settings.datafeed.watch_symbols]
