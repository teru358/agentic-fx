"""rsi_indicator の最小テスト。

docs/examples/plugins/ は discover の対象外 (承認・本番配線とは独立の
サンプル)。承認フロー (プラン 7 Task 6) はこのファイルを実行して plugin
提出者が「動くテスト」を書いていることを検証する。
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


def test_compute_returns_rsi_key_when_enough_bars():
    closes = [100 + i * 0.1 for i in range(30)]
    out = compute(_df(closes), {})
    assert "rsi_14" in out
    assert 0.0 <= out["rsi_14"] <= 100.0


def test_compute_warmup_insufficient_returns_empty():
    """len(df) < period+1 は空を返す (warmup は plugin 自身の責務)。"""
    out = compute(_df([100.0] * 5), {})
    assert out == {}


def test_compute_uptrend_yields_high_rsi():
    closes = [100 + i for i in range(20)]  # 単調増加
    out = compute(_df(closes), {})
    assert out["rsi_14"] > 70.0


def test_compute_respects_custom_period_param():
    closes = [100 + i * 0.1 for i in range(10)]
    out = compute(_df(closes), {"period": 5})
    assert "rsi_5" in out
