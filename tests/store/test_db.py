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
