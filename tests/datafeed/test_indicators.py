import math
from datetime import datetime, timedelta, timezone

import pytest

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
    for k in ("sma_20", "sma_50", "ema_12", "ema_26", "rsi_14", "atr_14",
              "macd", "macd_signal", "bb_upper", "bb_lower"):
        assert k in ind and isinstance(ind[k], float)
    assert 0 <= ind["rsi_14"] <= 100
    assert ind["bb_lower"] < ind["sma_20"] < ind["bb_upper"]


def test_insufficient_data_returns_none():
    # n=10 は sma_20 (min_len=20) にも sma_50 (min_len=50) にも足りない。
    # どちらも常に None になるはずで、「None か float か」という恒真な
    # assert では実装の型契約を確認しているだけで、この関数が実際に
    # 何を返すかは何も主張していない (レビュー指摘 2)。
    ind = compute_indicators(bars_to_df(_bars(10)))
    assert ind["sma_50"] is None
    assert ind["sma_20"] is None


# ---- RSI: avg_loss == 0 のときの挙動 (レビュー指摘 1) -----------------------
#
# 単調な系列を作るための専用ヘルパー。_bars() の正弦波は上げ下げが混在する
# ため avg_loss==0 のケースを再現できない。

def _mono_bars(n, step):
    """close が毎本 `step` ずつ変化する単調系列 (step=0 で完全な横ばい)。"""
    out = []
    for i in range(n):
        c = 148.0 + step * i
        out.append(Bar("USDJPY", "1h", NOW + timedelta(hours=i),
                       c - 0.02, c + 0.1, c - 0.1, c, 100))
    return out


def test_rsi_all_gains_is_100():
    """直近 14 本が連続上昇 (avg_loss==0, avg_gain>0) → RSI=100 (教科書の極限)。"""
    ind = compute_indicators(bars_to_df(_mono_bars(20, step=0.05)))
    assert ind["rsi_14"] == pytest.approx(100.0)


def test_rsi_all_losses_is_0():
    """直近 14 本が連続下落 (avg_gain==0, avg_loss>0) → RSI=0。"""
    ind = compute_indicators(bars_to_df(_mono_bars(20, step=-0.05)))
    assert ind["rsi_14"] == pytest.approx(0.0)


def test_rsi_flat_is_50():
    """完全な横ばい (avg_gain==0 かつ avg_loss==0, 0/0) → 中立の 50。

    50 は業界標準ではなく本プロジェクトの規約 (RSI 定義域 [0,100] の中央値)。
    """
    ind = compute_indicators(bars_to_df(_mono_bars(20, step=0.0)))
    assert ind["rsi_14"] == pytest.approx(50.0)


def test_rsi_none_only_for_insufficient_data():
    """rsi_14 が None を返すのはデータ不足 (系列長 < 15) のときだけ。

    avg_loss==0 の各ケース (連続上昇・横ばい) は上の 2 テストで float を
    返すことを確認済み。ここでは系列長が足りない場合との対比を明示する。
    """
    short = compute_indicators(bars_to_df(_mono_bars(10, step=0.05)))
    assert short["rsi_14"] is None
    full = compute_indicators(bars_to_df(_mono_bars(20, step=0.05)))
    assert full["rsi_14"] is not None


# ---- 数値の厳密な照合 (レビュー指摘 3) --------------------------------------
#
# 上記までのテストはキーの存在・型・順序関係しか見ておらず、EMA の adjust
# 切り替え・ATR の shift 抜け・BB の ddof 変更・MACD signal の期間変更と
# いった回帰を検出できない。以下は「実装を実行して得た値」ではなく、
# 定義を素朴に書き下した参照実装で独立に計算した期待値と突き合わせる。
#
# _bars() (正弦波、振幅 0.5、high-low 幅 0.2 固定) は値動きが high-low 幅
# より小さく、ATR の前日終値項が high-low 項を上回らないため shift 抜けの
# 回帰を検出できない。振幅を大きく・high-low 幅を狭くした専用の系列を使う。

def _volatile_bars(n=40):
    out = []
    for i in range(n):
        c = 148.0 + math.sin(i * 0.9) * 1.0
        out.append(Bar("USDJPY", "1h", NOW + timedelta(hours=i),
                       c - 0.02, c + 0.05, c - 0.05, c, 100))
    return out


def _reference_indicators(bars: list[Bar]) -> dict[str, float]:
    """compute_indicators の実装を一切呼ばない、素朴な参照計算。"""
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    n = len(closes)

    def ema_series(values, span):
        alpha = 2 / (span + 1)
        vals = [values[0]]
        for v in values[1:]:
            vals.append(alpha * v + (1 - alpha) * vals[-1])
        return vals

    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    macd_signal = ema_series(macd_line, 9)

    window20 = closes[-20:]
    mean20 = sum(window20) / 20
    var20 = sum((c - mean20) ** 2 for c in window20) / (len(window20) - 1)  # ddof=1
    std20 = var20 ** 0.5

    diffs = [closes[i] - closes[i - 1] for i in range(1, n)]
    last14 = diffs[-14:]
    gains = [max(d, 0.0) for d in last14]
    losses = [max(-d, 0.0) for d in last14]
    avg_gain = sum(gains) / 14
    avg_loss = sum(losses) / 14
    if avg_loss == 0:
        rsi = 100.0 if avg_gain > 0 else 50.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100 - 100 / (1 + rs)

    trs = []
    for i in range(n - 14, n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))  # 前日終値の項を含む
        trs.append(tr)
    atr = sum(trs) / 14

    return {
        "sma_20": mean20,
        "ema_12": ema12[-1],
        "ema_26": ema26[-1],
        "rsi_14": rsi,
        "atr_14": atr,
        "macd": macd_line[-1],
        "macd_signal": macd_signal[-1],
        "bb_upper": mean20 + 2 * std20,
        "bb_lower": mean20 - 2 * std20,
    }


def test_indicator_values_match_independent_reference():
    bars = _volatile_bars(40)
    ind = compute_indicators(bars_to_df(bars))
    ref = _reference_indicators(bars)
    for k, expected in ref.items():
        assert ind[k] == pytest.approx(expected, rel=1e-9, abs=1e-9), k
