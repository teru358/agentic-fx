"""キャッシュ読み込み窓の計算 + 要求 interval への floor。

DB 非依存の純関数モジュール — `sqlite3.Connection` も `PriceProvider`
インスタンスも受け取らない。spec:
docs/superpowers/specs/2026-08-10-ohlcv-cache-fallback-design.md §3.1/3.2
(改訂 5 が本文に優先するが、本ファイルが実装する窓計算・floor の規則
自体は改訂 5 でも不変)。

`base_candidates`/`finest_native_base`/`DERIVE_ONLY_INTERVALS` の定義元は
このモジュール。Task 8 時点では `price_provider.py` の既存実装と一時的に
重複していたが、Task 10 で `PriceProvider._base_candidates` /
`_finest_native_base` をこのモジュールへの委譲に置き換えて解消済み
(重複は残っていない)。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentic_fx.datafeed import sources
from agentic_fx.datafeed.health import DataUnhealthy

# 境界がソース依存の足 (price_provider.py:33 と同じ定義)。ネイティブに
# 持っていても常に細かい足から導出する — ブローカー格子の裏口混入を防ぐ。
DERIVE_ONLY_INTERVALS = frozenset({"4h", "1d"})

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def base_candidates(interval: str) -> list[str]:
    """interval を導出できる base 足を、粗い順に返す (ソース非依存)。"""
    want = sources.INTERVAL_MIN[interval]
    cands = [i for i, m in sources.INTERVAL_MIN.items()
             if i not in DERIVE_ONLY_INTERVALS and m < want and want % m == 0]
    return sorted(cands, key=lambda i: sources.INTERVAL_MIN[i], reverse=True)


def finest_native_base(source: str, interval: str) -> str:
    """interval を導出できる、最も粗いネイティブ足を選ぶ。"""
    native = sources.NATIVE_INTERVALS[source]
    for base in base_candidates(interval):
        if base in native:
            return base
    raise DataUnhealthy(f"{source} cannot provide or derive {interval}")


def live_window_days(source: str, interval: str, lookback_days: int) -> int:
    """spec §3.1: キャッシュの読み込み窓を、ライブ経路が同じ要求に対して
    取る期間に揃える。

    窓は要求 (source, interval, lookback_days) だけで決まり、キャッシュ側で
    どの base を使うか (実際に何がキャッシュされているか) には一切依存
    しない — この関数はキャッシュの中身を読まない (DB 非依存)。

    **定義域は `lookback_days >= 1`**。`lookback_days=0` は窓 0 日
    (`window_start == now`) に退化し、ライブ経路 (`_fetch_native` の
    `days = max(1, int(lookback_days))`) が最低 1 日に持ち上げるのとは
    非対称になる。本番呼び出し元で 0 を渡すものは無い (既定 5 /
    `latest_1m_bar` は 1)。ここで `max(1, ...)` を掛けて対称化しない
    のは、`tests/datafeed/test_price_provider.py` の floor 防御
    (直読候補は floor しない / 導出候補は floor する) が
    `lookback_days=0` を `window_start == now` を観測可能にする道具として
    使っているため — 対称化するとその観測点が潰れる (束 C 1 周目 実測:
    `max(1, ...)` を入れると当該 2 テストが赤)。
    """
    if (interval in sources.NATIVE_INTERVALS[source]
            and interval not in DERIVE_ONLY_INTERVALS):
        return lookback_days
    base = finest_native_base(source, interval)
    ratio = sources.INTERVAL_MIN[interval] / sources.INTERVAL_MIN[base]
    return int(lookback_days * ratio)


def floor_to_interval(ts: datetime, interval: str) -> datetime:
    """UTC epoch 錨のバケット開始時刻へ切り下げる。"""
    if ts.tzinfo is None:
        raise ValueError(
            "floor_to_interval: naive datetime is rejected "
            "(tz-aware UTC required)")
    ts_utc = ts.astimezone(timezone.utc)
    width = timedelta(minutes=sources.INTERVAL_MIN[interval])
    return _EPOCH + ((ts_utc - _EPOCH) // width) * width
