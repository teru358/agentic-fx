"""rsi_pullback (依存あり strategy の例) の最小テスト。

`indicators` はハーネスが渡す `{alias: {output_key: float | pd.Series}}`。
このテストではそれを手で組み立てる (plugin は他 plugin を import できない)。
"""
from __future__ import annotations

import pandas as pd

from plugin import evaluate

PARAMS = {"oversold": 30, "overbought": 70, "stop_loss_pips": 30,
          "take_profit_pips": 60, "pip_size": 0.01}


def _df(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "volume": [1.0] * len(closes)}, index=idx)


def _ind(df, values):
    return {"rsi": {"rsi": pd.Series(values, index=df.index, dtype="float64")}}


def test_long_on_upward_cross_of_oversold():
    df = _df([150.0, 150.0])
    out = evaluate(df, _ind(df, [25.0, 35.0]), None, PARAMS)
    assert out["action"] == "open"
    assert out["direction"] == "long"
    assert out["entry_type"] == "market"
    assert out["stop_loss"] == 150.0 - 0.30
    assert out["take_profit"] == 150.0 + 0.60


def test_short_on_downward_cross_of_overbought():
    df = _df([150.0, 150.0])
    out = evaluate(df, _ind(df, [75.0, 65.0]), None, PARAMS)
    assert out["action"] == "open"
    assert out["direction"] == "short"
    assert out["stop_loss"] == 150.0 + 0.30
    assert out["take_profit"] == 150.0 - 0.60


def test_hold_when_no_cross():
    df = _df([150.0, 150.0])
    assert evaluate(df, _ind(df, [40.0, 45.0]), None, PARAMS)["action"] == "hold"


def test_hold_when_either_value_is_nan():
    df = _df([150.0, 150.0])
    assert evaluate(df, _ind(df, [float("nan"), 35.0]), None,
                    PARAMS)["action"] == "hold"
    assert evaluate(df, _ind(df, [25.0, float("nan")]), None,
                    PARAMS)["action"] == "hold"


def test_hold_when_fewer_than_two_bars():
    df = _df([150.0])
    assert evaluate(df, _ind(df, [35.0]), None, PARAMS)["action"] == "hold"


def test_boundary_equal_to_threshold_is_not_a_cross():
    """`prev <= oversold and curr > oversold` — curr == oversold は hold。"""
    df = _df([150.0, 150.0])
    assert evaluate(df, _ind(df, [25.0, 30.0]), None, PARAMS)["action"] == "hold"


def test_prev_exactly_at_oversold_is_a_long_cross():
    """契約は `prev <= oversold and curr > oversold` — **prev がちょうど
    閾値**でも上抜けとして扱う。`prev < oversold` への退行はここでだけ
    落ちる (curr 側の境界しか見ていない上のテストは通ってしまう)。"""
    df = _df([150.0, 150.0])
    out = evaluate(df, _ind(df, [30.0, 35.0]), None, PARAMS)
    assert out["action"] == "open"
    assert out["direction"] == "long"


def test_prev_exactly_at_overbought_is_a_short_cross():
    """short 側の対称形 (`prev >= overbought and curr < overbought`)。"""
    df = _df([150.0, 150.0])
    out = evaluate(df, _ind(df, [70.0, 65.0]), None, PARAMS)
    assert out["action"] == "open"
    assert out["direction"] == "short"
