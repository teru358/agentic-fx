import sqlite3

import pytest

from agentic_fx.store.db import TABLE_NAMES, connect, init_db

EXPECTED = {
    "ohlcv", "missions", "trade_intents", "orders", "reflections",
    "account_snapshots", "improvement_backlog", "improvement_runs",
    "econ_events", "approval_requests", "news_sources",
}


def test_init_creates_all_11_tables(tmp_path):
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
    row = conn.execute("SELECT source, spread FROM ohlcv").fetchone()
    assert row["source"] == "yfinance" and row["spread"] is None
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


def test_ohlcv_migration_resumes_from_stale_ohlcv_v1(tmp_path):
    """v2 の ohlcv と ohlcv_v1 が両方残った (RENAME 後 DROP 前に失敗した) 状態
    から再開できる — INSERT..SELECT からやり直す。"""
    conn = connect(tmp_path / "resume.db")
    init_db(conn)  # 正常な v2 スキーマを作る
    conn.execute("ALTER TABLE ohlcv RENAME TO ohlcv_v1")
    conn.executescript(db_v2_ddl_for_test())
    conn.execute("INSERT INTO ohlcv_v1 VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100,'yfinance',NULL)")
    conn.commit()

    init_db(conn)  # ohlcv_v1 が残っていても再開して片付く

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv_v1" not in names
    assert conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0] == 1


def db_v2_ddl_for_test():
    return (
        "CREATE TABLE ohlcv ("
        "symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time TEXT NOT NULL, "
        "open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, "
        "close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0, "
        "source TEXT NOT NULL DEFAULT 'yfinance', spread REAL, "
        "PRIMARY KEY (symbol, interval, bar_time, source))"
    )
