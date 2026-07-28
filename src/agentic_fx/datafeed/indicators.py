"""組み込みテクニカル指標。判断はせず生の値のみ返す (前身の technical_scorer は移植しない)。

MTF (マルチタイムフレーム) はこのモジュールに専用関数を持たない。設計書 §2 の
`get_indicators(pair, timeframe)` が示すとおり、上位足を見たい呼び出し側は
`timeframe` を変えて `PriceProvider.get_bars` を呼び直す設計であり (特定の足に
固定した自動追補はしない)、足の変換そのものは `datafeed/bars.py` の責務。
ここで独自に resample すると `get_bars` 側の格子と別物ができてしまうため
(Task 3 で実測済みの不具合)、bars.py の実装をそのまま import して使う。
"""
from __future__ import annotations

import pandas as pd

# 足の変換は datafeed/bars.py (Task 3) に置く。ここで再定義しない。
# resample の rule には interval 文字列をそのまま渡さないこと —
# pandas 3 では "1m"/"5m"/"15m"/"30m" が月末 ('ME') 扱いで ValueError になる
# ("4h" は文字列が pandas の freq alias とたまたま一致するだけ)。
# 呼び出し側は必ず pandas_rule(interval) を経由すること。テストは
# tests/datafeed/test_bars.py::test_pandas_rule_covers_every_supported_interval
# で全 interval を検証済み (Task 3)。
from agentic_fx.datafeed.bars import bars_to_df, pandas_rule, resample  # noqa: F401


def _last(series: pd.Series, min_len: int) -> float | None:
    """直近値を float で返す。データ不足 (系列長 < min_len) や NaN なら None。"""
    if len(series.dropna()) < 1 or len(series) < min_len:
        return None
    v = series.iloc[-1]
    return None if pd.isna(v) else float(v)


def compute_indicators(df: pd.DataFrame) -> dict:
    """OHLCV DataFrame から組み込み指標の直近値を計算する。

    データ不足の指標は None を返す (fail closed で LLM に誤った確信を渡さない
    ためではなく、単に「まだ計算できない」ことを明示するため — 判断そのものは
    LLM に委ねる、この関数は生の値だけを返す)。
    """
    close, high, low = df["close"], df["high"], df["low"]
    out: dict[str, float | None] = {}
    out["sma_20"] = _last(close.rolling(20).mean(), 20)
    out["sma_50"] = _last(close.rolling(50).mean(), 50)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    out["ema_12"] = _last(ema12, 12)
    out["ema_26"] = _last(ema26, 26)
    macd = ema12 - ema26
    out["macd"] = _last(macd, 26)
    out["macd_signal"] = _last(macd.ewm(span=9, adjust=False).mean(), 26)

    delta = close.diff()
    avg_gain = delta.clip(lower=0).rolling(14).mean()
    avg_loss = (-delta.clip(upper=0)).rolling(14).mean()
    # rs = avg_gain / avg_loss。avg_loss == 0 かつ avg_gain > 0 (直近 14 本が
    # 連続上昇) のときは 0 除算で rs = inf になるが、そのまま
    # `100 - 100/(1+inf)` を計算すると 100/(inf) = 0 で自然に 100.0 (教科書の
    # RSI の極限どおり) に収束するため特別扱いは不要 (pandas は 0 除算で
    # 例外を出さず inf/NaN を返す、実測確認済み)。
    #
    # 例外は avg_gain == 0 かつ avg_loss == 0 (直近 14 本の値動きがゼロ、
    # 完全な横ばい) の 0/0 = NaN で、これだけは算術で自動解決しないため
    # 明示的に埋める。**このモジュールの不変条件: rsi_14 が None を返すのは
    # データ不足 (系列長 < 15) のときだけ**。avg_loss==0 を NaN のままにする
    # と「データ不足」と「買われすぎ/横ばい」が区別できなくなり (実測: 単調
    # 増加系列で rsi_14 が 100.0 ではなく None を返す欠陥があった)、LLM が
    # 最も価値の高い判断材料 (天井圏) をデータ不足と誤認しかねないため修正した。
    # 50.0 (中立) という値は業界標準ではなく本プロジェクトの規約 — RSI の
    # 定義域 [0, 100] の中央値であり、上げも下げも無い以上「買われすぎでも
    # 売られすぎでもない」とみなすのが妥当という判断
    rs = avg_gain / avg_loss
    rsi = 100 - 100 / (1 + rs)
    flat = avg_gain.eq(0) & avg_loss.eq(0)
    rsi = rsi.where(~flat, 50.0)
    out["rsi_14"] = _last(rsi, 15)

    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    out["atr_14"] = _last(tr.rolling(14).mean(), 15)

    std20 = close.rolling(20).std()
    sma20 = close.rolling(20).mean()
    out["bb_upper"] = _last(sma20 + 2 * std20, 20)
    out["bb_lower"] = _last(sma20 - 2 * std20, 20)
    return out
