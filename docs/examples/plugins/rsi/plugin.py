"""RSI (Relative Strength Index、Wilder 平滑) indicator plugin。

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

3. **`max_bars` は 400。** この指標は平滑量どうしの**比**を取り、その比が
   非線形な除算を通って次の段へ入る。したがって線形再帰の上界式は
   **使えず、400 本で一致するという普遍的な保証は書けない**
   (設計書 §3.2 (i-b))。保証の形は「設計書 §6 I5 が生成式と seed まで
   仕様化した fixture 集合での受入公差」であって、任意の入力に対する
   上界ではない。値動きの無い期間で比が退化する件は、下の
   「値動きの無い期間は中立値を返す」の項 (ε 規則) で扱う。
   **この indicator に依存する strategy は、自分の `max_bars` を 400 以上
   に宣言すること** — strategy worker は依存に
   `df.tail(min(strategy.max_bars, 400))` を渡すので、小さく宣言すると
   渡る履歴が短くなり値がわずかにずれる。

4. **純関数であること。** `df` と `params` を書き換えない (必要なら新しい
   Series を作る)。モジュールレベルの状態を持たない。

5. **値動きの無い期間は中立値を返す** (設計書 §3.2 (i-b))。判定は
   `<= EPS * |close|` の相対 ε (EPS = 1e-9)。`|close|` は**その行自身の
   close**。strategy 側で「中立」と「板が動いていない」を読み分けたいなら
   `atr` を併せて宣言して自分で判定すること。

**未知の `params` キーは `ValueError` にする。** この plugin が読むキーは
下の `_DEFAULTS` がすべてである。strategy 側の params 上書きは承認不要
なので、`period` のつもりで綴りを誤ったまま黙って既定値で動くと、その
値を前提にした backtest 結果が出てしまう。

入力に NaN は無いものとする (挙動は未規定。ただし例外は送出しない)。
"""
from __future__ import annotations

import pandas as pd



EPS = 1e-9


#: **この plugin が読む params と既定値。** `config.yaml` の `params` はこの表と
#: 一致していなければならない (設計書 §4)。既定値をここに 1 箇所だけ持ち、
#: `compute` も受入テストもここを読む。
_DEFAULTS = {"period": 14}
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
    """Wilder 平滑の RSI を系列で返す。

    **退化規則 (設計書 §3.2 (i-b))**: 値動きが実質ゼロの区間では
    `avg_gain` と `avg_loss` が同率で減衰し、その比が残るため「横ばいなのに
    RSI が 100」という誤った値になる。相対 ε で潰す。**判定順は固定**:

      (1) `avg_gain + avg_loss <= EPS * |close|`  -> 50.0 (値動きなし = 中立)
      (2) それ以外で `avg_loss <= EPS * |close|`  -> 100.0 (上げのみ)
      (3) それ以外は式どおり

    `|close|` は **その行自身の close** (shift も平均も使わない)。

    **配備済 `rsi_indicator` との差異**: あちらは厳密な `== 0` 判定で
    「下げが無い -> 100」しか持たない。横ばい相場で両者の値は一致しない
    (既存 100 / 本 plugin 50)。**両方を同じ strategy で混ぜて使わないこと。**
    """
    _reject_unknown_params(params)
    period = _int_param(params, "period", _DEFAULTS["period"])
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False,
                        min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False,
                        min_periods=period).mean()
    threshold = EPS * close.abs()
    rsi = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    rsi = rsi.where(avg_loss > threshold, 100.0)
    rsi = rsi.where(avg_gain + avg_loss > threshold, 50.0)
    rsi = rsi.where(~(avg_gain.isna() | avg_loss.isna()))
    return {"rsi": rsi}
