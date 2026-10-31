"""ohlcv (参照系列の素の OHLCV を返す) indicator plugin。

参照系列 (別 symbol や別 timeframe) の値そのものを strategy で読むための
素通しの指標。`outputs` は `open high low close volume` をこの順で、各出力は
入力と同じ index の数値 Series で、対応する入力列と完全に一致する。

作者向け注意:

1. **seed が要らない。** 何も平滑化しないので `max_bars` は渡される窓の
   上限宣言でしかなく、値は warmup に依存しない。
2. **入力を書き換えない。** 出力は入力列の複製であり、NaN を新たに作らない。
   接頭辞 (先頭 t 行) の出力は全行の出力の先頭 t 行と一致する。
3. **params は持たない。** 未知の `params` キーは `ValueError` にする
   (綴り誤りが黙って無視されないように)。
"""
from __future__ import annotations

import pandas as pd

_COLUMNS = ("open", "high", "low", "close", "volume")


def _reject_unknown_params(params: dict) -> None:
    if not isinstance(params, dict):
        raise ValueError(f"params. must be a mapping, got {type(params).__name__}")
    unknown = sorted((key for key in params),
                     key=lambda key: (0, key) if isinstance(key, str) else (1, repr(key)))
    if unknown:
        first = unknown[0]
        label = first if isinstance(first, str) else repr(first)
        raise ValueError(f"params.{label} is not a known parameter, known: (none)")


def compute(df: pd.DataFrame, params: dict) -> dict:
    """入力の OHLCV 5 列を、入力と同じ順・同じ値の Series で返す。"""
    _reject_unknown_params(params)
    return {column: df[column].copy() for column in _COLUMNS}
