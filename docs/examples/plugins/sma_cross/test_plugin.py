"""sma_cross の最小テスト。

docs/examples/plugins/ は discover の対象外 (承認・本番配線とは独立の
サンプル)。承認フロー (プラン 7 Task 6) はこのファイルを実行して plugin
提出者が「動くテスト」を書いていることを検証する。
"""
from __future__ import annotations

import pandas as pd

from plugin import evaluate


def _df(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "volume": [1.0] * len(closes)},
        index=idx)


def test_evaluate_holds_when_insufficient_bars_for_warmup():
    """len(df) < slow_period+1 (既定 21) は必ず hold — warmup は plugin の責務。"""
    out = evaluate(_df([100.0] * 5), {}, [], {})
    assert out["action"] == "hold"


def test_evaluate_holds_on_flat_prices_no_crossover():
    out = evaluate(_df([100.0] * 25), {}, [], {})
    assert out["action"] == "hold"


def test_evaluate_opens_long_on_upward_crossover():
    # 20 本の緩やかな下降 (120→101) の直後に急騰 (200) させ、
    # fast SMA(5) が slow SMA(20) を上抜ける状態を作る (手計算で確認済み:
    # prev_diff=-7.5 → curr_diff=+7.5)。
    closes = [120 - i for i in range(20)] + [200]
    out = evaluate(_df(closes), {}, [], {})
    assert out["action"] == "open"
    assert out["direction"] == "long"
    assert out["entry_type"] == "market"
    assert out["stop_loss"] < closes[-1]


def test_evaluate_opens_short_on_downward_crossover():
    # 上記の鏡像 (急騰の代わりに急落) で下抜けクロスを作る。
    closes = [101 + i for i in range(20)] + [50]
    out = evaluate(_df(closes), {}, [], {})
    assert out["action"] == "open"
    assert out["direction"] == "short"
    assert out["stop_loss"] > closes[-1]
