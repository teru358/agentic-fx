"""足の変換 (Bar ⇄ DataFrame、リサンプル)。

データ層に置く理由: PriceProvider がネイティブに無い足を導出するのに
必要であり、indicators (Task 4) もこれを import する。指標計算の付属物
ではなく、データ層の基本操作である。
"""
from __future__ import annotations

import pandas as pd
from pandas.tseries.frequencies import to_offset
from pandas.tseries.offsets import Tick

from agentic_fx.core.contracts import Bar

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum"}

_COLUMNS = ("open", "high", "low", "close", "volume")

# interval 文字列 → pandas の freq alias。**同じ文字列ではない**。
# pandas 3 では "1m"/"5m"/... は分ではなく月末 ('ME') と解釈されて
# ValueError になり、"1d" は非推奨警告が出る。interval をそのまま
# resample の rule に渡す実装は分足の導出 (yfinance に 30m が無いため
# 15m→30m を作る経路) で必ず落ちるので、明示的に写像する。
_PANDAS_RULE = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
                "1h": "1h", "4h": "4h", "1d": "1D"}

# 足の境界の錨。既定は UTC epoch (夏時間の影響を受けず、取得ウィンドウにも依存しない)。
#
# 別案として NY クローズ (America/New_York 17:00) 基準がある。FX 業界慣行の日足・4h と
# 一致し、実測ではブローカーのネイティブ格子とも概ね一致する (完全一致ではない —
# EU と US の夏時間切替日が春に約 2 週間・秋に約 1 週間ずれるため)。
# 錨の是非はバックテストの spec で判断する (ユーザー裁定 2026-07-28)。
#
# **導出足はキャッシュしない設計なので、この値を変えてもキャッシュ破棄は不要**。
# 切り替えはここ 1 箇所の変更で済む。
BAR_ANCHOR = "epoch"


def pandas_rule(interval: str) -> str:
    """interval 文字列を pandas の freq alias へ写す。未知の足は ValueError。"""
    try:
        return _PANDAS_RULE[interval]
    except KeyError:
        raise ValueError(f"unknown interval: {interval}") from None


def bars_to_df(bars: list[Bar]) -> pd.DataFrame:
    """Bar 列 → DataFrame (index=ts, columns=OHLCV)。

    naive な ts は ValueError で拒否する。pandas の `tz="UTC"` は naive を
    **無言で UTC として localize** してしまい、「naive なら UTC とみなす」
    というプロジェクト禁止事項をそのまま実行することになるため
    (aware な入力に対しては変換のみで、tz の正規化点は sources.py 側)。
    """
    naive = [b for b in bars if b.ts.tzinfo is None]
    if naive:
        raise ValueError(
            f"naive datetime in bars (first={naive[0].ts}); "
            "timezone-aware timestamps are required")
    return pd.DataFrame(
        {"open": [b.open for b in bars], "high": [b.high for b in bars],
         "low": [b.low for b in bars], "close": [b.close for b in bars],
         "volume": [b.volume for b in bars]},
        index=pd.DatetimeIndex([b.ts for b in bars], tz="UTC"),
        columns=list(_COLUMNS))


def df_to_bars(df: pd.DataFrame, symbol: str, interval: str) -> list[Bar]:
    return [Bar(symbol, interval, ts.to_pydatetime(),
                float(r["open"]), float(r["high"]), float(r["low"]),
                float(r["close"]), float(r["volume"]))
            for ts, r in df.iterrows()]


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """OHLCV リサンプル。**バケット境界の錨は `BAR_ANCHOR`** (既定 UTC epoch)。

    origin を既定 (データ先頭) にすると、取得開始時刻によって 4h の
    切り方が変わり、実運用とバックテストで違う足を見ることになる。

    origin は Tick 系 (分・時) の freq にしか効かない (pandas 3 は非 Tick に
    渡すと RuntimeWarning)。日足以上は index が UTC である限り UTC 深夜
    始まりに揃うので、**錨が epoch のときに限り**同じ境界になる — そのため
    渡さない。epoch 以外の錨に変える場合は日足側の明示実装が要るので、
    無言で違う錨の足を作らないよう NotImplementedError で止める。

    データが 1 本も無いバケットは落とす (OHLC が全て NaN になる)。部分的な
    バケット (形成中の足) は残る。
    """
    if isinstance(to_offset(rule), Tick):
        kwargs = {"origin": BAR_ANCHOR}
    else:
        if BAR_ANCHOR != "epoch":
            raise NotImplementedError(
                f"BAR_ANCHOR={BAR_ANCHOR!r} is not applied to non-Tick rule "
                f"{rule!r} (pandas ignores origin); implement it explicitly")
        kwargs = {}
    return df.resample(rule, **kwargs).agg(_AGG).dropna()
