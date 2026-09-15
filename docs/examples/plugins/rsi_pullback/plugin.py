"""配備済 indicator に依存する strategy の例 (RSI プルバック)。

plugin 契約: strategy kind は `evaluate(df, indicators, signals, params) -> dict`
を実装する。`indicators` は **`config.yaml` の `indicators:` で宣言した依存**
だけが入る `{alias: {output_key: float | pd.Series}}` — ここでは
`indicators["rsi"]["rsi"]` が df と同じ index の `pd.Series` (warmup 行は NaN)。
`signals` は現行ハーネスでは常に `None`。

**依存の版 (`pin`) はハーネスが書く** — 作者は書かない。探索中は pin 無しの
ままでよいが、**提出 (submit / bless) の前に必ずロックすること**:
改善 worker は `lock_staging_deps(name="rsi_pullback")`、人間は
`afx plugin lock --from _human rsi_pullback`。ロック後に self-test と
backtest を再実行する (テストした artifact == 提出する artifact)。

**`max_bars` は「依存の warmup + 自分の lookback」を覆うように宣言すること。**
本例は RSI(14) の Wilder 平滑に十分な 200 本を宣言している。系列が全 NaN の
ままなら warmup 不足の兆候。

純関数のみで書くこと (I/O・乱数・実時計へのアクセス禁止)。使ってよいのは
pandas/numpy/math のみ。pandas/numpy の**グローバル設定**
(`pd.set_option` / `np.seterr` / `pd.options.* = ...`) は同一プロセスを共有
する他 plugin に影響するため禁止 (ハーネスが AST で拒否する)。
"""
from __future__ import annotations

import pandas as pd


def evaluate(df: pd.DataFrame, indicators: dict, signals: list,
             params: dict) -> dict:
    oversold = float(params.get("oversold", 30))
    overbought = float(params.get("overbought", 70))
    stop_loss_pips = float(params.get("stop_loss_pips", 30))
    take_profit_pips = float(params.get("take_profit_pips", 60))
    pip_size = float(params.get("pip_size", 0.01))

    rsi = indicators["rsi"]["rsi"]
    if len(rsi) < 2:
        return {"action": "hold", "rationale": "insufficient bars"}
    prev, curr = rsi.iloc[-2], rsi.iloc[-1]
    if pd.isna(prev) or pd.isna(curr):
        return {"action": "hold", "rationale": "indicator warmup"}

    close = float(df["close"].iloc[-1])
    stop = stop_loss_pips * pip_size
    target = take_profit_pips * pip_size
    if prev <= oversold and curr > oversold:
        return {"action": "open", "direction": "long", "entry_type": "market",
                "stop_loss": close - stop, "take_profit": close + target,
                "rationale": "RSI recovered above the oversold threshold"}
    if prev >= overbought and curr < overbought:
        return {"action": "open", "direction": "short", "entry_type": "market",
                "stop_loss": close + stop, "take_profit": close - target,
                "rationale": "RSI fell below the overbought threshold"}
    return {"action": "hold", "rationale": "no RSI threshold crossing"}
