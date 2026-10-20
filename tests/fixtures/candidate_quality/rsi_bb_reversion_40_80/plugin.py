from __future__ import annotations
import pandas as pd

def evaluate(df: pd.DataFrame, indicators: dict, signals: list, params: dict) -> dict:
    sl=float(params.get("stop_loss_pips",40))
    tp=float(params.get("take_profit_pips",80))
    pip=float(params.get("pip_size",0.01))
    if len(df)<2:
        return {"action":"hold","rationale":"insufficient bars"}
    try:
        upper=indicators["bb"]["upper"]; lower=indicators["bb"]["lower"]
        rsi=indicators["rsi"]["rsi"]
    except (KeyError,TypeError):
        return {"action":"hold","rationale":"missing indicator output"}
    if min(len(upper),len(lower),len(rsi))<2:
        return {"action":"hold","rationale":"indicator warmup"}
    vals=[upper.iloc[-2],upper.iloc[-1],lower.iloc[-2],lower.iloc[-1],
          rsi.iloc[-2],rsi.iloc[-1],df["close"].iloc[-2],df["close"].iloc[-1]]
    if any(pd.isna(x) for x in vals):
        return {"action":"hold","rationale":"indicator warmup"}
    prev=float(df["close"].iloc[-2]); close=float(df["close"].iloc[-1])
    stop=sl*pip; target=tp*pip
    if (prev<=float(lower.iloc[-2]) and close>float(lower.iloc[-1])
            and float(rsi.iloc[-2])<=50.0 and float(rsi.iloc[-1])>50.0):
        return {"action":"open","direction":"long","entry_type":"market",
                "stop_loss":close-stop,"take_profit":close+target,
                "rationale":"same-bar lower-band reclaim and RSI(6) recovery"}
    if (prev>=float(upper.iloc[-2]) and close<float(upper.iloc[-1])
            and float(rsi.iloc[-2])>=50.0 and float(rsi.iloc[-1])<50.0):
        return {"action":"open","direction":"short","entry_type":"market",
                "stop_loss":close+stop,"take_profit":close-target,
                "rationale":"same-bar upper-band rejection and RSI(6) rollover"}
    return {"action":"hold","rationale":"no confirmed RSI/Bollinger reversal"}
