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


# round2 D4 是正 (検収 acceptance-round2.md D4): #8 是正 (idea_norm 列の
# ALTER+backfill) を旧スキーマ (idea_norm 列なし・既存行あり) から
# `init_db` を通す経路で pin するテストが 0 本だった
# (`tests/loops/test_improve_loop_tx1_selection.py` は新スキーマへ
# idea_norm 込みで直接 INSERT する fixture のみ)。台帳の backfill 実測は
# 手動 probe であって committed test ではなかった。

def test_idea_norm_backfill_normalizes_existing_rows_and_keeps_dedup_consistent(
        tmp_path):
    """旧スキーマ (`idea_norm` 列なし) の `improvement_backlog` に既存行を
    直接 INSERT しておき、`init_db` (ALTER+backfill) を通した後:
    ①既存行の `idea_norm` が Python 正規形 (`idea.strip().lower()`) で
    埋まっていること (SQL の `lower(trim())` なら 'improve x\n' /
    'improve Ä' になるところが Python 正規形になっていることを区別する)
    ②backfill された旧行と、新規追加された行 (`backlog_store.add` — 同じ
    Python 正規化を使う) が同じ `idea_norm` 基準で dedup 照合に当たる
    (旧行・新行の整合) ことを確認する。"""
    import sqlite3

    from agentic_fx.store import backlog as backlog_store
    from agentic_fx.store.db import connect, init_db

    db_path = tmp_path / "t.db"
    # 旧スキーマ: idea_norm 列を持たない `improvement_backlog` を直接作成
    # (db.py の CREATE TABLE IF NOT EXISTS の DDL から idea_norm/attempts/
    # last_result を除いたもの — 移植元は _ensure_column 導入前の実物)。
    legacy = sqlite3.connect(str(db_path))
    legacy.execute("""
        CREATE TABLE improvement_backlog (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          idea TEXT NOT NULL,
          source TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    legacy.execute(
        "INSERT INTO improvement_backlog (idea, source, status, created_at, "
        "updated_at) VALUES (?,?,?,?,?)",
        ("IMPROVE X\n", "user", "open", "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:00+00:00"))
    legacy.execute(
        "INSERT INTO improvement_backlog (idea, source, status, created_at, "
        "updated_at) VALUES (?,?,?,?,?)",
        ("IMPROVE Ä", "user", "open", "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:00+00:00"))
    legacy.commit()
    legacy.close()

    conn = connect(db_path)
    init_db(conn)  # ALTER TABLE ... ADD COLUMN idea_norm + backfill

    rows = {r["idea"]: r["idea_norm"] for r in conn.execute(
        "SELECT idea, idea_norm FROM improvement_backlog ORDER BY id")}
    # ① backfill は Python 正規形 (strip().lower()) — SQL の
    # lower(trim()) なら 'improve x\n' / 'improve ä' になるところが
    # 'improve x' / 'improve ä' (末尾改行が消え、Ä が全角小文字化)。
    assert rows["IMPROVE X\n"] == "improve x\n".strip()
    assert rows["IMPROVE X\n"] == "improve x"
    assert rows["IMPROVE Ä"] == "improve ä"

    # ② dedup 整合: 新規追加 (backlog_store.add、同じ Python 正規化) した
    # 行の idea_norm で `idea_norm=?` 照合すると、旧 (backfill された) 行が
    # 見つかる (新規行と同じ基準で当たる = 旧行・新行の整合)。
    from datetime import datetime, timezone
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    backlog_store.add(conn, "improve x", "agent", now)  # 大文字化前の同一idea
    hit = conn.execute(
        "SELECT id, idea FROM improvement_backlog WHERE idea_norm=? "
        "ORDER BY id", ("improve x",)).fetchall()
    assert len(hit) == 2  # 旧行 (backfill) + 新規行の両方が同じ基準で当たる
    assert {r["idea"] for r in hit} == {"IMPROVE X\n", "improve x"}
    conn.close()
