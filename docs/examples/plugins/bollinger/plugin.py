"""ボリンジャーバンド indicator plugin。

plugin 契約 ([indicator-initial-set] 設計書 §3 / §4)。indicator kind は
`compute(df, params) -> dict` を実装する。df はハーネスが供給する完成バー
のみの DataFrame (DatetimeIndex は UTC・昇順、末尾最大 `config.yaml` の
`max_bars` 本)。純関数のみ — I/O・乱数・実時計へのアクセスは禁止。
使ってよいのは pandas / numpy / math のみ。

作者向け注意 (設計書 §4 の必須項目):

1. **warmup はこの関数自身の責務。** `max_bars` は「渡す DataFrame の末尾
   最大本数の上限宣言」であり「常に同じ本数が入っている保証」ではない。
   系列契約では「行が足りないので何も返さない」はできない (ハーネスは宣言
   `outputs` と完全一致するキー集合を毎回要求する)。足りない期間は **NaN**
   にする — 消費側 (strategy) は `pd.isna` を見て hold を返す規約。

2. **1d 足のバケット境界は UTC 00:00 (epoch 錨)** であり、FX の取引日境界
   (NY 17:00 ロールオーバー) ではない。本 plugin は `timeframe` を宣言しない
   ため、呼び出し側が渡す任意の足で使われ得る。

3. **`max_bars` は 400。** この指標は**有限の窓しか見ない** (rolling) ので
   先頭依存そのものは無い — 同じ最終行を出すのに 400 本は要らない。
   400 に揃えてあるのは、9 本で値を 1 つにするため (設計書 §3.2 / 裁定
   D4): 指標ごとに散らすと、依存する strategy が小さく宣言したときに
   「一部の指標だけ静かにずれる」という気づきにくい部分的劣化になる。
   ただし窓 (下の `params`) より短い履歴では値は NaN のままである。
   **この indicator に依存する strategy は、自分の `max_bars` を 400 以上
   に宣言すること** — strategy worker は依存に
   `df.tail(min(strategy.max_bars, 400))` を渡すので、小さく宣言すると
   渡る履歴が短くなり、窓を満たせない期間は NaN のままになる
   (rolling の逐次更新に由来する丸めの差もわずかに残る)。

4. **純関数であること。** `df` と `params` を書き換えない (必要なら新しい
   Series を作る)。モジュールレベルの状態を持たない。

入力に NaN は無いものとする (挙動は未規定。ただし例外は送出しない)。
"""
from __future__ import annotations

import math

import pandas as pd



def _int_param(params: dict, name: str, default: int) -> int:
    """**変換ではなく型の確認** (設計書 §4)。`int(params.get(...))` は
    `14.9` を `14` に、`"14"` を `14` に黙って読み替えてしまい、承認不要の
    params 上書き (U3) 経由で誰のレビューも通らず本番へ届く。`bool` は
    `int` の派生なので先に弾く。

    **この関数は 9 本の plugin に逐語で重複している。** plugin は 1 フォルダ
    3 ファイルで完結しなければならず (loader が 4 本目の `.py` を拒否、
    sandbox が相対 import を拒否)、共有モジュールを置く経路が無い。
    直すときは 9 本まとめて直すこと。
    """
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"params.{name} must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"params.{name} must be >= 1, got {value}")
    return value


def _float_param(params: dict, name: str, default: float) -> float:
    """有限かつ正の数であることを確認する (設計書 §4)。`bool` を先に弾く。"""
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"params.{name} must be a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(
            f"params.{name} must be a finite number > 0, got {value}")
    return value


def compute(df: pd.DataFrame, params: dict) -> dict:
    """ボリンジャーバンドを系列で返す (母標準偏差 `ddof=0`)。

    sigma == 0 の行は除算を伴わないので `upper == middle == lower` になる
    — 異常ではない。`num_std` 自体は有限かつ `> 0` を要求する
    (0 は 3 本が同一系列になる退化形、負値は upper/lower の反転)。
    """
    period = _int_param(params, "period", 20)
    num_std = _float_param(params, "num_std", 2.0)
    close = df["close"].astype(float)
    middle = close.rolling(window=period, min_periods=period).mean()
    sigma = close.rolling(window=period, min_periods=period).std(ddof=0)
    return {"upper": middle + num_std * sigma,
            "middle": middle,
            "lower": middle - num_std * sigma}
