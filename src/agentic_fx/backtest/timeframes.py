"""上位足供給 — ohlcv の 1m 行からの読み取り時リサンプル (プラン 7 Task 0)。

設計判断 (ユーザー裁定 2026-08-02): 導出保存ではなく**読み取り時リサンプル**。
①導出保存は ohlcv の既存行不変契約 (import_bars) と衝突する ②spec §5
「細かい足から resample」と同一思想 ③`datafeed.bars.resample` は
`BAR_ANCHOR="epoch"` でバックテストのバケット錨と整合済み。

interval → pandas freq の写像は既存 `datafeed.bars.pandas_rule` のみを使う
(新写像テーブルを作らない — 既存は "1d"→"1D" を意図的に採っており再複製は
食い違いの温床。opus R2 I6)。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd

from agentic_fx.datafeed.bars import pandas_rule, resample

# 本モジュールの許容 timeframe の正規列挙 (分析用に 15m を含む)。
RESAMPLE_TIMEFRAMES = ("1m", "15m", "1h", "4h", "1d")

# signal/strategy plugin の宣言 timeframe の正規列挙 (D5 — 1m を除く
# RESAMPLE_TIMEFRAMES)。1m は約定判定の足そのもの、5m は resample 供給面の
# 拡張が要るため本プランでは不可。将来の拡張点はこの定数 +
# RESAMPLE_TIMEFRAMES/TF_MINUTES + signals store の CASE 式 (Task 7) の
# 3 箇所に集約されている (5m 追加は小 task 1 個で可能な設計)。
PLUGIN_TIMEFRAMES = ("15m", "1h", "4h", "1d")

TF_MINUTES = {"1m": 1, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}

assert set(TF_MINUTES) == set(RESAMPLE_TIMEFRAMES)
assert PLUGIN_TIMEFRAMES == tuple(t for t in RESAMPLE_TIMEFRAMES if t != "1m")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_COLUMNS = ("open", "high", "low", "close", "volume")


def _require_aware_utc(dt: datetime, label: str) -> datetime:
    """naive は ValueError (fail closed)、aware は UTC へ正規化。"""
    if dt.tzinfo is None:
        raise ValueError(f"{label} is naive; tz-aware UTC datetime required "
                         "(cannot safely assume UTC)")
    return dt.astimezone(timezone.utc)


def _validate_timeframe(timeframe: str) -> None:
    if timeframe not in RESAMPLE_TIMEFRAMES:
        raise ValueError(
            f"timeframe {timeframe!r} is not one of {RESAMPLE_TIMEFRAMES}")


def floor_to_bucket(ts: datetime, timeframe: str) -> datetime:
    """UTC epoch 錨のバケット開始時刻へ切り下げる (Task 8 producer が使う)。

    epoch (1970-01-01T00:00Z) は分・時・日の格子に整列しているので、
    「epoch からの経過分を timeframe 幅で切り捨てる」= `datafeed.bars.
    resample` の `origin="epoch"` と同一の境界になる。1d の境界は UTC 00:00
    であり FX の取引日境界 (NY 17:00 ロールオーバー) ではない。
    """
    _validate_timeframe(timeframe)
    ts_utc = _require_aware_utc(ts, "ts")
    width = timedelta(minutes=TF_MINUTES[timeframe])
    return _EPOCH + ((ts_utc - _EPOCH) // width) * width


def load_resampled_frame(conn: sqlite3.Connection, symbol: str,
                         timeframe: str, *, source: str,
                         since: datetime | None = None,
                         until: datetime | None = None,
                         max_bars: int | None = None) -> pd.DataFrame:
    """ohlcv の 1m 行 (単一 source) を timeframe へ読み取り時リサンプルする。

    契約:
    - ``until`` は**排他**。返すのは「until 時点で終端が確定した完成バケット」
      のみ (``bucket_start + tf 幅 <= until`` — 末尾の部分バケットは落とす。
      先読み防止)。until 無しでは完成判定の基準点が無いため、"1m" 以外
      (および max_bars 指定時) は ValueError (fail closed)。
    - ``since``: 「**since 以降に開始する完成バケットのみ**を返す」。SQL は
      since から素直に読む。錨は epoch 固定でデータ開始位置に依存しない
      ため、これで部分集約は生じない (境界前に開始するバケットはラベルが
      since より前になり index フィルタで落ちる — opus R2 I2。codex R1 I6
      の floor 拡張は `index >= since` フィルタと打ち消し合うため採らない)。
    - ``max_bars``: 末尾 max_bars 本のみ返し、SQL 読み出しも
      ``until - max_bars×tf×2`` (安全係数 2 — 1m 欠損を許容) までに制限する
      (毎評価の全履歴読みを防ぐ — opus R2 I1/I9)。窓の下限がバケット境界に
      乗らない場合、先頭バケットが部分集約になり得るが、安全係数 2 の余裕
      の中で tail(max_bars) に生き残ることは完全欠損級のデータ穴が無い限り
      ない。
    - バケット内の 1m 欠損は「在る分だけの集約」— runner の
      ``_aggregate_bucket``・§6「バー欠損を市場クローズとみなさない」と
      同一規則 (codex R1 I7)。
    - DataFrame は ``bars_to_df`` と同一契約 (index=UTC DatetimeIndex,
      columns=OHLCV, 昇順)。1 年分の 1m (~50 万行) を Bar オブジェクト経由
      で作るのは無駄なので SQL 行から直接構築する。
    """
    _validate_timeframe(timeframe)
    if until is None:
        if timeframe != "1m" or max_bars is not None:
            raise ValueError(
                "until is required (bucket completeness and the max_bars SQL "
                "window have no reference point without it)")
        until_utc = None
    else:
        until_utc = _require_aware_utc(until, "until")
    since_utc = None if since is None else _require_aware_utc(since, "since")
    if max_bars is not None and max_bars < 1:
        raise ValueError("max_bars must be >= 1")

    width = timedelta(minutes=TF_MINUTES[timeframe])

    lower: datetime | None = since_utc
    if max_bars is not None:
        window_lower = until_utc - max_bars * width * 2
        lower = window_lower if lower is None else max(lower, window_lower)

    q = ("SELECT bar_time, open, high, low, close, volume FROM ohlcv "
         "WHERE symbol=? AND interval='1m' AND source=?")
    args: list = [symbol, source]
    if lower is not None:
        q += " AND bar_time >= ?"
        args.append(lower.isoformat())
    if until_utc is not None:
        q += " AND bar_time < ?"
        args.append(until_utc.isoformat())
    rows = conn.execute(q + " ORDER BY bar_time", args).fetchall()

    if not rows:
        return pd.DataFrame(
            {c: pd.Series(dtype="float64") for c in _COLUMNS},
            index=pd.DatetimeIndex([], tz="UTC"))

    df = pd.DataFrame(
        {"open": [r["open"] for r in rows],
         "high": [r["high"] for r in rows],
         "low": [r["low"] for r in rows],
         "close": [r["close"] for r in rows],
         "volume": [r["volume"] for r in rows]},
        index=pd.DatetimeIndex(
            pd.to_datetime([r["bar_time"] for r in rows], utc=True)),
        columns=list(_COLUMNS))

    if timeframe != "1m":
        df = resample(df, pandas_rule(timeframe))

    if until_utc is not None:
        # 完成バケットのみ: bucket_end (= label + tf 幅) <= until
        df = df[df.index + width <= until_utc]
    if since_utc is not None:
        df = df[df.index >= since_utc]
    if max_bars is not None:
        df = df.tail(max_bars)
    return df
