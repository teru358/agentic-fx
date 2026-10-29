"""ohlcv indicator plugin の自己テスト。

このファイルは bless の pytest ゲート (subprocess + Landlock) で毎回走る。
`check_source(..., extra_allowed={"pytest", "plugin"})` を通る範囲で書くこと。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from plugin import compute

OUTPUTS = ("open", "high", "low", "close", "volume")


def _mkdf(n: int = 60, *, seed: int = 0, base: float = 150.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = base + np.cumsum(rng.normal(0.0, base * 0.002, n))
    high = close + np.abs(rng.normal(0.0, 0.1, n))
    low = close - np.abs(rng.normal(0.0, 0.1, n))
    open_ = np.concatenate([close[:1], close[:-1]])
    index = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                          "volume": np.arange(1.0, n + 1.0)}, index=index)


def test_outputs_are_open_high_low_close_volume_in_that_order():
    out = compute(_mkdf(), {})
    assert list(out) == list(OUTPUTS)


def test_every_output_equals_its_input_column_exactly():
    df = _mkdf()
    out = compute(df, {})
    for column in OUTPUTS:
        assert isinstance(out[column], pd.Series), column
        assert out[column].index.equals(df.index), column
        assert out[column].to_numpy().tolist() == df[column].to_numpy().tolist(), column


def test_input_frame_is_not_mutated_and_outputs_are_copies():
    df = _mkdf()
    before = df.copy(deep=True)
    out = compute(df, {})
    assert df.equals(before)
    out["close"].iloc[0] = -1.0
    assert df.equals(before)


def test_no_nan_is_created():
    out = compute(_mkdf(), {})
    for column in OUTPUTS:
        assert not bool(out[column].isna().any()), column


def test_prefix_consistency_on_every_prefix():
    df = _mkdf()
    full = compute(df, {})
    for t in range(1, len(df) + 1):
        sub = compute(df.iloc[:t], {})
        for column in OUTPUTS:
            assert sub[column].to_numpy().tolist() == full[column].iloc[:t].to_numpy().tolist(), (
                column, t)


def test_short_and_empty_frames_return_all_declared_keys():
    for n in (0, 1, 3):
        out = compute(_mkdf(n=n), {})
        assert list(out) == list(OUTPUTS)
        for column in OUTPUTS:
            assert len(out[column]) == n


def test_repeated_calls_are_deterministic():
    df = _mkdf()
    first = compute(df, {})
    second = compute(df, {})
    for column in OUTPUTS:
        assert first[column].equals(second[column]), column


@pytest.mark.parametrize("params", [{"period": 5}, {"x": 1}, {0: 1}])
def test_unknown_params_raise_value_error(params):
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=10), params)


@pytest.mark.parametrize("params", [None, [], "p", 3])
def test_non_mapping_params_raise_value_error(params):
    with pytest.raises(ValueError, match=r"^params\."):
        compute(_mkdf(n=10), params)
