"""timeframes — 上位足の読み取り時リサンプル (プラン 7 Task 0)。

契約 (プラン Task 0):
- until は**排他**。末尾の部分バケット (until 時点で終端未確定 =
  bucket_end > until) は落とす (先読み防止)。
- since は「since 以降に開始する完成バケットのみ」(epoch 錨なので部分集約
  は生じない)。
- max_bars 指定時は末尾 max_bars 本のみ返し、SQL 読み出しも
  `until - max_bars×tf×2` (安全係数 2) までに制限する。
- バケット内の 1m 欠損は「在る分だけの集約」(codex R1 I7)。

プラン記述の欠陥修正 (レジャー参照): プラン原文の
`test_max_bars_limits_result_and_sql_window` は index[-1] == H+3h を期待
するが、since テストが固定する完成判定 (bucket_end <= until) の下では
until=H+300m のとき bucket [H+4h, H+5h) は完成 → 正しくは H+4h。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.backtest.timeframes import (
    PLUGIN_TIMEFRAMES, RESAMPLE_TIMEFRAMES, TF_MINUTES, floor_to_bucket,
    load_resampled_frame,
)
from agentic_fx.core.contracts import Bar
from agentic_fx.store import ohlcv

from tests.backtest.factories import H, _conn, _row_at


def test_enums_and_minutes_are_consistent():
    # 段階 3: 5m が base_interval の一般化 (A2/A3) で正規列挙に追加された
    # (production の意図変更 — 5m 基底のバックテストが対象)。
    assert RESAMPLE_TIMEFRAMES == ("1m", "5m", "15m", "1h", "4h", "1d")
    assert PLUGIN_TIMEFRAMES == ("15m", "1h", "4h", "1d")
    # plugin 宣言足は「1m/5m を除く RESAMPLE_TIMEFRAMES」(D5、5m は基底専用)
    assert PLUGIN_TIMEFRAMES == tuple(
        tf for tf in RESAMPLE_TIMEFRAMES if tf not in ("1m", "5m"))
    assert TF_MINUTES == {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240,
                          "1d": 1440}


# --- floor_to_bucket (Task 8 producer が使う epoch 錨切り下げ) ----------

def test_floor_to_bucket_epoch_anchor():
    ts = datetime(2026, 7, 22, 14, 37, 12, 345678, tzinfo=timezone.utc)
    assert floor_to_bucket(ts, "1h") == datetime(
        2026, 7, 22, 14, 0, tzinfo=timezone.utc)
    assert floor_to_bucket(ts, "15m") == datetime(
        2026, 7, 22, 14, 30, tzinfo=timezone.utc)
    # 4h の epoch 錨は 00/04/08/12/16/20 時
    assert floor_to_bucket(ts, "4h") == datetime(
        2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    # 1d は UTC 00:00 (epoch は深夜整列なので日付切り下げと一致)
    assert floor_to_bucket(ts, "1d") == datetime(
        2026, 7, 22, 0, 0, tzinfo=timezone.utc)


def test_floor_to_bucket_identity_on_boundary():
    assert floor_to_bucket(H, "1h") == H
    assert floor_to_bucket(H, "4h") == H  # H = 12:00 UTC は 4h 境界


def test_floor_to_bucket_rejects_naive_and_unknown_tf():
    with pytest.raises(ValueError):
        floor_to_bucket(datetime(2026, 7, 22, 14, 0), "1h")  # naive
    with pytest.raises(ValueError):
        floor_to_bucket(H, "10m")  # 列挙外 (段階 3 で 5m が正規列挙入りしたため 10m に変更)


def test_floor_to_bucket_normalizes_non_utc_offset():
    # +09:00 表記の同一瞬間も UTC の同一バケットへ
    jst = timezone(timedelta(hours=9))
    ts = datetime(2026, 7, 22, 23, 37, tzinfo=jst)  # = 14:37 UTC
    assert floor_to_bucket(ts, "1h") == datetime(
        2026, 7, 22, 14, 0, tzinfo=timezone.utc)


# --- load_resampled_frame (プラン Step 1 の 5 テスト) --------------------

def test_resampled_1h_bucket_anchor_and_ohlc(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100 + i, h=100 + i + 0.5,
                    l=100 + i - 0.5, c=100 + i + 0.2) for i in range(90)]
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    df = load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                             base_interval="1m",
                              until=H + timedelta(minutes=90))
    assert len(df) == 1 and df.index[0].to_pydatetime() == H
    assert df.iloc[0]["open"] == 100 and df.iloc[0]["high"] == 159.5
    # close は最後の 1m バー (i=59) の close、low は最小値
    assert df.iloc[0]["close"] == 159.2 and df.iloc[0]["low"] == 99.5


def test_partial_tail_bucket_dropped_lookahead_guard(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100, h=100.5, l=99.5, c=100)
            for i in range(120)]
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    df = load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                             base_interval="1m",
                              until=H + timedelta(minutes=61))
    assert len(df) == 1


def test_since_returns_only_buckets_starting_at_or_after_since(tmp_path):
    """契約: since 以降に開始する完成バケットのみ (epoch 錨なので部分集約は生じない)。"""
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100 + i, h=100 + i + 0.5,
                    l=100 + i - 0.5, c=100 + i) for i in range(120)]
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    df = load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                             base_interval="1m",
                              since=H + timedelta(minutes=30),
                              until=H + timedelta(minutes=120))
    assert len(df) == 1 and df.iloc[0]["open"] == 160  # [H+1h,) のみ・欠け open なし


def test_max_bars_limits_result_and_sql_window(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100, h=100.5, l=99.5, c=100)
            for i in range(300)]
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    # codex R3 M2: SQL 側の読み出し下限も観測する (tail() で誤魔化せないよう
    # set_trace_callback で発行 SQL を記録し、bar_time >= のパラメータが
    # until - max_bars*tf*2 に一致することを assert する)
    seen_sql: list[str] = []
    conn.set_trace_callback(seen_sql.append)
    df = load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                             base_interval="1m",
                              until=H + timedelta(minutes=300), max_bars=2)
    conn.set_trace_callback(None)
    # プラン原文は index[-1]==H+3h だが完成判定 (bucket_end <= until) と
    # 矛盾 (レジャー参照)。[H+3h, H+4h] の 2 本が正。
    assert len(df) == 2
    assert df.index[-1].to_pydatetime() == H + timedelta(hours=4)
    assert df.index[0].to_pydatetime() == H + timedelta(hours=3)
    assert any((H + timedelta(minutes=300 - 2 * 60 * 2)).isoformat()
               in s for s in seen_sql)  # SQL 窓の下限が実際に渡っている


def test_max_bars_window_lower_is_bucket_aligned_no_partial_head(tmp_path):
    """F1 (レビュー Fix Round 1, codex High): max_bars の SQL 読み出し窓下限
    (``until - max_bars*width*2``) はバケット境界に非整列になり得る。境界
    へ切り下げないと、先頭バケットの冒頭行が SQL の WHERE bar_time >=
    window_lower で削られ「部分集約」のまま tail(max_bars) に生き残る
    (codex 再現例: tf=1h, until=H+5h30m, max_bars=2)。

    再現データ: [H+1h,H+2h) バケット内に窓境界 (H+90分) を跨ぐ 2 行
    (H+65分・H+115分、open を大きく違えて識別可能にする)、[H+2h,H+4h) は
    無データ (ギャップ)、[H+4h,H+5h) に 1 行。until=H+5h30m・max_bars=2 の
    完成バケットは [H+1h,H+2h) と [H+4h,H+5h) の 2 本のみ。
    """
    conn = _conn(tmp_path)
    rows = [
        _row_at(H + timedelta(minutes=65), o=100, h=100.5, l=99.5, c=100),
        _row_at(H + timedelta(minutes=115), o=200, h=200.5, l=199.5, c=200),
        _row_at(H + timedelta(minutes=245), o=300, h=300.5, l=299.5, c=300),
    ]
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    df = load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                             base_interval="1m",
                              until=H + timedelta(hours=5, minutes=30),
                              max_bars=2)
    assert len(df) == 2
    assert df.index[0].to_pydatetime() == H + timedelta(hours=1)
    assert df.index[-1].to_pydatetime() == H + timedelta(hours=4)
    # 先頭バケットの open は「バケット内で時系列最初の行」(H+65分,
    # open=100) でなければならない。窓下限がバケット境界 (H+60分) へ切り
    # 下げられず生の H+90分のままだと、H+65分の行が SQL で除外されて
    # H+115分の行 (open=200) が誤って先頭行扱いされる。
    assert df.iloc[0]["open"] == 100


def test_max_bars_completeness_cut_precedes_tail_not_after(tmp_path):
    """F3 (レビュー Fix Round 1, sonnet Important-2): 「完成カット → since
    → tail」の順序が保たれていることを直接検証する — tail を完成カットより
    先に適用する変異は既存テストでは検出できなかった。

    tf=1h, until=H+5h30m (オフグリッド), max_bars=2, H〜H+5h30m に密な 1m
    データ。正実装: 完成バケットは ...[H+3h,H+4h),[H+4h,H+5h) まで (末尾の
    形成中バケット [H+5h,H+6h) は bucket_end=H+6h > until で除外) →
    tail(2) = [H+3h, H+4h] (len 2)。tail 先行の変異: 生のリサンプル結果
    (末尾は [H+5h,H+6h) の形成中バケットまで含む) から tail(2) を取ると
    [H+4h, H+5h] になり、その後の完成カットで [H+5h,H+6h) 相当の行が落ち
    len 1 だけが残る — len と index の両方で判別できる。
    """
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100, h=100.5, l=99.5, c=100)
            for i in range(330)]  # H 〜 H+5h30m、密な 1m データ
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    df = load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                             base_interval="1m",
                              until=H + timedelta(hours=5, minutes=30),
                              max_bars=2)
    assert len(df) == 2
    assert df.index[0].to_pydatetime() == H + timedelta(hours=3)
    assert df.index[-1].to_pydatetime() == H + timedelta(hours=4)


def test_intra_bucket_gap_aggregates_present_bars(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100, h=100.5, l=99.5, c=100)
            for i in range(60) if i != 30]
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    df = load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                             base_interval="1m",
                              until=H + timedelta(minutes=60))
    assert len(df) == 1 and df.iloc[0]["volume"] == 59 * 10.0


# --- 契約の細部 (fail closed / 1m 素通し / 空) ---------------------------

def test_1m_is_passthrough_not_resampled(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100 + i, h=100.5 + i,
                    l=99.5 + i, c=100 + i) for i in range(3)]
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    df = load_resampled_frame(conn, "USDJPY", "1m", source="dukascopy",
                             base_interval="1m",
                              until=H + timedelta(minutes=3))
    assert len(df) == 3
    assert list(df["open"]) == [100, 101, 102]


def test_1m_partial_tail_minute_dropped(tmp_path):
    """1m でも完成判定は同一規則: bar_ts + 1m > until の行は返さない。"""
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100, h=100.5, l=99.5, c=100)
            for i in range(3)]
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    df = load_resampled_frame(conn, "USDJPY", "1m", source="dukascopy",
                             base_interval="1m",
                              until=H + timedelta(minutes=2, seconds=30))
    assert len(df) == 2  # 3 本目 (H+2m) は終端 H+3m > until で未確定


def test_unknown_timeframe_rejected(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        load_resampled_frame(conn, "USDJPY", "10m", source="dukascopy",
                             base_interval="1m",
                             until=H)


def test_until_required_for_resampled_timeframes(tmp_path):
    """until 無しでは完成判定の基準点が無い — fail closed (レジャー裁定)。"""
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                             base_interval="1m")
    with pytest.raises(ValueError):  # max_bars の SQL 窓も until 依存
        load_resampled_frame(conn, "USDJPY", "1m", source="dukascopy",
                             base_interval="1m",
                             max_bars=10)


def test_naive_since_until_rejected(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                             base_interval="1m",
                             until=datetime(2026, 7, 22, 13, 0))  # naive
    with pytest.raises(ValueError):
        load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                             base_interval="1m",
                             since=datetime(2026, 7, 22, 12, 0),  # naive
                             until=H + timedelta(hours=1))


def test_max_bars_must_be_positive(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                             base_interval="1m",
                             until=H + timedelta(hours=1), max_bars=0)


def test_empty_history_returns_empty_frame(tmp_path):
    conn = _conn(tmp_path)
    df = load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                             base_interval="1m",
                              until=H + timedelta(hours=1))
    assert len(df) == 0
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


def test_source_is_filtered(tmp_path):
    """単一 source の SQL 直読み — 他 source の行を混ぜない。"""
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100, h=100.5, l=99.5, c=100)
            for i in range(60)]
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    ohlcv.upsert_cache_bars(
        conn,
        [Bar("USDJPY", "1m", H + timedelta(minutes=i), 200, 200.5, 199.5, 200,
             10.0) for i in range(60)],
        source="yfinance")
    df = load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                             base_interval="1m",
                              until=H + timedelta(minutes=60))
    assert len(df) == 1 and df.iloc[0]["open"] == 100


def test_1d_bucket_anchored_at_utc_midnight(tmp_path):
    """1d の境界は UTC 00:00 (epoch 錨) — NY ロールオーバーではない。"""
    conn = _conn(tmp_path)
    day = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
    # 1 日ぶんを疎に (0:00 と 23:59 の 2 本 — 在る分だけの集約)
    rows = [_row_at(day, o=100, h=101, l=99, c=100.5),
            _row_at(day + timedelta(hours=23, minutes=59),
                    o=102, h=103, l=101, c=102.5)]
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    df = load_resampled_frame(conn, "USDJPY", "1d", source="dukascopy",
                             base_interval="1m",
                              until=day + timedelta(days=1))
    assert len(df) == 1 and df.index[0].to_pydatetime() == day
    assert df.iloc[0]["open"] == 100 and df.iloc[0]["close"] == 102.5
    assert df.iloc[0]["high"] == 103 and df.iloc[0]["low"] == 99


# --- source からのテーブル導出 (プラン 9 Task 16 欠陥 #6 の修正) ---------
# load_resampled_frame は backtest 専用ではなく、本番の signal_producer
# (source = settings.plugin.producer_source = "yfinance" 既定) からも呼ば
# れる共有リーダ。テーブルを `FROM ohlcv_history` に固定すると、ライブ経路
# は空の履歴テーブルを読み、signal が永久に出ない (fail-open で WARNING が
# 出るだけ) 状態になる。読むテーブルは source から導出する。

def test_live_source_reads_the_cache_table(tmp_path):
    """ライブ source (LIVE_SOURCES) はキャッシュテーブルを読む。"""
    conn = _conn(tmp_path)
    bars = [Bar("USDJPY", "1m", H + timedelta(minutes=i),
                100.0, 101.0, 99.0, 100.5, 10.0) for i in range(60)]
    ohlcv.upsert_cache_bars(conn, bars, source="yfinance")
    df = load_resampled_frame(conn, "USDJPY", "1h", source="yfinance",
                             base_interval="1m",
                              until=H + timedelta(minutes=60))
    assert len(df) == 1
    assert df.index[0].to_pydatetime() == H


def test_history_source_does_not_read_cache_rows(tmp_path):
    """履歴 source は、同じ symbol/interval のキャッシュ行を読まない
    (テーブル導出が「どちらか一方」であることの観測点 — 両方 UNION する
    実装なら本テストが落ちる)。"""
    conn = _conn(tmp_path)
    bars = [Bar("USDJPY", "1m", H + timedelta(minutes=i),
                100.0, 101.0, 99.0, 100.5, 10.0) for i in range(60)]
    ohlcv.upsert_cache_bars(conn, bars, source="yfinance")
    df = load_resampled_frame(conn, "USDJPY", "1h", source="dukascopy",
                             base_interval="1m",
                              until=H + timedelta(minutes=60))
    assert df.empty


def test_live_source_does_not_read_history_rows(tmp_path):
    """逆向き — ライブ source は履歴行を読まない。"""
    conn = _conn(tmp_path)
    rows = [_row_at(H + timedelta(minutes=i), o=100, h=101, l=99, c=100.5)
            for i in range(60)]
    ohlcv.import_history_bars(conn, rows, source="dukascopy")
    df = load_resampled_frame(conn, "USDJPY", "1h", source="yfinance",
                             base_interval="1m",
                              until=H + timedelta(minutes=60))
    assert df.empty


def test_unknown_source_is_rejected(tmp_path):
    """どちらの allowlist にも属さない source は fail closed。
    (テーブル既定値へ暗黙に落ちる実装なら本テストが落ちる)"""
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="KNOWN_OHLCV_SOURCES"):
        load_resampled_frame(conn, "USDJPY", "1h", source="yfinace",
                             base_interval="1m",
                             until=H + timedelta(minutes=60))
