import sqlite3

import pytest

from agentic_fx.store.db import TABLE_NAMES, connect, connect_readonly, init_db

EXPECTED = {
    "ohlcv_cache", "ohlcv_history", "missions", "trade_intents", "orders",
    "reflections", "account_snapshots", "improvement_backlog",
    "improvement_runs", "econ_events", "approval_requests", "news_sources",
    "backtest_runs", "analysis_runs", "signals",
}


def test_init_creates_all_15_tables(tmp_path):
    conn = connect(tmp_path / "agentic.db")
    init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()
    assert {r["name"] for r in rows} == EXPECTED
    assert TABLE_NAMES == frozenset(EXPECTED)


def test_init_creates_account_snapshots_ts_id_index(tmp_path):
    """Task 12 Fix Round 1 (ユーザー裁定): snapshots.latest() の
    ``ORDER BY ts DESC, id DESC LIMIT 1`` が無索引だと全表スキャンになり
    tick 数に対して劣化する (実測 O(n^2) — cProfile で latest() が実行
    時間の 92%)。インデックスの存在そのものをピンする (init_db 再実行
    でも冪等に効くこと — ``test_init_is_idempotent`` の対象と同じ conn)。
    """
    conn = connect(tmp_path / "agentic.db")
    init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='ix_account_snapshots_ts_id'").fetchall()
    assert len(rows) == 1


def test_init_is_idempotent(tmp_path):
    conn = connect(tmp_path / "agentic.db")
    init_db(conn)
    init_db(conn)  # 二回目でも例外なし


def test_foreign_keys_enabled(tmp_path):
    conn = connect(tmp_path / "agentic.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_orders_has_lifecycle_columns(tmp_path):
    conn = connect(tmp_path / "agentic.db")
    init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
    assert {"client_order_id", "quantity", "filled_quantity",
            "remaining_quantity", "avg_fill_price", "broker_order_id",
            "broker_position_id", "broker_synced_at", "horizon",
            "status"} <= cols


def test_connection_usable_across_threads(tmp_path):
    import threading
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    errors = []

    def use():
        try:
            conn.execute("SELECT 1").fetchone()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=use)
    t.start()
    t.join()
    assert errors == []


def test_init_db_adds_trigger_column_to_legacy_missions_table(tmp_path):
    """旧スキーマの DB に init_db を流すと trigger 列が追加される。"""
    from agentic_fx.store import missions
    from datetime import datetime, timezone

    NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    p = tmp_path / "legacy.db"
    conn = connect(p)
    # trigger 列を持たない旧 missions テーブルを手で作る
    conn.execute("CREATE TABLE missions ("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT, loop TEXT NOT NULL, "
                 "runner TEXT NOT NULL, model TEXT NOT NULL, "
                 "status TEXT NOT NULL DEFAULT 'running', "
                 "output_json TEXT, transcript_json TEXT, "
                 "started_at TEXT NOT NULL, finished_at TEXT)")
    conn.execute("INSERT INTO missions (loop, runner, model, started_at) "
                 "VALUES ('trade','local','m','2026-07-22T12:00:00+00:00')")
    conn.commit()

    init_db(conn)   # ここで ALTER が走る

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(missions)")}
    assert "trigger" in cols
    assert conn.execute("SELECT trigger FROM missions").fetchone()[0] is None
    # 追加後に新しい start() が通ること
    mid = missions.start(conn, "trade", "local", "m", NOW, trigger="cron")
    assert conn.execute("SELECT trigger FROM missions WHERE id=?",
                        (mid,)).fetchone()[0] == "cron"


def test_connect_pragmas_busy_timeout(tmp_path):
    """I3: Verify PRAGMA busy_timeout=5000 is set (mutation kills if removed)."""
    conn = connect(tmp_path / "pragmas.db")
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == 5000


def test_connect_pragmas_journal_mode(tmp_path):
    """I3: Verify PRAGMA journal_mode=WAL is set."""
    conn = connect(tmp_path / "pragmas.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_init_db_migrates_legacy_v1_ohlcv_into_cache_table(tmp_path):
    """旧 v1 PK (symbol,interval,bar_time) の ohlcv が、v2 (source 込み PK)
    を経て ohlcv_cache (source='yfinance' 合成) へ収束する。"""
    conn = connect(tmp_path / "legacy.db")
    conn.execute(
        "CREATE TABLE ohlcv (symbol TEXT NOT NULL, interval TEXT NOT NULL, "
        "bar_time TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, "
        "low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0, "
        "PRIMARY KEY (symbol, interval, bar_time))")
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100)")
    conn.commit()

    init_db(conn)  # v1 → v2 → split が連鎖して走る

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv" not in names and "ohlcv_v1" not in names
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv_cache)")}
    assert "source" in cols and "spread" not in cols
    row = conn.execute("SELECT * FROM ohlcv_cache").fetchone()
    assert row["source"] == "yfinance"
    assert row["open"] == 1
    assert row["high"] == 2
    assert row["low"] == 0.5
    assert row["close"] == 1.5
    assert row["volume"] == 100


def test_init_db_ohlcv_migration_is_idempotent(tmp_path):
    """新規 DB (v1 ohlcv が最初から無い) に init_db を複数回流しても
    split/v2 migration が何もしないこと。"""
    conn = connect(tmp_path / "v2.db")
    init_db(conn)
    init_db(conn)
    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv" not in names and "ohlcv_v1" not in names
    assert "ohlcv_cache" in names and "ohlcv_history" in names


def test_ohlcv_migration_rolls_back_on_failure_and_resumes(tmp_path):
    """INSERT..SELECT 中の失敗は ROLLBACK され、再実行で成功する (再開可能性)。"""
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "legacy2.db")
    conn.execute(
        "CREATE TABLE ohlcv (symbol TEXT NOT NULL, interval TEXT NOT NULL, "
        "bar_time TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, "
        "low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0, "
        "PRIMARY KEY (symbol, interval, bar_time))")
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100)")
    conn.commit()

    class _FailingConn:
        """sqlite3.Connection は immutable type でメソッドを直接パッチできない
        (`cannot set 'execute' attribute of immutable type`) ため、実接続への
        委譲プロキシで INSERT INTO ohlcv だけ落とす。トランザクションは実接続
        (conn) 側で進むので ROLLBACK 後の状態は conn で確認できる。"""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            if str(sql).strip().startswith("INSERT OR IGNORE INTO ohlcv "):
                raise sqlite3.OperationalError("injected failure")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with pytest.raises(sqlite3.OperationalError):
        db_module._migrate_ohlcv_v2(_FailingConn(conn))

    # ROLLBACK 済み: v1 のまま (ohlcv_v1 は無い。RENAME 前に例外が起きた形に
    # 見えるよう、rebuild は commit まで全て 1 トランザクション内)
    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv_v1" not in names
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv)")}
    assert "source" not in cols
    assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 1

    # 再実行で成功する (v1 → v2 → split の連鎖が走り、ohlcv_cache に収束する)
    init_db(conn)
    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv" not in names
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv_cache)")}
    assert "source" in cols
    row = conn.execute("SELECT source FROM ohlcv_cache").fetchone()
    assert row["source"] == "yfinance"


def _legacy_v1_ddl() -> str:
    return (
        "CREATE TABLE ohlcv (symbol TEXT NOT NULL, interval TEXT NOT NULL, "
        "bar_time TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, "
        "low REAL NOT NULL, close REAL NOT NULL, "
        "volume REAL NOT NULL DEFAULT 0, "
        "PRIMARY KEY (symbol, interval, bar_time))")


def db_v2_ddl_for_test():
    return (
        "CREATE TABLE ohlcv ("
        "symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time TEXT NOT NULL, "
        "open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, "
        "close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0, "
        "source TEXT NOT NULL DEFAULT 'yfinance', spread REAL, "
        "PRIMARY KEY (symbol, interval, bar_time, source))"
    )


def test_ohlcv_migration_resumes_from_stale_ohlcv_v1(tmp_path):
    """v2 の ohlcv と ohlcv_v1 (v1 形式のまま) が両方残った (RENAME 後 DROP 前
    に失敗した) 状態から再開できる — INSERT..SELECT からやり直す。

    ohlcv_v1 は常に `ALTER TABLE ohlcv RENAME` で作られる (RENAME 元は
    その瞬間の ohlcv = v1 形式) ため、ここでは v1 形式 (source/spread 列
    なし、8 列) で再現する。v2 側には「クラッシュ前に完了していた
    INSERT..SELECT」を模して同じ値の行を先に入れておく (一致するので
    F3 の衝突検出には引っかからない)。
    """
    conn = connect(tmp_path / "resume.db")
    conn.execute(_legacy_v1_ddl())
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100)")
    conn.commit()
    conn.execute("ALTER TABLE ohlcv RENAME TO ohlcv_v1")
    conn.executescript(db_v2_ddl_for_test())
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100,'yfinance',NULL)")
    conn.commit()

    init_db(conn)  # ohlcv_v1 が残っていても再開して片付く (v2 → split も連鎖)

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv_v1" not in names and "ohlcv" not in names
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 1
    row = conn.execute("SELECT * FROM ohlcv_cache").fetchone()
    assert (row["open"], row["high"], row["low"], row["close"],
           row["volume"]) == (1, 2, 0.5, 1.5, 100)


def test_ohlcv_migration_resume_raises_on_value_conflict(tmp_path):
    """F3: 再開時に ohlcv_v1 と ohlcv (v2) の同一キー行の値が食い違うなら、
    ohlcv_v1 を DROP せず例外で停止する (黙って破棄しない)。"""
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "conflict.db")
    conn.execute(_legacy_v1_ddl())
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100)")
    conn.commit()
    conn.execute("ALTER TABLE ohlcv RENAME TO ohlcv_v1")
    conn.executescript(db_v2_ddl_for_test())
    # v2 側に食い違う値 (open=999) が既に入っている — 破損 / 想定外書き込みを模す
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',999,2,0.5,1.5,100,'yfinance',NULL)")
    conn.commit()

    with pytest.raises(RuntimeError, match="一致しない"):
        db_module._migrate_ohlcv_v2(conn)

    # 例外時は ROLLBACK される (DROP 未実施) ので ohlcv_v1 は温存される
    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv_v1" in names
    # v2 側の (食い違ったままの) データも変更されていない
    row = conn.execute(
        "SELECT open FROM ohlcv WHERE source='yfinance'").fetchone()
    assert row["open"] == 999


def test_migrate_ohlcv_v2_is_noop_when_already_migrated_by_another_connection(
        tmp_path):
    """F2: BEGIN IMMEDIATE 取得後に「既に完全移行済み」を再検証し、redundant
    な rebuild を実行しないこと。"""
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "race.db")
    conn.execute(_legacy_v1_ddl())
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100)")
    conn.commit()
    db_module._migrate_ohlcv_v2(conn)  # 1 回目の移行 (正常) — v2 単一テーブル止まり
    conn.execute("INSERT INTO ohlcv (symbol,interval,bar_time,open,high,low,"
                 "close,volume,source) VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,0,'dukascopy')")
    conn.commit()

    class _SpyConn:
        def __init__(self, real):
            self._real = real
            self.rename_calls = 0

        def execute(self, sql, *args, **kwargs):
            if str(sql).strip().startswith("ALTER TABLE ohlcv RENAME"):
                self.rename_calls += 1
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    spy = _SpyConn(conn)
    db_module._migrate_ohlcv_v2(spy)  # 2 回目 (別接続が先に済ませていた想定)

    assert spy.rename_calls == 0  # F2: 再検証で何もせず抜けている
    row = conn.execute(
        "SELECT open FROM ohlcv WHERE source='dukascopy'").fetchone()
    assert row is not None and row["open"] == 1


def test_migration_creates_backup_before_rebuild(tmp_path):
    """F8: 実 DB (ファイル) への移行前に自動でバックアップを作る。"""
    p = tmp_path / "legacy.db"
    conn = connect(p)
    conn.execute(_legacy_v1_ddl())
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100)")
    conn.commit()

    init_db(conn)

    bak = p.parent / (p.name + ".bak-ohlcv-v2")
    assert bak.exists()
    bak_conn = sqlite3.connect(bak)
    bak_conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in bak_conn.execute("PRAGMA table_info(ohlcv)")}
        assert "source" not in cols  # バックアップは移行前 (v1) のスキーマ
        row = bak_conn.execute("SELECT * FROM ohlcv").fetchone()
        assert row["open"] == 1
    finally:
        bak_conn.close()


def test_migration_backup_not_overwritten_on_retry(tmp_path):
    """再試行 (途中失敗からの再開) で既存のバックアップを上書きしない。"""
    from agentic_fx.store import db as db_module

    p = tmp_path / "legacy.db"
    bak = p.parent / (p.name + ".bak-ohlcv-v2")
    bak.write_text("sentinel")  # 既存バックアップの目印
    conn = connect(p)
    conn.execute(_legacy_v1_ddl())
    conn.commit()

    db_module._backup_before_migration(conn, ".bak-ohlcv-v2")

    assert bak.read_text() == "sentinel"  # 上書きされていない


def test_migration_skips_backup_for_inmemory_db():
    """F8: パス解決できない (in-memory) 接続はバックアップをスキップして
    クラッシュしないこと。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_legacy_v1_ddl())
    conn.commit()

    init_db(conn)  # backup 対象パスが無いのでスキップされ、移行は成功する

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv" not in names
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv_cache)")}
    assert "source" in cols


def test_connect_rejects_old_sqlite_version(tmp_path, monkeypatch):
    """SQLite < 3.35 の場合、RETURNING 句が使えないため接続時に
    RuntimeError で即座に fail-fast する (signals.py の使用要件)。"""
    import agentic_fx.store.db as db_mod

    monkeypatch.setattr(db_mod.sqlite3, "sqlite_version_info", (3, 34, 1))
    with pytest.raises(RuntimeError, match="3.35"):
        db_mod.connect(tmp_path / "x.db")


@pytest.mark.parametrize("filename", [
    "question?.db",
    "hash#.db",
    "percent%.db",
    "space name.db",
])
def test_connect_readonly_with_special_chars_in_filename(tmp_path, filename):
    """URI 内のパス部分を percent-encode する必要があるテスト (codex I-1)。
    `?` / `#` / `%` / 空白を含むファイル名で正しい DB を開くことを確認。

    各特殊文字について、RW 接続でセットアップ後に RO 接続で検証する。
    WAL locking 問題を回避するため個別のテストデータを使う。"""
    db_path = tmp_path / filename

    # RW 接続でセットアップ
    conn_rw = connect(db_path)
    init_db(conn_rw)
    # テーブル exists を確認してからデータ挿入
    tables = {r[0] for r in conn_rw.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()}
    assert "missions" in tables, f"missions table not created for {filename}"

    label = f"test_{filename}"  # ファイル名を label に含めてトレース容易に
    conn_rw.execute(
        "INSERT INTO missions (loop, runner, model, started_at) "
        "VALUES (?, ?, ?, ?)",
        (label, "test", "model", "2026-07-22T12:00:00+00:00"))
    conn_rw.commit()
    conn_rw.close()

    # RO 接続で読み取り確認
    conn_ro = connect_readonly(db_path)
    tables_ro = {r[0] for r in conn_ro.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()}
    assert "missions" in tables_ro, \
        f"RO connection opened wrong DB for {filename} (missions table not found)"

    row = conn_ro.execute(
        "SELECT loop FROM missions WHERE loop=?", (label,)).fetchone()
    conn_ro.close()

    # 正しいテーブルが読めたことを確認 (別の DB を開いていないことを保証)
    assert row is not None, \
        f"Failed to read from {filename}: got None (opened wrong DB?)"
    assert row["loop"] == label

def test_migrate_ohlcv_split_routes_unknown_source_to_history(tmp_path):
    """設計書 §12 移行方針: 未知 source は履歴側へ隔離する (fail-safe —
    履歴側は削除されないため、判断を誤っても失われない)。"""
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "unknown.db")
    conn.executescript(db_v2_ddl_for_test())
    conn.executescript(db_module._OHLCV_CACHE_DDL +
                       db_module._OHLCV_HISTORY_DDL)
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100,"
                 "'some-future-vendor',NULL)")
    conn.commit()

    db_module._migrate_ohlcv_split(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM ohlcv_history WHERE source='some-future-vendor'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM ohlcv_cache WHERE source='some-future-vendor'"
    ).fetchone()[0] == 0


def test_migrate_ohlcv_split_routes_live_and_import_sources_correctly(tmp_path):
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "mixed.db")
    conn.executescript(db_v2_ddl_for_test())
    conn.executescript(db_module._OHLCV_CACHE_DDL +
                       db_module._OHLCV_HISTORY_DDL)
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100,'yfinance',NULL)")
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:01:00+00:00',1,2,0.5,1.5,100,'mt5-live',NULL)")
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:02:00+00:00',1,2,0.5,1.5,100,'dukascopy',0.01)")
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:03:00+00:00',1,2,0.5,1.5,100,'mt5',NULL)")
    conn.commit()

    db_module._migrate_ohlcv_split(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM ohlcv_cache WHERE source IN ('dukascopy','mt5')"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM ohlcv_history WHERE source IN "
        "('yfinance','mt5-live')").fetchone()[0] == 0


def test_migrate_ohlcv_split_is_noop_when_ohlcv_table_absent(tmp_path):
    """フレッシュ DB (init_db 直後) には `ohlcv` が存在しない — 直接呼んでも
    何もせず正常終了する。"""
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "fresh.db")
    init_db(conn)
    db_module._migrate_ohlcv_split(conn)  # 例外なく即座に戻る
    assert conn.execute(
        "SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 0


def test_migrate_ohlcv_split_raises_on_value_conflict(tmp_path):
    """F3 相当: 分割先に既に食い違う値の行がある場合は例外で停止し、
    `ohlcv` を温存する (黙って破棄しない)。"""
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "split_conflict.db")
    conn.executescript(db_v2_ddl_for_test())
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100,'yfinance',NULL)")
    conn.commit()
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS ohlcv_cache ("
        "symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time TEXT NOT NULL, "
        "open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, "
        "close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0, "
        "source TEXT NOT NULL, "
        "PRIMARY KEY (symbol, interval, bar_time, source));")
    conn.execute("INSERT INTO ohlcv_cache VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',999,2,0.5,1.5,100,'yfinance')")
    conn.commit()

    with pytest.raises(RuntimeError, match="一致しない"):
        db_module._migrate_ohlcv_split(conn)

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv" in names  # 温存されている


def test_migrate_ohlcv_split_raises_on_history_spread_conflict(tmp_path):
    """履歴側の衝突検証は `spread` も見る。

    OHLCV 5 列だけを比較すると、`spread` だけ食い違う行が「一致」と判定され、
    旧 `ohlcv` が DROP されて旧 spread が無警告で失われる。spread はバック
    テストのコスト計算に効くので、他の列と同じ扱い (不一致なら温存) にする。
    """
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "split_spread_conflict.db")
    conn.executescript(db_v2_ddl_for_test())
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100,'dukascopy',1.1)")
    conn.commit()
    conn.executescript(db_module._OHLCV_HISTORY_DDL)
    # OHLCV 5 列は完全一致・spread だけ違う (再実行で別データが入った状況)
    conn.execute("INSERT INTO ohlcv_history VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100,'dukascopy',9.9)")
    conn.commit()

    with pytest.raises(RuntimeError, match="一致しない"):
        db_module._migrate_ohlcv_split(conn)

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv" in names  # 温存されている


def test_migrate_ohlcv_split_accepts_matching_null_spread_on_rerun(tmp_path):
    """spread が両側とも NULL なら一致として扱う (NULL 同士で誤検出しない)。

    上のテストだけだと「spread を比較しない」→「常に不一致とみなす」への
    退行を区別できない。
    """
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "split_spread_null.db")
    conn.executescript(db_v2_ddl_for_test())
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100,'dukascopy',NULL)")
    conn.commit()
    conn.executescript(db_module._OHLCV_HISTORY_DDL)
    conn.execute("INSERT INTO ohlcv_history VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100,'dukascopy',NULL)")
    conn.commit()

    db_module._migrate_ohlcv_split(conn)

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv" not in names  # 一致したので移行完了
