from datetime import datetime, timedelta, timezone

import pytest
from pandas.tseries.frequencies import to_offset

from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.bars import (
    bars_to_df, df_to_bars, pandas_rule, resample,
)
from agentic_fx.datafeed.sources import INTERVAL_MIN

NOW = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)


def _bars(n=8, interval="1h", start=NOW):
    step = timedelta(hours=1)
    return [Bar("USDJPY", interval, start + step * i,
                148.0 + i, 148.5 + i, 147.5 + i, 148.2 + i, 10.0 + i)
            for i in range(n)]


def test_round_trip_preserves_values_tz_and_interval():
    """bars_to_df → df_to_bars で値・tz・interval が落ちないこと。"""
    src = _bars()
    out = df_to_bars(bars_to_df(src), "USDJPY", "1h")
    assert len(out) == len(src)
    for a, b in zip(src, out):
        assert a.ts == b.ts and b.ts.tzinfo is not None
        assert (a.open, a.high, a.low, a.close) == (b.open, b.high, b.low, b.close)
        assert a.volume == b.volume        # volume を捨てない
        assert b.interval == "1h" and b.symbol == "USDJPY"


def test_resample_aggregates_ohlcv_correctly():
    src = _bars(n=8)                       # 1h × 8 → 4h × 2
    out = df_to_bars(resample(bars_to_df(src), "4h"), "USDJPY", "4h")
    assert len(out) == 2
    assert out[0].open == src[0].open      # first
    assert out[0].close == src[3].close    # last
    assert out[0].high == max(b.high for b in src[:4])
    assert out[0].low == min(b.low for b in src[:4])
    assert out[0].volume == sum(b.volume for b in src[:4])  # sum


def test_resample_bucket_boundary_is_utc_epoch_anchored():
    """バケット境界は UTC 固定。開始時刻がずれても境界は動かない。

    実運用とバックテストで同じ境界を使うことが目的。origin を既定
    (=データ先頭) にすると、取得開始時刻によって 4h の切り方が変わる。
    """
    shifted = _bars(n=8, start=NOW + timedelta(hours=2))  # 02:00 開始
    out = df_to_bars(resample(bars_to_df(shifted), "4h"), "USDJPY", "4h")
    assert out[0].ts == datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
    assert all(b.ts.hour % 4 == 0 for b in out)


def test_resample_drops_incomplete_buckets_only_when_empty():
    """データが無いバケットは落ちるが、部分的なバケットは残る。"""
    out = df_to_bars(resample(bars_to_df(_bars(n=6)), "4h"), "USDJPY", "4h")
    assert len(out) == 2                   # 00-04 完全 + 04-08 部分 (2 本)


def test_resample_drops_gap_buckets():
    """穴の空いたバケット (データ 0 本) は落ちること。"""
    src = _bars(n=2) + _bars(n=1, start=NOW + timedelta(hours=9))
    out = df_to_bars(resample(bars_to_df(src), "4h"), "USDJPY", "4h")
    assert [b.ts.hour for b in out] == [0, 8]   # 04-08 は空なので落ちる


def test_pandas_rule_covers_every_supported_interval():
    """全 interval が pandas の有効な freq に写り、幅が INTERVAL_MIN と一致する。

    プラン記述の `resample(df, interval)` は pandas 3 では成立しない
    ("1m"/"5m"/... は月末 'ME' 扱いで ValueError、"1d" は非推奨)。
    interval 文字列と pandas freq alias は別物なので明示的に写像する。
    """
    for interval, minutes in INTERVAL_MIN.items():
        off = to_offset(pandas_rule(interval))
        assert off.nanos / 1e9 / 60 == minutes, interval


def test_bars_to_df_rejects_naive_timestamps():
    """naive datetime は拒否する (pandas は tz="UTC" で無言に localize する)。

    「naive なら UTC とみなす」はプロジェクト制約で禁止。ここで通すと
    絶対時刻が静かにずれたまま指標計算・約定判定まで流れる。
    """
    naive = [Bar("USDJPY", "1h", datetime(2026, 7, 22, 0, 0),
                 148.0, 148.5, 147.5, 148.2, 10.0)]
    with pytest.raises(ValueError, match="naive"):
        bars_to_df(naive)


def test_bars_to_df_empty_returns_empty_df():
    """空リストでも列構造を保った空 DataFrame を返す (呼び出し側の分岐を減らす)。"""
    df = bars_to_df([])
    assert len(df) == 0
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
