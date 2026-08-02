"""RSI (Relative Strength Index) の indicator plugin サンプル。

plugin 契約 (プラン 7 §6): indicator kind は
`compute(df, params) -> dict[str, float]` を実装する。df はハーネスが
供給する完成バーのみの DataFrame (DatetimeIndex は UTC・昇順、末尾最大
`config.yaml` の `max_bars` 本)。純関数のみ — I/O・乱数・実時計へのアク
セスは禁止 (サンドボックス上限)。使ってよいのは pandas/numpy/math のみ。

作者向け注意 (plugin を書く LLM/人間の両方が必ず守ること):

1. **warmup はこの関数自身の責務。** `max_bars` は「渡す DataFrame の
   末尾最大本数の上限宣言」であり「常に同じ本数が入っている保証」ではな
   い (source・欠損・履歴開始位置によって実行時の行数は変わり得る)。
   呼び出されるたびに必ず `len(df)` を検査し、計算に必要な本数
   (RSI なら period+1 本) に満たない場合は計算せず**空の結果 `{}` を
   返す** (strategy plugin なら hold を返すのが対応する規約)。

2. **1d 足のバケット境界は UTC 00:00 (epoch 錨) であり、FX の取引日境界
   (NY 17:00 ロールオーバー) ではない。** 分析・承認バックテスト・本番
   producer は全て同一の epoch 錨でリサンプルするため plugin から見た
   一貫性は保たれるが、「NY 基準の日足」を期待したロジック (日次ロール
   オーバー跨ぎの判定等) はここでは書けない — NY 錨の日足は本プランでは
   未対応 (`datafeed.bars.resample` が epoch 以外の錨を NotImplementedError
   で明示拒否している既存防御と整合)。本 plugin は timeframe を宣言しな
   いため (indicator は timeframe 省略可)、呼び出し側が渡す任意の足で
   使われ得ることに注意。
"""
from __future__ import annotations

import pandas as pd


def compute(df: pd.DataFrame, params: dict) -> dict:
    period = int(params.get("period", 14))

    # 注意1 (warmup): 差分計算に period+1 本必要。不足時は空を返す。
    if len(df) < period + 1:
        return {}

    delta = df["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.rolling(window=period).mean().iloc[-1]
    avg_loss = loss.rolling(window=period).mean().iloc[-1]

    if pd.isna(avg_gain) or pd.isna(avg_loss):
        return {}

    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

    return {f"rsi_{period}": float(rsi)}
