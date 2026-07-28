import math
from datetime import datetime, timedelta, timezone

from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.indicators import (
    bars_to_df, compute_indicators, resample,
)

NOW = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)


def _bars(n=100):
    out = []
    for i in range(n):
        base = 148.0 + math.sin(i / 10) * 0.5
        out.append(Bar("USDJPY", "1h", NOW + timedelta(hours=i),
                       base, base + 0.1, base - 0.1, base + 0.02, 100))
    return out


def test_bars_to_df_shape():
    df = bars_to_df(_bars(10))
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 10


def test_resample_4h():
    df = bars_to_df(_bars(8))
    r = resample(df, "4h")
    assert len(r) == 2
    assert r.iloc[0]["high"] == df.iloc[0:4]["high"].max()
    assert r.iloc[0]["open"] == df.iloc[0]["open"]


def test_compute_indicators_keys():
    ind = compute_indicators(bars_to_df(_bars(100)))
    for k in ("sma_20", "sma_50", "ema_12", "rsi_14", "atr_14", "macd",
              "macd_signal", "bb_upper", "bb_lower"):
        assert k in ind and isinstance(ind[k], float)
    assert 0 <= ind["rsi_14"] <= 100
    assert ind["bb_lower"] < ind["sma_20"] < ind["bb_upper"]


def test_insufficient_data_returns_none():
    ind = compute_indicators(bars_to_df(_bars(10)))
    assert ind["sma_50"] is None
    assert ind["sma_20"] is None or isinstance(ind["sma_20"], float)
