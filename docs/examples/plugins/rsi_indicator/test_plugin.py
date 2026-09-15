"""rsi_indicator の最小テスト。

docs/examples/plugins/ は discover の対象外 (承認・本番配線とは独立の
サンプル)。承認フロー (プラン 7 Task 6) はこのファイルを実行して plugin
提出者が「動くテスト」を書いていることを検証する。

[indicator-consumption-wiring] 2026-09-14: 戻り値を**系列** (`pd.Series`、
df と同じ index、warmup 行は NaN) にした。`config.yaml` の `outputs: [rsi]`
が宣言キー — ハーネスは毎回この集合と完全一致することを検証する。
"""
from __future__ import annotations

import pandas as pd

from plugin import compute


def _df(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "volume": [1.0] * len(closes)},
        index=idx)


def test_compute_returns_declared_output_key_only():
    out = compute(_df([100 + i * 0.1 for i in range(30)]), {})
    assert set(out) == {"rsi"}


def test_compute_returns_series_aligned_to_df_index():
    df = _df([100 + i * 0.1 for i in range(30)])
    out = compute(df, {})
    assert isinstance(out["rsi"], pd.Series)
    assert out["rsi"].index.equals(df.index)


def test_warmup_rows_are_nan_and_later_rows_have_values():
    df = _df([100 + i * 0.1 for i in range(30)])
    rsi = compute(df, {})["rsi"]
    # period=14 (`min_periods=14`) → **先頭 14 行 (index 0..13) が NaN**、
    # index 14 (15 本目) から値が入る。probe 実測 (`tmp/plan-indicator-wiring/
    # probe_fixture.txt`) と設計書 §6 の warmup 記述と一致する。
    # codex plan r1 M4: `iloc[:13]` では index 13 を見ておらず、warmup が
    # 1 本早く明ける変異を検出できなかった。**境界 2 点を pin する**。
    assert bool(rsi.iloc[:14].isna().all())
    assert not pd.isna(rsi.iloc[14])
    assert not pd.isna(rsi.iloc[-1])
    assert 0.0 <= float(rsi.iloc[-1]) <= 100.0


def test_short_frame_returns_all_nan_series_not_empty_dict():
    df = _df([100.0] * 5)
    out = compute(df, {})
    assert set(out) == {"rsi"}
    assert bool(out["rsi"].isna().all())


def test_uptrend_yields_high_rsi():
    rsi = compute(_df([100 + i for i in range(20)]), {})["rsi"]
    assert float(rsi.iloc[-1]) > 70.0


def test_custom_period_param_keeps_the_same_output_key():
    out = compute(_df([100 + i * 0.1 for i in range(10)]), {"period": 5})
    assert set(out) == {"rsi"}
    assert not pd.isna(out["rsi"].iloc[-1])


def test_flat_series_yields_neutral_rsi():
    rsi = compute(_df([100.0] * 20), {})["rsi"]
    assert float(rsi.iloc[-1]) == 50.0
