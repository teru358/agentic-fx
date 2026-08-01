from datetime import datetime

from agentic_fx.core.contracts import Bar
from agentic_fx.store import ohlcv
from agentic_fx.store.db import connect, init_db

NOW_ISO = "2026-07-22T12:00:00+00:00"
ROW = ("USDJPY", "1m", NOW_ISO, 148.0, 148.2, 147.9, 148.1, 100.0, 0.012)


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def test_import_bars_inserts_and_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    r1 = ohlcv.import_bars(conn, [ROW], source="dukascopy")
    r2 = ohlcv.import_bars(conn, [ROW], source="dukascopy")
    assert (r1.inserted, r1.unchanged, r1.conflicted) == (1, 0, 0)
    assert (r2.inserted, r2.unchanged, r2.conflicted) == (0, 1, 0)


def test_import_bars_never_mutates_existing(tmp_path):
    """既存行と値が異なる入力は棄却 — spec §6「既存行は不変」。"""
    conn = _conn(tmp_path)
    ohlcv.import_bars(conn, [ROW], source="dukascopy")
    tampered = ROW[:3] + (999.0,) + ROW[4:]
    r = ohlcv.import_bars(conn, [tampered], source="dukascopy")
    assert r.conflicted == 1
    assert conn.execute("SELECT open FROM ohlcv").fetchone()[0] == 148.0


def test_import_bars_spread_null_vs_value_conflicts(tmp_path):
    """spread の NULL 同士は一致・NULL vs 数値は conflicted。"""
    conn = _conn(tmp_path)
    no_spread = ROW[:8] + (None,)
    ohlcv.import_bars(conn, [no_spread], source="dukascopy")
    # 同じ NULL を再送 → unchanged
    r_same = ohlcv.import_bars(conn, [no_spread], source="dukascopy")
    assert (r_same.inserted, r_same.unchanged, r_same.conflicted) == (0, 1, 0)
    # NULL だった行に数値 spread を送る → conflicted (行は不変)
    with_spread = ROW  # spread=0.012
    r_conflict = ohlcv.import_bars(conn, [with_spread], source="dukascopy")
    assert r_conflict.conflicted == 1
    assert conn.execute("SELECT spread FROM ohlcv").fetchone()[0] is None


def test_import_bars_float_tolerance_within_1e9_is_unchanged(tmp_path):
    """float 誤差 1e-9 未満は同一値扱い (unchanged)。"""
    conn = _conn(tmp_path)
    ohlcv.import_bars(conn, [ROW], source="dukascopy")
    almost_same = ROW[:3] + (ROW[3] + 1e-10,) + ROW[4:]
    r = ohlcv.import_bars(conn, [almost_same], source="dukascopy")
    assert (r.inserted, r.unchanged, r.conflicted) == (0, 1, 0)


def test_import_bars_conflicted_logs_warning(tmp_path, caplog):
    conn = _conn(tmp_path)
    ohlcv.import_bars(conn, [ROW], source="dukascopy")
    tampered = ROW[:3] + (999.0,) + ROW[4:]
    with caplog.at_level("WARNING"):
        ohlcv.import_bars(conn, [tampered], source="dukascopy")
    assert any("conflict" in r.message.lower() for r in caplog.records)


def test_load_bars_filters_by_source(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_bars(conn, [ROW], source="dukascopy")
    ohlcv.upsert_bars(conn, [Bar("USDJPY", "1m",
                                 datetime.fromisoformat(NOW_ISO),
                                 1, 2, 0.5, 1.5, 0)], source="yfinance")
    assert len(ohlcv.load_bars(conn, "USDJPY", "1m", source="dukascopy")) == 1
    assert ohlcv.load_bars(conn, "USDJPY", "1m",
                           source="dukascopy")[0].open == 148.0


def test_upsert_bars_still_overwrites_live_cache(tmp_path):
    """live キャッシュ (yfinance) は形成中バー更新のため上書き — 現行維持。"""
    conn = _conn(tmp_path)
    b = Bar("USDJPY", "1m", datetime.fromisoformat(NOW_ISO), 1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_bars(conn, [b], source="yfinance")
    ohlcv.upsert_bars(conn, [Bar("USDJPY", "1m", b.ts, 1, 3, 0.5, 2.5, 9)],
                      source="yfinance")
    row = conn.execute("SELECT high, source FROM ohlcv").fetchone()
    assert row["high"] == 3 and row["source"] == "yfinance"


def test_upsert_bars_different_sources_coexist(tmp_path):
    """異なる source の upsert は別行として共存する (PK に source を含む)。

    source 名は "mt5" (`price_provider._chain` が使う実チェーン名 — 上書き 1
    参照)。brief 本文は例示として "mt5-live" を挙げるが、`_cached_bars` の
    読み出しは `_chain` の name をそのまま使うため、書き込み側もそれに
    揃えないと読み書きの名前が食い違ってキャッシュが死ぬ (report の逸脱参照)。
    """
    conn = _conn(tmp_path)
    b = Bar("USDJPY", "1m", datetime.fromisoformat(NOW_ISO), 1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_bars(conn, [b], source="yfinance")
    ohlcv.upsert_bars(conn, [b], source="mt5")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 2


def test_load_spread_returns_value(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_bars(conn, [ROW], source="dukascopy")
    assert ohlcv.load_spread(conn, "USDJPY", NOW_ISO, source="dukascopy") == 0.012


def test_load_spread_none_when_missing(tmp_path):
    conn = _conn(tmp_path)
    assert ohlcv.load_spread(conn, "USDJPY", NOW_ISO, source="dukascopy") is None


def test_load_spread_is_deterministic_across_intervals_sharing_bar_time(tmp_path):
    """load_spread は brief 通り interval を取らない。(symbol, bar_time,
    source) だけでは PK (symbol, interval, bar_time, source) を一意に絞れない
    ため、複数 interval が同じ bar_time を持つ場合は `ORDER BY interval` で
    決定的な行を選ぶ (非決定的な結果を避ける) — が、意味的にどちらの
    interval の spread かは呼び出し側からは区別できない (report の逸脱参照)。
    """
    conn = _conn(tmp_path)
    row_1h = ("USDJPY", "1h", NOW_ISO, 148.0, 148.2, 147.9, 148.1, 100.0, 0.02)
    row_1m = ("USDJPY", "1m", NOW_ISO, 148.0, 148.2, 147.9, 148.1, 100.0, 0.01)
    ohlcv.import_bars(conn, [row_1h], source="dukascopy")
    ohlcv.import_bars(conn, [row_1m], source="dukascopy")
    # ORDER BY interval ASC → "1h" < "1m" (文字列比較) が先に来る
    assert ohlcv.load_spread(conn, "USDJPY", NOW_ISO, source="dukascopy") == 0.02
