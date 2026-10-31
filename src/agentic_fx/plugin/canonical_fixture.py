"""Deterministic OHLCV input used by indicator gate probes."""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

from agentic_fx.datafeed.bars import pandas_rule, resample


def build_canonical_5m(*, seed: int = 20261102, days: int = 340) -> pd.DataFrame:
    """Build a UTC five-minute market-shaped series with the FX weekend gap."""
    if days <= 0:
        raise ValueError("days must be positive")
    index = pd.date_range("2026-01-01", periods=days * 24 * 12,
                          freq="5min", tz="UTC")
    # FX closes Friday 21:00 UTC and resumes Sunday 21:00 UTC.
    closed = ((index.weekday == 4) & (index.hour >= 21)) | (index.weekday == 5) | (
        (index.weekday == 6) & (index.hour < 21))
    index = index[~closed]
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 0.0003, len(index))
    weekend_open = (index.weekday == 6) & (index.hour == 21) & (index.minute == 0)
    steps[weekend_open] += rng.normal(0.0, 0.03, weekend_open.sum())
    close = 150.0 * np.exp(np.cumsum(steps))
    open_ = np.empty_like(close)
    open_[0] = close[0] * (1.0 + rng.normal(0, 0.0003))
    open_[1:] = close[:-1] * (1.0 + rng.normal(0, 0.00015, len(close) - 1))
    spread = np.abs(rng.normal(0.015, 0.006, len(index))) + 0.001
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.lognormal(mean=7.55, sigma=0.35, size=len(index))
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
    digest.update(np.asarray(df.index.asi8, dtype="<i8").tobytes())
    digest.update(np.asarray(df.loc[:, required], dtype="<f8").tobytes())
    return digest.hexdigest()
