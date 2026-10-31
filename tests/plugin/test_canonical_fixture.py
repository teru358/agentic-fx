from __future__ import annotations

import pandas as pd
import pytest

from agentic_fx.plugin.canonical_fixture import (
    aggregate_to, build_canonical_5m, fixture_data_hash,
)


def test_canonical_fixture_has_utc_ohlcv_weekend_hole_and_gaps():
    df = build_canonical_5m(seed=20261102, days=10)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is not None and str(df.index.tz) == "UTC"
    assert pd.Timestamp("2026-01-02 21:00", tz="UTC") not in df.index
    assert pd.Timestamp("2026-01-04 21:00", tz="UTC") in df.index
    assert (df["open"].iloc[1:].to_numpy() != df["close"].iloc[:-1].to_numpy()).any()


def test_canonical_fixture_semantic_pins_and_hash_are_data_based():
    df = build_canonical_5m(seed=20261102, days=340)
    assert 69_000 < len(df) < 71_000
    assert 80 < df.close.iloc[0] < 220
    assert 80 < df.close.iloc[-1] < 220
    assert 50_000_000 < df.volume.sum() < 500_000_000
    assert fixture_data_hash(df) == fixture_data_hash(df.copy())
    changed = df.copy()
    changed.iloc[0, 0] += 0.001
    assert fixture_data_hash(changed) != fixture_data_hash(df)


def test_aggregate_to_uses_project_ohlcv_resampling():
    df = build_canonical_5m(seed=1, days=2)
    out = aggregate_to(df, "1h")
    assert len(out) < len(df)
    assert out.index.tz is not None
    assert out.volume.sum() == pytest.approx(df.volume.sum())
