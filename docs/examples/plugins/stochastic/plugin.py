"""ストキャスティクス (slow、%K / %D) indicator plugin。

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
_DEFAULTS = {"period": 14, "k_period": 3, "d_period": 3}
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

    `params` 自体が dict でない場合、および未知キーの型が混ざっている場合も
    `TypeError` を漏らさない (3 周目 codex Important)。前者は `params.` 自体
    を対象とした文言で `ValueError` にする。後者は `str` でないキーをそれ
    自体不正として扱い (loader は str キーしか通さない契約)、str キーを
    先に・非 str キーを後に置く決定的な順序で 1 つ選ぶ — 素の `sorted` は
    型の混ざった集合を比較できず `TypeError` になるため使わない。
    """
    if not isinstance(params, dict):
        raise ValueError(f"params. must be a mapping, got {type(params).__name__}")
    unknown = sorted(
        (key for key in params if not isinstance(key, str)
         or key not in _KNOWN_PARAMS),
        key=lambda key: (0, key) if isinstance(key, str) else (1, repr(key)),
    )
    if unknown:
        known = ", ".join(sorted(_KNOWN_PARAMS))
        first = unknown[0]
        label = first if isinstance(first, str) else repr(first)
        raise ValueError(f"params.{label} is not a known parameter, "
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
    """slow stochastic の %K / %D を系列で返す (既定 14/3/3、設計書 D2)。

      raw %K = 100 * (close - LL(period)) / (HH(period) - LL(period))
      k      = SMA(k_period) of raw %K
      d      = SMA(d_period) of k

    `HH == LL` (期間内が完全な横ばい) の行は raw %K を 50.0 にする。
    `k_period: 1` を上書きすると fast stochastic になる (承認不要の調整)。
    """
    _reject_unknown_params(params)
    period = _int_param(params, "period", _DEFAULTS["period"])
    k_period = _int_param(params, "k_period", _DEFAULTS["k_period"])
    d_period = _int_param(params, "d_period", _DEFAULTS["d_period"])
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    highest = high.rolling(window=period, min_periods=period).max()
    lowest = low.rolling(window=period, min_periods=period).min()
    span = highest - lowest
    raw_k = 100.0 * (close - lowest) / span
    raw_k = raw_k.where(span != 0.0, 50.0)
    k = raw_k.rolling(window=k_period, min_periods=k_period).mean()
    d = k.rolling(window=d_period, min_periods=d_period).mean()
    return {"k": k, "d": d}
