import sqlite3

import pytest

from agentic_fx.store.db import TABLE_NAMES, connect, init_db

EXPECTED = {
    "ohlcv", "missions", "trade_intents", "orders", "reflections",
    "account_snapshots", "improvement_backlog", "improvement_runs",
    "econ_events", "approval_requests", "news_sources", "backtest_runs",
}


def test_init_creates_all_12_tables(tmp_path):
    conn = connect(tmp_path / "agentic.db")
    init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()
    assert {r["name"] for r in rows} == EXPECTED
    assert TABLE_NAMES == frozenset(EXPECTED)


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


def test_init_db_migrates_legacy_ohlcv_to_v2(tmp_path):
    """旧 PK (symbol,interval,bar_time) の ohlcv が source 込み PK に再構築される。"""
    conn = connect(tmp_path / "legacy.db")
    conn.execute(
        "CREATE TABLE ohlcv (symbol TEXT NOT NULL, interval TEXT NOT NULL, "
        "bar_time TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, "
        "low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0, "
        "PRIMARY KEY (symbol, interval, bar_time))")
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100)")
    conn.commit()

    init_db(conn)  # ここで再構築 migration が走る

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv)")}
    assert {"source", "spread"} <= cols
    row = conn.execute("SELECT * FROM ohlcv").fetchone()
    assert row["source"] == "yfinance" and row["spread"] is None
    # F9: 列順反転 (open/low 入替等) の変異を検出するため OHLCV 値を個別に
    # 検証する (元行: 1,2,0.5,1.5,100 は全列が異なる値なので入替が露呈する)
    assert row["open"] == 1
    assert row["high"] == 2
    assert row["low"] == 0.5
    assert row["close"] == 1.5
    assert row["volume"] == 100
    # PK が source を含む: 同キー別 source が共存できる
    conn.execute("INSERT INTO ohlcv (symbol,interval,bar_time,open,high,low,"
                 "close,volume,source) VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,0,'dukascopy')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 2


def test_init_db_ohlcv_migration_is_idempotent(tmp_path):
    """v2 スキーマの DB に init_db を複数回流しても再構築が起きない。"""
    conn = connect(tmp_path / "v2.db")
    init_db(conn)
    init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv)")}
    assert {"source", "spread"} <= cols
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='ohlcv_v1'"
    ).fetchone()[0] == 0


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

    # 再実行で成功する
    init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv)")}
    assert {"source", "spread"} <= cols
    row = conn.execute("SELECT source, spread FROM ohlcv").fetchone()
    assert row["source"] == "yfinance" and row["spread"] is None


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

    init_db(conn)  # ohlcv_v1 が残っていても再開して片付く

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv_v1" not in names
    assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 1
    row = conn.execute("SELECT * FROM ohlcv").fetchone()
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
    な rebuild を実行しないこと。2 接続が同時に v1 判定した状況を、同じ
    関数を連続で呼ぶことで再現する (2 回目 = 別接続が先に完了させていた
    想定)。"""
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "race.db")
    init_db(conn)  # 1 回目の移行 (正常)
    # 別 source の行を追加。redundant rebuild が起きて INSERT..SELECT が
    # source='yfinance' に決め打ちで上書きすれば、この行の由来情報が消える
    conn.execute("INSERT INTO ohlcv (symbol,interval,bar_time,open,high,low,"
                 "close,volume,source) VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,0,'dukascopy')")
    conn.commit()

    class _SpyConn:
        """RENAME が実際に発行されたかを数える委譲プロキシ。"""

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

    db_module._backup_before_migration(conn)

    assert bak.read_text() == "sentinel"  # 上書きされていない


def test_migration_skips_backup_for_inmemory_db():
    """F8: パス解決できない (in-memory) 接続はバックアップをスキップして
    クラッシュしないこと。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_legacy_v1_ddl())
    conn.commit()

    init_db(conn)  # backup 対象パスが無いのでスキップされ、移行は成功する

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv)")}
    assert "source" in cols
