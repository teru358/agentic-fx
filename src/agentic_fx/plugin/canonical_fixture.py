"""Deterministic OHLCV input used by indicator gate probes."""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

from agentic_fx.datafeed.bars import pandas_rule, resample


# Log-volume level per UTC hour band: Asia, London morning, London/NY overlap, rollover.
_VOLUME_BANDS = ((0, 7, 6.8), (7, 13, 7.5), (13, 21, 7.9), (21, 24, 7.1))
_VOLUME_SIGMA = 0.35


def seasonal_close(k: np.ndarray) -> np.ndarray:
    """Deterministic close component at five-minute grid positions k."""
    k = np.asarray(k, dtype=float)
    return (150.0 + 2.0 * np.sin(2.0 * np.pi * k / 288.0)
            + 0.5 * np.sin(2.0 * np.pi * k / 2016.0))


def build_canonical_5m(*, seed: int = 20261102, days: int = 340) -> pd.DataFrame:
    """Build a UTC five-minute market-shaped series with the FX weekend gap."""
    if days <= 0:
        raise ValueError("days must be positive")
    grid = pd.date_range("2026-01-01", periods=days * 24 * 12,
                         freq="5min", tz="UTC")
    # FX closes Friday 21:00 UTC and resumes Sunday 21:00 UTC.
    closed = ((grid.weekday == 4) & (grid.hour >= 21)) | (grid.weekday == 5) | (
        (grid.weekday == 6) & (grid.hour < 21))
    k = np.arange(len(grid))[~closed]
    index = grid[~closed]
    n = len(index)
    rng = np.random.default_rng(seed)
    close = seasonal_close(k) + np.cumsum(rng.normal(0.0, 0.05, n))
    weekend_open = (index.weekday == 6) & (index.hour == 21) & (index.minute == 0)
    open_sd = np.where(weekend_open, 0.3, 0.02)
    open_ = np.empty(n)
    open_[0] = close[0] + rng.normal(0.0, 0.02)
    open_[1:] = close[:-1] + rng.normal(0.0, 1.0, n - 1) * open_sd[1:]
    reach = np.abs(rng.normal(0.015, 0.006, (2, n))) + 0.001
    high = np.maximum(open_, close) + reach[0]
    low = np.minimum(open_, close) - reach[1]
    level = np.zeros(n)
    for start, stop, mean in _VOLUME_BANDS:
        level[(index.hour >= start) & (index.hour < stop)] = mean
    volume = rng.lognormal(mean=level, sigma=_VOLUME_SIGMA)
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": volume}, index=index)


def aggregate_to(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate with the production OHLCV implementation."""
    return resample(df, pandas_rule(timeframe))


def fixture_data_hash(df: pd.DataFrame) -> str:
    """Return a stable digest of the actual index and OHLCV values."""
    required = ("open", "high", "low", "close", "volume")
    if tuple(df.columns) != required:
        raise ValueError("fixture must have canonical OHLCV columns")
    digest = hashlib.sha256()
    index = pd.DatetimeIndex(df.index)
    digest.update(np.asarray(index.as_unit("ns").asi8, dtype="<i8").tobytes())
    digest.update(np.asarray(df.loc[:, required], dtype="<f8").tobytes())
    return digest.hexdigest()
