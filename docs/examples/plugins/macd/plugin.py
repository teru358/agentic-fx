"""MACD indicator plugin。

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

3. **`max_bars` は 400。** この指標は**再帰平滑** (前の行の値を使う) を
   含むので、履歴の先頭を切ると初期値 (seed) が変わり、その残差が最終行
   まで残る。再帰が線形なので残差は `|Δseed| × (1−α)^(本数−warmup)` で
   減衰し、400 本あればこの係数が 1e-13 の桁まで落ちる (設計書 §3.2 の
   上界式)。残差の大きさは入力の値域に比例するので、**「400 本なら必ず
   1e-6 以内」という普遍的な保証ではない**。
   **この indicator に依存する strategy は、自分の `max_bars` を 400 以上
   に宣言すること** — strategy worker は依存に
   `df.tail(min(strategy.max_bars, 400))` を渡すので、小さく宣言すると
   渡る履歴が短くなり値がわずかにずれる。

4. **純関数であること。** `df` と `params` を書き換えない (必要なら新しい
   Series を作る)。モジュールレベルの状態を持たない。

**未知の `params` キーは `ValueError` にする。** この plugin が読むキーは
下の `_DEFAULTS` がすべてである。strategy 側の params 上書きは承認不要
なので、`period` のつもりで綴りを誤ったまま黙って既定値で動くと、その
値を前提にした backtest 結果が出てしまう。

入力に NaN は無いものとする (挙動は未規定。ただし例外は送出しない)。
"""
from __future__ import annotations

import pandas as pd



#: **この plugin が読む params と既定値。** `config.yaml` の `params` はこの表と
#: 一致していなければならない (設計書 §4)。既定値をここに 1 箇所だけ持ち、
#: `compute` も受入テストもここを読む。
_DEFAULTS = {"fast": 12, "slow": 26, "signal_period": 9}
#: 既知キー集合。`_DEFAULTS` から導くので、両者がずれることはない。
_KNOWN_PARAMS = frozenset(_DEFAULTS)


def _reject_unknown_params(params: dict) -> None:
    """**既知でない params キーを `ValueError` にする** (設計書 §4)。

    「出力に効かないから無害」ではない — 綴り誤りが黙って既定値で動くと、
    strategy の作者もレビュー担当も「上書きが効いている」と読み違える。
    承認・backtest・bless のどの経路でも fail closed になるのが正しい。

    **この関数も `_DEFAULTS` / `_KNOWN_PARAMS` も 9 本の plugin に同形で
    重複している** (下の `_int_param` と同じ理由 — 共有モジュールを置く
    経路が無い)。違うのは `_DEFAULTS` の中身だけ。直すときは 9 本まとめて。
    """
    unknown = sorted(set(params) - _KNOWN_PARAMS)
    if unknown:
        known = ", ".join(sorted(_KNOWN_PARAMS))
        raise ValueError(f"params.{unknown[0]} is not a known parameter, "
                         f"known: {known}")


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


def compute(df: pd.DataFrame, params: dict) -> dict:
    """MACD / signal / histogram を系列で返す。

    `fast < slow` を要求する (設計書 §4): `fast == slow` は macd と hist が
    恒等的に 0 になる退化形、`fast > slow` は全クロスの符号が反転して
    「macd が signal を上抜けたら買い」が静かに逆売買になる。
    """
    _reject_unknown_params(params)
    fast = _int_param(params, "fast", _DEFAULTS["fast"])
    slow = _int_param(params, "slow", _DEFAULTS["slow"])
    signal_period = _int_param(params, "signal_period",
                               _DEFAULTS["signal_period"])
    if fast >= slow:
        raise ValueError(
            f"params.fast must be < params.slow, got fast={fast} slow={slow}")
    close = df["close"].astype(float)
    fast_ema = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    slow_ema = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd = fast_ema - slow_ema
    signal = macd.ewm(span=signal_period, adjust=False,
                      min_periods=signal_period).mean()
    return {"macd": macd, "signal": signal, "hist": macd - signal}
