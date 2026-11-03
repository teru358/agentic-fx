from __future__ import annotations

import inspect

import numpy as np
import pandas as pd
import pytest

from agentic_fx.plugin.canonical_fixture import (
    aggregate_to, build_canonical_5m, fixture_data_hash, seasonal_close,
)

UTC = "UTC"


def _ts(text):
    return pd.Timestamp(text, tz=UTC)


@pytest.fixture(scope="module")
def full():
    return build_canonical_5m(seed=20261102, days=340)


def test_canonical_fixture_has_utc_ohlcv_weekend_hole_and_gaps():
    df = build_canonical_5m(seed=20261102, days=10)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is not None and str(df.index.tz) == "UTC"
    assert df.index.is_monotonic_increasing and df.index.is_unique
    assert (df["open"].iloc[1:].to_numpy() != df["close"].iloc[:-1].to_numpy()).any()


def test_weekend_hole_boundaries_are_exact():
    df = build_canonical_5m(seed=1, days=10)
    idx = df.index
    assert _ts("2026-01-01 00:00") == idx[0]
    assert _ts("2026-01-02 20:00") in idx and _ts("2026-01-02 20:55") in idx
    assert _ts("2026-01-02 21:00") not in idx
    assert _ts("2026-01-02 23:55") not in idx
    assert not any(t.weekday() == 5 for t in idx)
    assert _ts("2026-01-04 00:00") not in idx and _ts("2026-01-04 20:55") not in idx
    assert _ts("2026-01-04 21:00") in idx
    assert _ts("2026-01-05 00:00") in idx
    gaps = idx.to_series().diff().dropna()
    assert set(gaps.unique()) == {pd.Timedelta("5min"), pd.Timedelta("48h05min")}
    assert (gaps == pd.Timedelta("48h05min")).sum() == 1


def test_default_arguments_are_the_documented_ones(full):
    params = inspect.signature(build_canonical_5m).parameters
    assert (params["seed"].default, params["days"].default) == (20261102, 340)
    assert fixture_data_hash(build_canonical_5m()) == fixture_data_hash(full)
    assert full.index[0] == _ts("2026-01-01 00:00")
    assert full.index[-1] == _ts("2026-12-06 23:55")


@pytest.mark.parametrize(("timeframe", "expected"), [
    ("5m", "dc010588fc44ff4516eb783be6142c69289fb6e7d07a011860f7a080344975de"),
    ("15m", "ed41035a113b39b5a6c041a72003c1bee88699b64317f416f8733f3b022f9c76"),
    ("1h", "7881c93a468ee967bf9d6e06f04469a8803e98af832d231088b78fa26a58161d"),
    ("4h", "ef92d788da7348ab13fa821c78e90adec33805635a89920ab799a0c27dba7a95"),
    ("1d", "5e156fad4ca4b98af28aa478df89d4e3c524293225369ea0ef65b330dfccf2c6"),
])
def test_canonical_fixture_hashes_are_compatibility_pins(timeframe, expected):
    from agentic_fx.plugin.indicator_pipeline import canonical_bars

    assert canonical_bars(timeframe)[1] == expected


@pytest.mark.parametrize("days", [0, -1])
def test_non_positive_days_are_rejected(days):
    with pytest.raises(ValueError):
        build_canonical_5m(days=days)


def test_one_day_builds_and_seed_changes_the_data():
    one = build_canonical_5m(seed=3, days=1)
    assert len(one) == 24 * 12
    assert len(build_canonical_5m(seed=3, days=2)) == 540
    assert fixture_data_hash(build_canonical_5m(seed=3, days=1)) == fixture_data_hash(one)
    assert fixture_data_hash(build_canonical_5m(seed=4, days=1)) != fixture_data_hash(one)
    assert not np.array_equal(build_canonical_5m(seed=4, days=1).close.to_numpy(), one.close.to_numpy())


def test_canonical_fixture_semantic_pins(full):
    assert 69_000 < len(full) < 71_000
    assert 80 < full.close.iloc[0] < 220
    assert 80 < full.close.iloc[-1] < 220
    assert 50_000_000 < full.volume.sum() < 500_000_000
    assert (full.volume > 0).all() and np.isfinite(full.to_numpy()).all()


def test_seasonal_component_has_the_specified_periods():
    assert seasonal_close(np.array([0])) == pytest.approx([150.0])
    assert seasonal_close(np.array([72])) == pytest.approx([150.0 + 2.0 + 0.5 * np.sin(2 * np.pi * 72 / 2016)])
    k = np.arange(2016 * 3)
    comp = seasonal_close(k)
    assert np.allclose(comp[:2016], comp[2016:4032]) and not np.allclose(comp[:1000], comp[1000:2000])
    assert seasonal_close(np.array([504]))[0] == pytest.approx(148.5)
    assert seasonal_close(np.array([216]))[0] == pytest.approx(150.0 - 2.0 + 0.5 * np.sin(2 * np.pi * 216 / 2016))
    fast = seasonal_close(np.arange(288)) - 0.5 * np.sin(2 * np.pi * np.arange(288) / 2016)
    assert fast.max() == pytest.approx(152.0, abs=1e-3) and fast.min() == pytest.approx(148.0, abs=1e-3)


def test_close_is_seasonal_plus_a_random_walk_with_the_specified_step(full):
    grid = pd.date_range("2026-01-01", periods=340 * 288, freq="5min", tz=UTC)
    k = ((full.index - grid[0]) / pd.Timedelta("5min")).astype(int)
    walk = full.close.to_numpy() - seasonal_close(k)
    steps = np.diff(walk)
    assert steps.std() == pytest.approx(0.05, rel=0.03)
    assert abs(steps.mean()) < 0.002
    assert walk.std() > 1.0  # cumulated, not white
    assert abs(np.corrcoef(steps[:-1], steps[1:])[0, 1]) < 0.02


def test_open_follows_previous_close_with_a_larger_gap_after_the_weekend(full):
    prev_close = full.close.shift(1)
    jump = (full.open - prev_close).iloc[1:]
    opens = full.index[1:]
    after_weekend = (opens.weekday == 6) & (opens.hour == 21) & (opens.minute == 0)
    assert after_weekend.sum() > 40
    assert jump[~after_weekend].std() == pytest.approx(0.02, rel=0.05)
    assert jump[after_weekend].std() == pytest.approx(0.3, rel=0.25)
    assert jump[after_weekend].std() / jump[~after_weekend].std() > 8
    assert abs(jump[~after_weekend].mean()) < 0.001
    assert 0 < abs(full.open.iloc[0] - full.close.iloc[0]) < 0.2


def test_high_low_enclose_open_and_close_with_a_positive_margin(full):
    top = np.maximum(full.open, full.close)
    bottom = np.minimum(full.open, full.close)
    assert (full.high - top).min() >= 0.001
    assert (bottom - full.low).min() >= 0.001
    assert (full.high - top).mean() == pytest.approx(0.015, abs=0.003)
    assert (bottom - full.low).mean() == pytest.approx(0.015, abs=0.003)
    assert (full.high > full.low).all()
    assert ((full.high - top) > 0).all() and ((bottom - full.low) > 0).all()
    assert not np.allclose((full.high - top).to_numpy(), (bottom - full.low).to_numpy())


def test_volume_is_lognormal_with_a_level_per_utc_band(full):
    log_vol = np.log(full.volume)
    bands = {"asia": (0, 7), "london": (7, 13), "overlap": (13, 21), "rollover": (21, 24)}
    median = {}
    for name, (start, stop) in bands.items():
        part = log_vol[(full.index.hour >= start) & (full.index.hour < stop)]
        median[name] = part.median()
        assert part.std() == pytest.approx(0.35, rel=0.05)
    assert median["overlap"] > median["london"] > median["rollover"] > median["asia"]
    assert median["asia"] == pytest.approx(6.8, abs=0.02)
    assert median["london"] == pytest.approx(7.5, abs=0.02)
    assert median["overlap"] == pytest.approx(7.9, abs=0.02)
    assert median["rollover"] == pytest.approx(7.1, abs=0.02)


def test_hash_is_data_based_and_sensitive_to_every_part(full):
    df = full.iloc[:500].copy()
    base = fixture_data_hash(df)
    assert fixture_data_hash(df.copy()) == base
    assert len(base) == 64
    for position in range(5):
        changed = df.copy()
        changed.iloc[0, position] += 0.001
        assert fixture_data_hash(changed) != base, position
    tiny = df.copy()
    tiny.iloc[0, 0] += 1e-9
    assert fixture_data_hash(tiny) != base
    shifted = df.copy()
    shifted.index = shifted.index + pd.Timedelta("1min")
    assert fixture_data_hash(shifted) != base
    vol = df.copy()
    vol.iloc[3, 4] *= 1.5
    assert fixture_data_hash(vol) != base
    assert fixture_data_hash(df.iloc[:-1]) != base


@pytest.mark.parametrize("columns", [
    ["open", "high", "low", "volume", "close"],
    ["open", "high", "low", "close"],
    ["open", "high", "low", "close", "volume", "extra"],
    ["Open", "High", "Low", "Close", "Volume"],
])
def test_hash_requires_the_canonical_columns_in_order(full, columns):
    df = full.iloc[:20].copy()
    if set(columns) <= set(df.columns):
        df = df.loc[:, columns]
    else:
        df = pd.DataFrame(np.ones((20, len(columns))), index=df.index, columns=columns)
    with pytest.raises(ValueError):
        fixture_data_hash(df)


def test_aggregate_to_uses_project_ohlcv_resampling():
    df = build_canonical_5m(seed=1, days=2)
    out = aggregate_to(df, "1h")
    assert len(out) < len(df)
    assert out.index.tz is not None
    assert out.volume.sum() == pytest.approx(df.volume.sum())
    first = df.loc[_ts("2026-01-01 00:00"):_ts("2026-01-01 00:55")]
    row = out.loc[_ts("2026-01-01 00:00")]
    assert row.open == first.open.iloc[0] and row.close == first.close.iloc[-1]
    assert row.high == first.high.max() and row.low == first.low.min()
    assert row.volume == pytest.approx(first.volume.sum())
    assert (out.index.minute == 0).all()
    four = aggregate_to(df, "4h")
    assert len(four) < len(out) and (four.index.hour % 4 == 0).all()
    assert aggregate_to(df, "5m").equals(df) or len(aggregate_to(df, "5m")) == len(df)


def test_hash_does_not_depend_on_the_index_unit(full):
    df = full.iloc[:200].copy()
    us = df.copy()
    us.index = df.index.as_unit("us")
    ns = df.copy()
    ns.index = df.index.as_unit("ns")
    assert fixture_data_hash(us) == fixture_data_hash(ns)
