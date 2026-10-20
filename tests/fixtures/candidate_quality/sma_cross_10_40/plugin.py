from __future__ import annotations

import pandas as pd


def evaluate(df: pd.DataFrame, indicators: dict, signals: list,
             params: dict) -> dict:
    stop_loss_pips = float(params.get("stop_loss_pips", 30))
    take_profit_pips = float(params.get("take_profit_pips", 60))
    pip_size = float(params.get("pip_size", 0.01))

    fast = indicators["fast_sma"]["value"]
    slow = indicators["slow_sma"]["value"]
    if len(df) < 2 or len(fast) < 2 or len(slow) < 2:
        return {"action": "hold", "rationale": "insufficient bars"}
    prev_fast, curr_fast = fast.iloc[-2], fast.iloc[-1]
    prev_slow, curr_slow = slow.iloc[-2], slow.iloc[-1]
    if any(pd.isna(v) for v in
           (prev_fast, curr_fast, prev_slow, curr_slow)):
        return {"action": "hold", "rationale": "indicator warmup"}

    close = float(df["close"].iloc[-1])
    stop_offset = stop_loss_pips * pip_size
    target_offset = take_profit_pips * pip_size
    if prev_fast <= prev_slow and curr_fast > curr_slow:
        return {
            "action": "open", "direction": "long", "entry_type": "market",
            "stop_loss": close - stop_offset,
            "take_profit": close + target_offset,
            "rationale": "SMA(10) crossed above SMA(40)",
        }
    if prev_fast >= prev_slow and curr_fast < curr_slow:
        return {
            "action": "open", "direction": "short", "entry_type": "market",
            "stop_loss": close + stop_offset,
            "take_profit": close - target_offset,
            "rationale": "SMA(10) crossed below SMA(40)",
        }
    return {"action": "hold", "rationale": "no crossover"}
