"""SMA クロスオーバーの strategy plugin サンプル (kind: strategy)。

plugin 契約 (プラン 7 §6): strategy kind は
`evaluate(df, indicators, signals, params) -> dict` を実装する。戻り値は
`contracts.StrategyDecision` 互換の dict — `action="open"` の場合は
`direction`/`entry_type`/`stop_loss` が必須 (exit は levels のみ:
stop_loss/take_profit。**"exit" という action 値はこのプランの語彙に無く
使えない** — strategy_adapter (Task 5) が SandboxError にする)。純関数の
みで書くこと (I/O・乱数・実時計へのアクセス禁止)。使ってよいのは
pandas/numpy/math のみ。

df は本 plugin の宣言 timeframe (config.yaml: `timeframe: 1h`) にリサン
プル済みの完成バーのみを含む (先読みなし。DatetimeIndex は UTC・昇順、
末尾最大 `max_bars` (200) 本)。

作者向け注意 (plugin を書く LLM/人間の両方が必ず守ること):

1. **warmup はこの関数自身の責務。** `max_bars` は「渡す df の末尾最大
   本数の上限宣言」であり保証本数ではない (source・欠損・履歴開始位置に
   よって実行時の行数は変わり得る)。クロス判定に必要な本数
   (`slow_period + 1`) に満たない場合は必ず **hold を返す** — ハーネスは
   plugin の代わりに判断を補わない (帰属規則)。

2. **1d 足のバケット境界は UTC 00:00 (epoch 錨) であり、FX の取引日境界
   (NY 17:00 ロールオーバー) ではない。** 本 plugin は `timeframe: 1h` を
   宣言しており直接の影響は無いが、1d を宣言する plugin を書く場合は
   「NY 基準の日足」を前提にしたロジック (日次ロールオーバー跨ぎの判定
   等) は書けない点に注意する。分析・承認バックテスト・本番 producer は
   全て同一の epoch 錨でリサンプルするため plugin から見た一貫性は保た
   れるが、NY 錨の日足自体が本プランでは未対応
   (`datafeed.bars.resample` が epoch 以外の錨を NotImplementedError で
   明示拒否している既存防御と整合)。
"""
from __future__ import annotations

import pandas as pd


def evaluate(df: pd.DataFrame, indicators: dict, signals: list,
            params: dict) -> dict:
    fast_period = int(params.get("fast_period", 5))
    slow_period = int(params.get("slow_period", 20))
    stop_loss_pips = float(params.get("stop_loss_pips", 20))
    pip_size = float(params.get("pip_size", 0.01))  # USDJPY 既定 (1pip=0.01)

    # 注意1 (warmup): クロス判定には直近2本の SMA (slow_period+1 本) が要る。
    if len(df) < slow_period + 1:
        return {"action": "hold", "rationale": "insufficient bars for warmup"}

    fast = df["close"].rolling(window=fast_period).mean()
    slow = df["close"].rolling(window=slow_period).mean()

    prev_fast, prev_slow = fast.iloc[-2], slow.iloc[-2]
    curr_fast, curr_slow = fast.iloc[-1], slow.iloc[-1]

    if (pd.isna(prev_fast) or pd.isna(prev_slow)
            or pd.isna(curr_fast) or pd.isna(curr_slow)):
        return {"action": "hold", "rationale": "insufficient bars for warmup"}

    prev_diff = prev_fast - prev_slow
    curr_diff = curr_fast - curr_slow
    last_close = float(df["close"].iloc[-1])
    stop_offset = stop_loss_pips * pip_size

    if prev_diff <= 0 and curr_diff > 0:
        return {
            "action": "open", "direction": "long", "entry_type": "market",
            "stop_loss": last_close - stop_offset,
            "rationale": (f"fast SMA({fast_period}) crossed above "
                         f"slow SMA({slow_period})"),
        }
    if prev_diff >= 0 and curr_diff < 0:
        return {
            "action": "open", "direction": "short", "entry_type": "market",
            "stop_loss": last_close + stop_offset,
            "rationale": (f"fast SMA({fast_period}) crossed below "
                         f"slow SMA({slow_period})"),
        }

    return {"action": "hold", "rationale": "no crossover"}
