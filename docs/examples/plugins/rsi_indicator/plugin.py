"""RSI (Relative Strength Index) の indicator plugin サンプル。

plugin 契約 (プラン 7 §6、[indicator-consumption-wiring] 2026-09-14 改訂):
indicator kind は `compute(df, params) -> dict` を実装する。df はハーネスが
供給する完成バーのみの DataFrame (DatetimeIndex は UTC・昇順、末尾最大
`config.yaml` の `max_bars` 本)。純関数のみ — I/O・乱数・実時計へのアク
セスは禁止 (サンドボックス上限)。使ってよいのは pandas/numpy/math のみ。

戻り値は `{outputs で宣言したキー: float | pd.Series}`。系列は df と同じ
index で返すこと (ハーネスが index 一致を検証する)。

作者向け注意 (plugin を書く LLM/人間の両方が必ず守ること):

1. **warmup はこの関数自身の責務。** `max_bars` は「渡す DataFrame の
   末尾最大本数の上限宣言」であり「常に同じ本数が入っている保証」ではな
   い (source・欠損・履歴開始位置によって実行時の行数は変わり得る)。
   系列契約では「行が足りないので何も返さない」はできない (ハーネスは
   宣言 `outputs` と完全一致するキー集合を毎回要求する)。足りない期間は
   **NaN** にする — 消費側 (strategy) は `pd.isna` を見て hold を返す規約。

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
    """Wilder 平滑の RSI を **系列** (df と同じ index) で返す。

    注意1 (warmup): 系列契約では「行が足りないので何も返さない」はできない
    (ハーネスは宣言 `outputs` と完全一致するキー集合を毎回要求する)。
    足りない期間は **NaN** にする — 消費側 (strategy) は `pd.isna` を見て
    hold を返す規約。`max_bars` は「渡す DataFrame の末尾最大本数の上限
    宣言」であり、常に同じ本数が入っている保証ではない。

    注意2: 本 plugin は timeframe を宣言しないため (indicator は timeframe
    省略可)、呼び出し側が渡す任意の足で使われ得る。
    """
    period = int(params.get("period", 14))

    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False,
                        min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False,
                        min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0.0, 100.0)
    rsi = rsi.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    # avg_gain/avg_loss が未確定 (warmup) の行は NaN のまま残す。
    rsi = rsi.where(~(avg_gain.isna() | avg_loss.isna()))
    return {"rsi": rsi}
