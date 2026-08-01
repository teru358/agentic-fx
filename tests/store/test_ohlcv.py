import sqlite3
from datetime import datetime

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


def test_import_bars_inserts_and_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    r1 = ohlcv.import_bars(conn, [ROW], source="dukascopy")
    r2 = ohlcv.import_bars(conn, [ROW], source="dukascopy")
    assert (r1.inserted, r1.unchanged, r1.conflicted) == (1, 0, 0)
    assert (r2.inserted, r2.unchanged, r2.conflicted) == (0, 1, 0)


def test_import_bars_never_mutates_existing(tmp_path):
    """既存行と値が異なる入力は棄却 — spec §6「既存行は不変」。

    tamper 値は「既存行と違うが、それ自体は有効な OHLC」であること
    (open/high/low/close の順序制約を破る値は F4 の投入前検証で reject
    されてしまい、既存行との比較にすら到達しないため)。close を
    148.1→148.15 に変える (low<=min<=max<=high は維持したまま既存行と
    差異を作る)。
    """
    conn = _conn(tmp_path)
    ohlcv.import_bars(conn, [ROW], source="dukascopy")
    tampered = ROW[:6] + (148.15,) + ROW[7:]
    r = ohlcv.import_bars(conn, [tampered], source="dukascopy")
    assert r.conflicted == 1
    assert conn.execute("SELECT close FROM ohlcv").fetchone()[0] == 148.1


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
    tampered = ROW[:6] + (148.15,) + ROW[7:]
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


def test_upsert_bars_normalizes_bar_time_to_utc(tmp_path):
    """advisor 指摘: import_bars (F4) は bar_time を UTC 正規化するのに
    upsert_bars 側が非対称だと、offset 混在で `since`/`until`/`ORDER BY` の
    文字列比較が静かに壊れる。upsert_bars 側も同じ瞬間なら同じ PK になる
    こと (+09:00 で書いても UTC 表記で読める) を確認する。"""
    conn = _conn(tmp_path)
    jst_ts = datetime.fromisoformat("2026-07-22T21:00:00+09:00")  # == NOW_ISO
    b = Bar("USDJPY", "1m", jst_ts, 1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_bars(conn, [b], source="yfinance")
    row = conn.execute("SELECT bar_time FROM ohlcv").fetchone()
    assert row["bar_time"] == NOW_ISO
    loaded = ohlcv.load_bars(conn, "USDJPY", "1m", source="yfinance")
    assert len(loaded) == 1 and loaded[0].ts == datetime.fromisoformat(NOW_ISO)


def test_upsert_bars_different_sources_coexist(tmp_path):
    """異なる source の upsert は別行として共存する (PK に source を含む)。

    source 名は "mt5-live" (F1: ohlcv 永続化用 source ID。live MT5 は
    "mt5-live" — 一括インポータの "mt5" と衝突させないため。
    `price_provider._chain` のチェーン表示名 "mt5" はここでは使わない —
    `PriceProvider._storage_source` が永続化直前に変換する。ここは
    ohlcv 層を直接叩くテストなので、すでに変換済みの名前を渡す)。
    """
    conn = _conn(tmp_path)
    b = Bar("USDJPY", "1m", datetime.fromisoformat(NOW_ISO), 1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_bars(conn, [b], source="yfinance")
    ohlcv.upsert_bars(conn, [b], source="mt5-live")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 2


def test_upsert_bars_rejects_non_live_source(tmp_path):
    """F5: live allowlist 外の source (import 専用の "dukascopy"、あるいは
    永続化 ID に変換されていない生のチェーン名 "mt5") は upsert_bars で
    拒否される — live/import 境界を API 契約で強制する。"""
    conn = _conn(tmp_path)
    b = Bar("USDJPY", "1m", datetime.fromisoformat(NOW_ISO), 1, 2, 0.5, 1.5, 0)
    with pytest.raises(ValueError, match="live allowlist"):
        ohlcv.upsert_bars(conn, [b], source="dukascopy")
    with pytest.raises(ValueError, match="live allowlist"):
        ohlcv.upsert_bars(conn, [b], source="mt5")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 0


def test_load_spread_returns_value(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_bars(conn, [ROW], source="dukascopy")
    assert ohlcv.load_spread(conn, "USDJPY", "1m", NOW_ISO,
                             source="dukascopy") == 0.012


def test_load_spread_none_when_missing(tmp_path):
    conn = _conn(tmp_path)
    assert ohlcv.load_spread(conn, "USDJPY", "1m", NOW_ISO,
                             source="dukascopy") is None


def test_load_spread_distinguishes_intervals_sharing_bar_time(tmp_path):
    """F7: load_spread は interval 必須引数を取り、PK と同じ 4 列で一意に
    絞り込む。同じ bar_time を共有する別 interval の spread を混同しない。"""
    conn = _conn(tmp_path)
    row_1h = ("USDJPY", "1h", NOW_ISO, 148.0, 148.2, 147.9, 148.1, 100.0, 0.02)
    row_1m = ("USDJPY", "1m", NOW_ISO, 148.0, 148.2, 147.9, 148.1, 100.0, 0.01)
    ohlcv.import_bars(conn, [row_1h], source="dukascopy")
    ohlcv.import_bars(conn, [row_1m], source="dukascopy")
    assert ohlcv.load_spread(conn, "USDJPY", "1h", NOW_ISO,
                             source="dukascopy") == 0.02
    assert ohlcv.load_spread(conn, "USDJPY", "1m", NOW_ISO,
                             source="dukascopy") == 0.01


# ---- F4: 投入前検証・バッチ原子性 -----------------------------------------

def test_import_bars_rejects_naive_bar_time(tmp_path):
    """bar_time は aware datetime として parse できなければならない
    (naive は表記ゆれで同じ瞬間が別 PK になるのを防ぐため拒否)。"""
    conn = _conn(tmp_path)
    naive = ROW[:2] + ("2026-07-22T12:00:00",) + ROW[3:]  # tz なし
    with pytest.raises(ValueError, match="aware"):
        ohlcv.import_bars(conn, [naive], source="dukascopy")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 0


def test_import_bars_normalizes_bar_time_representation(tmp_path):
    """同じ瞬間の別表記 (+00:00 vs Z 相当の別オフセット表記) は正規化されて
    同一 PK になる — 表記ゆれによる重複行を防ぐ (codex M1/sonnet Minor-1)。"""
    conn = _conn(tmp_path)
    ohlcv.import_bars(conn, [ROW], source="dukascopy")  # bar_time = +00:00
    same_instant_different_repr = (
        ROW[0], ROW[1], "2026-07-22T21:00:00+09:00") + ROW[3:]
    r = ohlcv.import_bars(conn, [same_instant_different_repr],
                          source="dukascopy")
    assert r.unchanged == 1  # 別表記でも同じ瞬間なので既存行と一致
    assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 1


def test_import_bars_rejects_ohlc_ordering_violation(tmp_path):
    """low<=min(open,close)<=max(open,close)<=high を満たさない行は拒否。"""
    conn = _conn(tmp_path)
    broken = ROW[:3] + (999.0,) + ROW[4:]  # open が high を超える
    with pytest.raises(ValueError, match="low<="):
        ohlcv.import_bars(conn, [broken], source="dukascopy")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 0


def test_import_bars_rejects_non_finite_values(tmp_path):
    """NaN/inf は有限値検証で拒否される。"""
    conn = _conn(tmp_path)
    broken = ROW[:3] + (float("nan"),) + ROW[4:]
    with pytest.raises(ValueError, match="finite"):
        ohlcv.import_bars(conn, [broken], source="dukascopy")


def test_import_bars_rejects_empty_source(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="source"):
        ohlcv.import_bars(conn, [ROW], source="")


def test_import_bars_batch_rejects_whole_input_on_late_bad_row(tmp_path):
    """F4: 後半に不正な行を混ぜると、例外が出て先行する正常行も未挿入
    (投入前検証を全行に対して先に行うため — トランザクション開始前の
    reject)。"""
    conn = _conn(tmp_path)
    good = ROW
    bad = ROW[:3] + (999.0,) + ROW[4:]  # OHLC 順序違反
    with pytest.raises(ValueError):
        ohlcv.import_bars(conn, [good, bad], source="dukascopy")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 0


def test_import_bars_rolls_back_on_mid_batch_write_failure(tmp_path):
    """F4: 検証を通った後、書き込みの途中で (想定外の) 例外が起きても
    SAVEPOINT による明示ロールバックで部分挿入が残らないこと。

    以前の実装はここを明示トランザクションで囲んでおらず、例外後に呼ばれる
    "後続の無関係な commit" (ここでは次のテストの `_conn` が別 DB を触るだけ
    なので実際には無関係だが、同一 DB で他の関数が commit すれば) で部分
    挿入が永続化されてしまっていた (sqlite3 の暗黙トランザクションは接続を
    またいで残る)。ここでは 2 行目の INSERT で例外を注入する委譲プロキシで
    再現する。
    """
    from agentic_fx.store import ohlcv as ohlcv_module

    conn = _conn(tmp_path)
    row2 = ("USDJPY", "5m", NOW_ISO, 1.0, 2.0, 0.5, 1.5, 10.0, None)

    class _FailingConn:
        def __init__(self, real):
            self._real = real
            self._insert_count = 0

        def execute(self, sql, *args, **kwargs):
            if str(sql).strip().startswith("INSERT OR IGNORE INTO ohlcv "):
                self._insert_count += 1
                if self._insert_count == 2:
                    raise sqlite3.OperationalError("injected failure")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with pytest.raises(sqlite3.OperationalError):
        ohlcv_module.import_bars(_FailingConn(conn), [ROW, row2],
                                 source="dukascopy")

    # 1 行目 (ROW) は正常に INSERT されていたはずだが、SAVEPOINT の
    # ROLLBACK でどちらも残っていないこと
    assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 0

    # 別の (無関係な) commit がこの接続で走っても、rollback 済みの部分挿入が
    # 復活しないこと (かつて「無関係な commit で永続化」した問題の再現確認)
    conn.execute("INSERT OR IGNORE INTO ohlcv (symbol, interval, bar_time, "
                "open, high, low, close, volume, source, spread) "
                "VALUES ('EURUSD','1h','2026-07-22T13:00:00+00:00',"
                "1,2,0.5,1.5,10,'dukascopy',NULL)")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 1
    assert conn.execute(
        "SELECT symbol FROM ohlcv").fetchone()[0] == "EURUSD"
