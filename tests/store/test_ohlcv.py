import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.contracts import Bar
from agentic_fx.store import ohlcv
from agentic_fx.store.db import connect, init_db

NOW_ISO = "2026-07-22T12:00:00+00:00"
ROW = ("USDJPY", "1m", NOW_ISO, 148.0, 148.2, 147.9, 148.1, 100.0, 0.012)


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


# ---- IMPORT_SOURCES / LIVE_SOURCES の対称性 --------------------------------

def test_import_sources_and_live_sources_are_disjoint():
    """D2 設計の中核: mt5 (履歴) と mt5-live (キャッシュ) が同じ集合に
    絶対に入らないこと。"""
    assert ohlcv.IMPORT_SOURCES.isdisjoint(ohlcv.LIVE_SOURCES)
    assert ohlcv.IMPORT_SOURCES == frozenset({"dukascopy", "mt5"})
    assert ohlcv.LIVE_SOURCES == frozenset({"yfinance", "twelvedata", "mt5-live"})


def test_known_ohlcv_sources_is_the_union():
    assert ohlcv.KNOWN_OHLCV_SOURCES == ohlcv.LIVE_SOURCES | ohlcv.IMPORT_SOURCES


# ---- import_history_bars (旧 import_bars) ----------------------------------

def test_import_history_bars_inserts_and_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    r1 = ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    r2 = ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    assert (r1.inserted, r1.unchanged, r1.conflicted) == (1, 0, 0)
    assert (r2.inserted, r2.unchanged, r2.conflicted) == (0, 1, 0)


def test_import_history_bars_writes_to_ohlcv_history_table(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 0


def test_import_history_bars_rejects_live_source(tmp_path):
    """D2 変異リスト: 履歴インポートにライブ source 名を渡しても通って
    しまうと、キャッシュ由来行が履歴テーブルに紛れ込む。"""
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="IMPORT_SOURCES"):
        ohlcv.import_history_bars(conn, [ROW], source="yfinance")
    with pytest.raises(ValueError, match="IMPORT_SOURCES"):
        ohlcv.import_history_bars(conn, [ROW], source="mt5-live")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0


def test_import_history_bars_never_mutates_existing(tmp_path):
    """既存行と値が異なる入力は棄却 — spec §6「既存行は不変」。"""
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    tampered = ROW[:6] + (148.15,) + ROW[7:]
    r = ohlcv.import_history_bars(conn, [tampered], source="dukascopy")
    assert r.conflicted == 1
    assert conn.execute(
        "SELECT close FROM ohlcv_history").fetchone()[0] == 148.1


def test_import_history_bars_spread_null_vs_value_conflicts(tmp_path):
    conn = _conn(tmp_path)
    no_spread = ROW[:8] + (None,)
    ohlcv.import_history_bars(conn, [no_spread], source="dukascopy")
    r_same = ohlcv.import_history_bars(conn, [no_spread], source="dukascopy")
    assert (r_same.inserted, r_same.unchanged, r_same.conflicted) == (0, 1, 0)
    with_spread = ROW  # spread=0.012
    r_conflict = ohlcv.import_history_bars(conn, [with_spread], source="dukascopy")
    assert r_conflict.conflicted == 1
    assert conn.execute(
        "SELECT spread FROM ohlcv_history").fetchone()[0] is None


def test_import_history_bars_float_tolerance_within_1e9_is_unchanged(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    almost_same = ROW[:3] + (ROW[3] + 1e-10,) + ROW[4:]
    r = ohlcv.import_history_bars(conn, [almost_same], source="dukascopy")
    assert (r.inserted, r.unchanged, r.conflicted) == (0, 1, 0)


def test_import_history_bars_conflicted_logs_warning(tmp_path, caplog):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    tampered = ROW[:6] + (148.15,) + ROW[7:]
    with caplog.at_level("WARNING"):
        ohlcv.import_history_bars(conn, [tampered], source="dukascopy")
    assert any("conflict" in r.message.lower() for r in caplog.records)


def test_import_history_bars_rejects_naive_bar_time(tmp_path):
    conn = _conn(tmp_path)
    naive = ROW[:2] + ("2026-07-22T12:00:00",) + ROW[3:]
    with pytest.raises(ValueError, match="aware"):
        ohlcv.import_history_bars(conn, [naive], source="dukascopy")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0


def test_import_history_bars_normalizes_bar_time_representation(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    same_instant_different_repr = (
        ROW[0], ROW[1], "2026-07-22T21:00:00+09:00") + ROW[3:]
    r = ohlcv.import_history_bars(conn, [same_instant_different_repr],
                                  source="dukascopy")
    assert r.unchanged == 1
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 1


def test_import_history_bars_rejects_ohlc_ordering_violation(tmp_path):
    conn = _conn(tmp_path)
    broken = ROW[:3] + (999.0,) + ROW[4:]
    with pytest.raises(ValueError, match="low<="):
        ohlcv.import_history_bars(conn, [broken], source="dukascopy")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0


def test_import_history_bars_rejects_non_finite_values(tmp_path):
    conn = _conn(tmp_path)
    broken = ROW[:3] + (float("nan"),) + ROW[4:]
    with pytest.raises(ValueError, match="finite"):
        ohlcv.import_history_bars(conn, [broken], source="dukascopy")


def test_import_history_bars_rejects_empty_source(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="IMPORT_SOURCES"):
        ohlcv.import_history_bars(conn, [ROW], source="")


def test_import_history_bars_batch_rejects_whole_input_on_late_bad_row(tmp_path):
    conn = _conn(tmp_path)
    good = ROW
    bad = ROW[:3] + (999.0,) + ROW[4:]
    with pytest.raises(ValueError):
        ohlcv.import_history_bars(conn, [good, bad], source="dukascopy")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0


def test_import_history_bars_rolls_back_on_mid_batch_write_failure(tmp_path):
    from agentic_fx.store import ohlcv as ohlcv_module

    conn = _conn(tmp_path)
    row2 = ("USDJPY", "5m", NOW_ISO, 1.0, 2.0, 0.5, 1.5, 10.0, None)

    class _FailingConn:
        def __init__(self, real):
            self._real = real
            self._insert_count = 0

        def execute(self, sql, *args, **kwargs):
            if str(sql).strip().startswith(
                    "INSERT OR IGNORE INTO ohlcv_history "):
                self._insert_count += 1
                if self._insert_count == 2:
                    raise sqlite3.OperationalError("injected failure")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with pytest.raises(sqlite3.OperationalError):
        ohlcv_module.import_history_bars(_FailingConn(conn), [ROW, row2],
                                         source="dukascopy")

    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0

    conn.execute(
        "INSERT OR IGNORE INTO ohlcv_history (symbol, interval, bar_time, "
        "open, high, low, close, volume, source, spread) "
        "VALUES ('EURUSD','1h','2026-07-22T13:00:00+00:00',"
        "1,2,0.5,1.5,10,'dukascopy',NULL)")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 1
    assert conn.execute(
        "SELECT symbol FROM ohlcv_history").fetchone()[0] == "EURUSD"


# ---- load_history_bars (旧 load_bars、source=履歴専用) ---------------------

def test_load_history_bars_filters_by_source(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    ohlcv.import_history_bars(
        conn, [("USDJPY", "1m", NOW_ISO, 1, 2, 0.5, 1.5, 0, None)],
        source="mt5")
    assert len(ohlcv.load_history_bars(
        conn, "USDJPY", "1m", source="dukascopy")) == 1
    assert ohlcv.load_history_bars(
        conn, "USDJPY", "1m", source="dukascopy")[0].open == 148.0


def test_load_history_bars_rejects_live_source(tmp_path):
    """人間 CLI がライブ source を渡したら fail closed — 構造的にストア層で
    強制する (CLI の argparse choices= はこれを補強する第二の壁)。"""
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="IMPORT_SOURCES"):
        ohlcv.load_history_bars(conn, "USDJPY", "1m", source="yfinance")


def test_load_history_bars_rejects_naive_since(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    with pytest.raises(ValueError, match="naive"):
        ohlcv.load_history_bars(conn, "USDJPY", "1m", source="dukascopy",
                                since=datetime(2026, 7, 22))


def test_load_history_bars_rejects_naive_until(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    with pytest.raises(ValueError, match="naive"):
        ohlcv.load_history_bars(conn, "USDJPY", "1m", source="dukascopy",
                                until=datetime(2026, 7, 22))


def test_load_history_bars_jst_since_matches_utc_instant(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    jst = timezone(timedelta(hours=9))
    since_jst = datetime.fromisoformat(NOW_ISO).astimezone(jst)
    bars_jst = ohlcv.load_history_bars(conn, "USDJPY", "1m", source="dukascopy",
                                       since=since_jst)
    bars_utc = ohlcv.load_history_bars(conn, "USDJPY", "1m", source="dukascopy",
                                       since=datetime.fromisoformat(NOW_ISO))
    assert len(bars_jst) == 1
    assert bars_jst == bars_utc


def test_load_history_spread_returns_value(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    assert ohlcv.load_history_spread(conn, "USDJPY", "1m", NOW_ISO,
                                     source="dukascopy") == 0.012


def test_load_history_spread_none_when_missing(tmp_path):
    conn = _conn(tmp_path)
    assert ohlcv.load_history_spread(conn, "USDJPY", "1m", NOW_ISO,
                                     source="dukascopy") is None


def test_load_history_spread_rejects_live_source(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="IMPORT_SOURCES"):
        ohlcv.load_history_spread(conn, "USDJPY", "1m", NOW_ISO,
                                  source="mt5-live")


def test_load_history_spread_distinguishes_intervals_sharing_bar_time(tmp_path):
    conn = _conn(tmp_path)
    row_1h = ("USDJPY", "1h", NOW_ISO, 148.0, 148.2, 147.9, 148.1, 100.0, 0.02)
    row_1m = ("USDJPY", "1m", NOW_ISO, 148.0, 148.2, 147.9, 148.1, 100.0, 0.01)
    ohlcv.import_history_bars(conn, [row_1h], source="dukascopy")
    ohlcv.import_history_bars(conn, [row_1m], source="dukascopy")
    assert ohlcv.load_history_spread(conn, "USDJPY", "1h", NOW_ISO,
                                     source="dukascopy") == 0.02
    assert ohlcv.load_history_spread(conn, "USDJPY", "1m", NOW_ISO,
                                     source="dukascopy") == 0.01


# ---- upsert_cache_bars / load_cache_bars (旧 upsert_bars/load_bars) --------

def test_upsert_cache_bars_writes_to_ohlcv_cache_table(tmp_path):
    conn = _conn(tmp_path)
    b = Bar("USDJPY", "1m", datetime.fromisoformat(NOW_ISO), 1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_cache_bars(conn, [b], source="yfinance")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0


def test_upsert_cache_bars_still_overwrites_live_cache(tmp_path):
    """live キャッシュ (yfinance) は形成中バー更新のため上書き — 現行維持。"""
    conn = _conn(tmp_path)
    b = Bar("USDJPY", "1m", datetime.fromisoformat(NOW_ISO), 1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_cache_bars(conn, [b], source="yfinance")
    ohlcv.upsert_cache_bars(
        conn, [Bar("USDJPY", "1m", b.ts, 1, 3, 0.5, 2.5, 9)], source="yfinance")
    row = conn.execute("SELECT high, source FROM ohlcv_cache").fetchone()
    assert row["high"] == 3 and row["source"] == "yfinance"


def test_upsert_cache_bars_normalizes_bar_time_to_utc(tmp_path):
    conn = _conn(tmp_path)
    jst_ts = datetime.fromisoformat("2026-07-22T21:00:00+09:00")  # == NOW_ISO
    b = Bar("USDJPY", "1m", jst_ts, 1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_cache_bars(conn, [b], source="yfinance")
    row = conn.execute("SELECT bar_time FROM ohlcv_cache").fetchone()
    assert row["bar_time"] == NOW_ISO
    loaded = ohlcv.load_cache_bars(conn, "USDJPY", "1m", source="yfinance")
    assert len(loaded) == 1 and loaded[0].ts == datetime.fromisoformat(NOW_ISO)


def test_upsert_cache_bars_different_sources_coexist(tmp_path):
    conn = _conn(tmp_path)
    b = Bar("USDJPY", "1m", datetime.fromisoformat(NOW_ISO), 1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_cache_bars(conn, [b], source="yfinance")
    ohlcv.upsert_cache_bars(conn, [b], source="mt5-live")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 2


def test_upsert_cache_bars_rejects_non_live_source(tmp_path):
    """live/import 境界を API 契約で強制する。"""
    conn = _conn(tmp_path)
    b = Bar("USDJPY", "1m", datetime.fromisoformat(NOW_ISO), 1, 2, 0.5, 1.5, 0)
    with pytest.raises(ValueError, match="LIVE_SOURCES"):
        ohlcv.upsert_cache_bars(conn, [b], source="dukascopy")
    with pytest.raises(ValueError, match="LIVE_SOURCES"):
        ohlcv.upsert_cache_bars(conn, [b], source="mt5")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 0


def test_upsert_cache_bars_rejects_naive_bar_time(tmp_path):
    conn = _conn(tmp_path)
    naive_bar = Bar("USDJPY", "1m", datetime(2026, 7, 22, 12, 0),
                    1, 2, 0.5, 1.5, 0)
    with pytest.raises(ValueError, match="naive"):
        ohlcv.upsert_cache_bars(conn, [naive_bar], source="yfinance")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 0


def test_load_cache_bars_rejects_history_source(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="LIVE_SOURCES"):
        ohlcv.load_cache_bars(conn, "USDJPY", "1m", source="dukascopy")


def test_load_cache_bars_rejects_naive_since(tmp_path):
    conn = _conn(tmp_path)
    b = Bar("USDJPY", "1m", datetime.fromisoformat(NOW_ISO), 1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_cache_bars(conn, [b], source="yfinance")
    with pytest.raises(ValueError, match="naive"):
        ohlcv.load_cache_bars(conn, "USDJPY", "1m", source="yfinance",
                              since=datetime(2026, 7, 22))


def test_ohlcv_cache_table_has_no_spread_column(tmp_path):
    """設計書 §12: ohlcv_cache は spread 列を持たない (ライブ経路は埋め
    ないため常に NULL だった不整合そのものを消す)。"""
    conn = _conn(tmp_path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv_cache)")}
    assert "spread" not in cols


# ---- prune_cache ------------------------------------------------------------

def _cache_row(conn, bar_time_iso: str, source="yfinance"):
    b = Bar("USDJPY", "1m", datetime.fromisoformat(bar_time_iso),
           1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_cache_bars(conn, [b], source=source)


def test_prune_cache_deletes_rows_older_than_cutoff(tmp_path):
    conn = _conn(tmp_path)
    _cache_row(conn, "2026-07-01T00:00:00+00:00")
    _cache_row(conn, "2026-07-22T00:00:00+00:00")
    cutoff = datetime(2026, 7, 15, tzinfo=timezone.utc)
    n = ohlcv.prune_cache(conn, cutoff=cutoff, limit=1000)
    assert n == 1
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 1
    remaining = conn.execute("SELECT bar_time FROM ohlcv_cache").fetchone()[0]
    assert remaining == "2026-07-22T00:00:00+00:00"


def test_prune_cache_never_touches_ohlcv_history(tmp_path):
    """D2 最重要変異の対偶: prune はどんな cutoff/limit でも履歴に到達
    できない (テーブルが分かれているため)。"""
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")  # 2026-07-22
    far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    ohlcv.prune_cache(conn, cutoff=far_future, limit=1000)
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 1


def test_prune_cache_respects_limit(tmp_path):
    conn = _conn(tmp_path)
    for i in range(5):
        _cache_row(conn, f"2026-07-0{i+1}T00:00:00+00:00")
    cutoff = datetime(2026, 7, 22, tzinfo=timezone.utc)
    n = ohlcv.prune_cache(conn, cutoff=cutoff, limit=2)
    assert n == 2
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 3


def test_prune_cache_repeated_calls_converge(tmp_path):
    """日次投入量が 1 batch を超えても複数 maintenance/日で追いつく。"""
    conn = _conn(tmp_path)
    cutoff = datetime(2026, 7, 22, tzinfo=timezone.utc)
    batch_limit = 3
    daily_ingest = 7
    assert daily_ingest > batch_limit
    for day in range(3):
        for i in range(daily_ingest):
            _cache_row(conn, f"2026-06-{day * daily_ingest + i + 1:02d}T00:00:00+00:00")
        # scheduler は日次一回ではなく maintenance ごとに呼ぶ。3 回なら
        # capacity=9 > daily_ingest=7 となり、その日の backlog が消える。
        for _ in range(3):
            ohlcv.prune_cache(conn, cutoff=cutoff, limit=batch_limit)
        # 各日の終わりに backlog が消えていること (これが収束の本体)
        assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 0


def test_prune_cache_rejects_naive_cutoff(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="naive"):
        ohlcv.prune_cache(conn, cutoff=datetime(2026, 7, 22), limit=10)


def test_prune_cache_rejects_non_positive_limit(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="limit"):
        ohlcv.prune_cache(conn, cutoff=datetime(2026, 7, 22, tzinfo=timezone.utc),
                          limit=0)
