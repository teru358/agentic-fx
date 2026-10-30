"""Validated identity of the imported price history used by a backtest."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from datetime import timedelta

from agentic_fx.store.ohlcv import IMPORT_SOURCES


class DatasetError(ValueError):
    """A history dataset cannot be used for replay."""


class HistorySourceUnconfiguredError(DatasetError):
    """No history source has been chosen (``backtest.eval_source`` is ``none``)."""


_WIDTHS = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5),
           "15m": timedelta(minutes=15)}
UNCONFIGURED_SOURCE = "none"
HISTORY_SOURCE_UNCONFIGURED_REASON = "history_source_unconfigured"
_CONFIGURED_OR_UNCONFIGURED_SOURCES = frozenset((*IMPORT_SOURCES, UNCONFIGURED_SOURCE))


@dataclass(frozen=True, slots=True)
class HistoryDataset:
    source: str
    base_interval: str

    def __post_init__(self) -> None:
        if self.source not in _CONFIGURED_OR_UNCONFIGURED_SOURCES:
            raise DatasetError(
                "source must be one of "
                f"{sorted(_CONFIGURED_OR_UNCONFIGURED_SOURCES)}, got {self.source!r}")
        if self.base_interval not in _WIDTHS:
            raise DatasetError(
                f"base_interval must be one of {sorted(_WIDTHS)}, "
                f"got {self.base_interval!r}")

    @property
    def width(self) -> timedelta:
        return _WIDTHS[self.base_interval]

    @property
    def unconfigured(self) -> bool:
        return self.source == UNCONFIGURED_SOURCE

    def require_configured(self) -> None:
        if self.unconfigured:
            raise HistorySourceUnconfiguredError(HISTORY_SOURCE_UNCONFIGURED_REASON)

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "base_interval": self.base_interval}


def literal_reference_coordinates(resolved) -> list[tuple[str, str]]:
    """Literal-symbol `(symbol, reference_timeframe)` declarations, in alias order."""
    return [(item.literal_symbol, item.timeframe) for item in resolved.items
            if item.symbol_mode == "literal" and item.literal_symbol is not None]


def reference_history_missing_error(symbol: str, timeframe: str, reason: str) -> str:
    return f"reference_history_missing({symbol}, {timeframe}, {reason})"


def missing_reference_history(conn, dataset: HistoryDataset,
                              references: Iterable[tuple[str, str]]) -> tuple[str, str, str] | None:
    """Return the first literal reference with no rows in this replay dataset.

    This deliberately tests existence only.  Gaps, stale bars and indicator
    warm-up remain strategy-level availability outcomes rather than a replay
    admission failure.
    """
    dataset.require_configured()
    for symbol, timeframe in references:
        row = conn.execute(
            "SELECT 1 FROM ohlcv_history WHERE symbol=? AND interval=? AND source=? LIMIT 1",
            (symbol, dataset.base_interval, dataset.source)).fetchone()
        if row is None:
            return symbol, timeframe, "no_rows"
    return None
