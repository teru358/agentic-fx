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
