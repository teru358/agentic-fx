"""Validated identity of the imported price history used by a backtest."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from agentic_fx.store.ohlcv import IMPORT_SOURCES


class DatasetError(ValueError):
    """A history dataset cannot be used for replay."""


_WIDTHS = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5),
           "15m": timedelta(minutes=15)}


@dataclass(frozen=True, slots=True)
class HistoryDataset:
    source: str
    base_interval: str

    def __post_init__(self) -> None:
        if self.source not in IMPORT_SOURCES:
            raise DatasetError(
                f"source must be one of {sorted(IMPORT_SOURCES)}, got {self.source!r}")
        if self.base_interval not in _WIDTHS:
            raise DatasetError(
                f"base_interval must be one of {sorted(_WIDTHS)}, "
                f"got {self.base_interval!r}")

    @property
    def width(self) -> timedelta:
        return _WIDTHS[self.base_interval]

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "base_interval": self.base_interval}
