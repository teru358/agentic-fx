"""indicator の戻り値検証 — 唯一の実装 ([indicator-consumption-wiring] §2.5)。

**親プロセス (`plugin/sandbox.py`) と sandbox worker (`plugin/worker.py`)
の両方から import される** — ①置き場の裁定 (ユーザー裁定 2026-09-14):
`core/` は `core/contracts.py` の前例どおり上位層に依存しない性格の
モジュールを置く場所であり、worker エントリは既に
`agentic_fx.core.landlock` を import している。**このモジュールは
`agentic_fx.plugin.*` を import してはならない** — `plugin/worker.py`
(子プロセス、RLIMIT_AS 下) が import するため、`subprocess` や DB 接続を
巻き込む `plugin.sandbox` / `plugin.switch` 等への依存を作ると子プロセスに
不要な依存を持ち込む。`pandas`/`numpy` は関数内 lazy import にする
(import 自体は worker がどのみち行うが、親プロセス側の単体テストで
余計な import を強制しない)。

検証内容 (設計書 §2.5):
- dict[str, value] であること (キーは str)
- value は (a) float/int (bool・±Inf は拒否、NaN は許可) または
  (b) 系列 — `pd.Series` は `index.equals(df_index)` 必須、list/ndarray は
  `len == len(df_index)`、要素は数値 (bool・±Inf 拒否、NaN 許可)
- `outputs` 宣言済みは毎回**宣言キー集合と完全一致** (warmup は NaN で埋める)
- `outputs is None` は standalone (`get_indicators`) 経由でのみ到達し、
  任意のキー集合 (空 dict 含む) を許す (既存 `rsi_wilder` 互換)
"""
from __future__ import annotations

import math
from typing import Any


class IndicatorResultError(ValueError):
    """indicator の戻り値が契約に反する。呼び出し元 (worker / sandbox) が
    それぞれの層の例外 (`SandboxError` 等) へ写像する。"""


def _check_number(value: Any, where: str) -> float:
    # opus r1 M3 是正: `float(value)` を先に呼ぶと `float("1.5")` が通り、
    # **数値文字列を受理**してしまう (設計書 §2.5 は「要素は数値」)。
    # 型を先に見て、数値型以外は無条件で拒否する。`numbers.Real` を使うと
    # `Decimal`/`Fraction` が漏れるので、実際に扱う型 (int / float /
    # numpy スカラー) だけを allowlist する。
    import numpy as np

    if isinstance(value, bool) or isinstance(value, np.bool_):
        raise IndicatorResultError(f"{where} must be a number, got bool")
    if value is None:
        raise IndicatorResultError(f"{where} must be a number, got None")
    if not isinstance(value, (int, float, np.integer, np.floating)):
        raise IndicatorResultError(
            f"{where} must be a number, got {type(value).__name__}")
    try:
        fvalue = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise IndicatorResultError(
            f"{where} must be a number, got {type(value).__name__}") from exc
    if math.isinf(fvalue):
        raise IndicatorResultError(f"{where} must not be +/-Inf")
    return fvalue


def validate_indicator_result(result: Any, *, df_index, outputs):
    import numpy as np
    import pandas as pd

    if not isinstance(result, dict):
        raise IndicatorResultError(
            f"indicator must return a dict, got {type(result).__name__}")
    for key in result:
        if not isinstance(key, str):
            raise IndicatorResultError(
                f"indicator result keys must be str, got {key!r}")
    if outputs is not None and set(result) != set(outputs):
        raise IndicatorResultError(
            f"indicator result keys {sorted(result)} do not match declared "
            f"outputs {sorted(outputs)}")

    expected_len = None if df_index is None else len(df_index)
    out: dict[str, Any] = {}
    for key, value in result.items():
        where = f"indicator result[{key!r}]"
        if isinstance(value, pd.Series):
            if df_index is not None and not value.index.equals(df_index):
                raise IndicatorResultError(f"{where} series index must equal df index")
            values = [None if pd.isna(v) else _check_number(v, where)
                      for v in value.tolist()]
            out[key] = pd.Series(values, index=value.index, dtype="float64")
            continue
        if isinstance(value, (list, tuple, np.ndarray)):
            seq = list(value)
            if expected_len is not None and len(seq) != expected_len:
                raise IndicatorResultError(
                    f"{where} series length {len(seq)} != df length {expected_len}")
            # opus r1 M4 是正: 元案は `pd.isna(v)` を要素に直接かけていたが、
            # `v` が list / dict / ndarray のとき `pd.isna` は**配列**を返し、
            # `if` が `ValueError: truth value of an array is ambiguous` を
            # 素で送出する (= `IndicatorResultError` にならず親の
            # `except IndicatorResultError` を素通りする)。NaN 判定を
            # スカラー数値型に限定し、それ以外は `_check_number` に回して
            # 必ず `IndicatorResultError` へ写像する。
            def _nan_or_number(v: Any) -> float | None:
                if v is None:
                    return None
                if isinstance(v, (float, np.floating)) and math.isnan(float(v)):
                    return None
                if v is pd.NaT or (v is pd.NA):
                    return None
                return _check_number(v, where)

            values = [_nan_or_number(v) for v in seq]
            out[key] = (pd.Series(values, index=df_index, dtype="float64")
                        if df_index is not None else values)
            continue
        out[key] = _check_number(value, where)
    return out
