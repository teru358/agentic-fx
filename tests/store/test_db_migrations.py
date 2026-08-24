"""backtest_runs/analysis_runs への mission_id 列追加 (プラン10 Task10-13、
Task 12 の `WHERE mission_id=?` assert が依存する既存テーブルへの列追加)。"""
from __future__ import annotations


def test_backtest_runs_and_analysis_runs_have_mission_id_column(tmp_path):
    from agentic_fx.store.db import connect, init_db

    conn = connect(tmp_path / "t.db")
    init_db(conn)
    bt_cols = {r["name"] for r in conn.execute("PRAGMA table_info(backtest_runs)")}
    an_cols = {r["name"] for r in conn.execute("PRAGMA table_info(analysis_runs)")}
    assert "mission_id" in bt_cols
    assert "mission_id" in an_cols


def test_ensure_column_migration_is_idempotent_for_mission_id(tmp_path):
    """2 回連続で init_db を呼んでも (再起動相当) ALTER TABLE が失敗しない。"""
    from agentic_fx.store.db import connect, init_db

    conn = connect(tmp_path / "t.db")
    init_db(conn)
    init_db(conn)  # 2 回目 — ALTER TABLE で例外が出たら red
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(backtest_runs)")}
    assert "mission_id" in cols


def test_table_names_unchanged_after_mission_id_columns_added():
    """T10-13 は既存テーブルへの列追加のみであり、新テーブルは追加しない
    — TABLE_NAMES (現物 20 項目) が不変であることの pin。"""
    from agentic_fx.store.db import TABLE_NAMES

    assert TABLE_NAMES == frozenset({
        "ohlcv_cache", "ohlcv_history", "missions", "trade_intents", "orders",
        "reflections", "account_snapshots", "improvement_backlog",
        "improvement_runs", "econ_events", "approval_requests", "news_sources",
        "backtest_runs", "analysis_runs", "signals", "reflection_attempts",
        "alert_state", "improve_waves", "improve_wave_slots",
        "plugin_switch_journal",
    })
