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


def test_table_names_include_candidate_archives():
    from agentic_fx.store.db import TABLE_NAMES

    assert TABLE_NAMES == frozenset({
        "ohlcv_cache", "ohlcv_history", "missions", "trade_intents", "orders",
        "reflections", "account_snapshots", "improvement_backlog",
        "improvement_runs", "econ_events", "approval_requests", "news_sources",
        "backtest_runs", "analysis_runs", "signals", "reflection_attempts",
        "alert_state", "improve_waves", "improve_wave_slots",
        "plugin_switch_journal",
        "candidate_archives",
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

    # round2 最終是正 A7 (2026-08-29、verified-local-round2.md A7):
    # `WHERE idea_norm IS NULL` → `WHERE 1=1` の変異は backfill が冪等
    # (idea_norm が既に埋まっている行は再計算しても同じ値) なので観測点に
    # 差が出ない。既存 idea_norm を手で壊した状態で 2 回目の init_db
    # (= 再起動相当) を通し、上書きされないことを直接固定する
    # (O-3b: init_db 2 回起動の冪等 pin も同時に閉じる)。
    row = conn.execute(
        "SELECT id FROM improvement_backlog WHERE idea='IMPROVE X\n'"
    ).fetchone()
    conn.execute(
        "UPDATE improvement_backlog SET idea_norm='sentinel' WHERE id=?",
        (row["id"],))
    conn.commit()
    init_db(conn)  # 2 回目の起動 (冪等 pin も兼ねる)
    assert conn.execute(
        "SELECT idea_norm FROM improvement_backlog WHERE id=?",
        (row["id"],)).fetchone()["idea_norm"] == "sentinel"  # 既存値を上書きしない
    conn.close()


def test_trade_intents_old_check_without_signal_is_rebuilt(tmp_path):
    """段 0 (2026-09-05) M5: 列は揃っているが CHECK の許可カテゴリに 'signal'
    が無い旧 DDL の DB でも init_db が trade_intents を rebuild し、既存行
    (action / reject_category) を保持する。列の有無だけ見る冪等ガードに
    戻す変異が生存していた。"""
    import sqlite3

    from agentic_fx.store.db import connect, init_db

    db_path = tmp_path / "t.db"
    conn = connect(db_path)
    init_db(conn)
    old_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='trade_intents'").fetchone()["sql"]
    assert "'signal'" in old_sql
    conn.close()

    raw = sqlite3.connect(db_path)
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.executescript(
        "INSERT INTO missions (id,loop,runner,model,status,started_at) VALUES "
        "(1,'trade','local','m','completed','2026-01-01T00:00:00+00:00');\n"
        "DROP TABLE trade_intents;\n"
        + old_sql.replace("'execution','signal'", "'execution'")
        + ";\n"
        "INSERT INTO trade_intents (id,mission_id,payload_json,action,gate_result,"
        "reject_reason,reject_category,created_at) VALUES "
        "(1,1,'{}','open','rejected','r','execution','2026-01-01T00:00:00+00:00');")
    raw.commit()
    stale = raw.execute(
        "SELECT sql FROM sqlite_master WHERE name='trade_intents'").fetchone()[0]
    assert "'signal'" not in stale
    raw.close()

    conn = connect(db_path)
    init_db(conn)
    new_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='trade_intents'").fetchone()["sql"]
    assert "'signal'" in new_sql
    row = conn.execute(
        "SELECT action, reject_category FROM trade_intents WHERE id=1").fetchone()
    assert (row["action"], row["reject_category"]) == ("open", "execution")
    conn.execute(
        "INSERT INTO trade_intents (mission_id,payload_json,action,gate_result,"
        "reject_reason,reject_category,created_at) VALUES "
        "(1,'{}','open','rejected','r','signal','2026-01-01T00:00:00+00:00')")


# 段階 2 (base_interval/params_json 導入) の migration pin ------------------


def test_backtest_runs_base_interval_and_params_json_migration_backfills_existing_rows(
        tmp_path):
    """旧スキーマ (`base_interval`/`params_json` 列なし) の `backtest_runs`
    に既存行を直接 INSERT しておき、`init_db` (ALTER TABLE) を通した後、
    既存行の `base_interval` が '1m'、`params_json` が '{}' で埋まっている
    ことを確認する (A6 設計: 許容値 CHECK は付けない — dataset が唯一の
    validator)。"""
    import sqlite3

    from agentic_fx.store.db import connect, init_db

    db_path = tmp_path / "t.db"
    legacy = sqlite3.connect(str(db_path))
    legacy.execute("""
        CREATE TABLE backtest_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          plugin_ref TEXT NOT NULL, content_hash TEXT NOT NULL,
          kind TEXT NOT NULL, pair TEXT NOT NULL, timeframe TEXT NOT NULL,
          source TEXT NOT NULL, period_start TEXT NOT NULL,
          period_end TEXT NOT NULL,
          scope TEXT NOT NULL, issued_by TEXT NOT NULL,
          metrics_json TEXT NOT NULL, settings_hash TEXT NOT NULL,
          core_commit TEXT NOT NULL, initial_balance REAL NOT NULL,
          created_at TEXT NOT NULL
        )
    """)
    legacy.execute(
        "INSERT INTO backtest_runs (plugin_ref, content_hash, kind, pair, "
        "timeframe, source, period_start, period_end, scope, issued_by, "
        "metrics_json, settings_hash, core_commit, initial_balance, "
        "created_at) VALUES ('p','h','strategy','USDJPY','1h','dukascopy',"
        "'2026-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00',"
        "'in_sample','harness','{}','s','c',1,'2026-01-01T00:00:00+00:00')")
    legacy.commit()
    legacy.close()

    conn = connect(db_path)
    init_db(conn)
    row = conn.execute(
        "SELECT base_interval, params_json FROM backtest_runs WHERE id=1"
    ).fetchone()
    assert row["base_interval"] == "1m"
    assert row["params_json"] == "{}"


def test_backtest_runs_view_columns_include_base_interval(tmp_path):
    from agentic_fx.store.backtest_runs import _VIEW_COLUMNS

    assert "base_interval" in _VIEW_COLUMNS
    # params_json (段階 2 の追加) は agent 公開面 (view) を汚さない設計
    # 決定 (A6) — VIEW_COLUMNS に含めない。
    assert "params_json" not in _VIEW_COLUMNS
    assert "params" not in _VIEW_COLUMNS


def test_backtest_runs_outcome_migration_preserves_rows_and_is_idempotent(tmp_path):
    """旧 schema の既存行は NULL として残り、init_db の再実行も安全。"""
    import sqlite3

    from agentic_fx.store.db import connect, init_db

    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute("""
        CREATE TABLE backtest_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          plugin_ref TEXT NOT NULL, content_hash TEXT NOT NULL,
          kind TEXT NOT NULL, pair TEXT NOT NULL, timeframe TEXT NOT NULL,
          source TEXT NOT NULL, base_interval TEXT NOT NULL DEFAULT '1m',
          params_json TEXT NOT NULL DEFAULT '{}', period_start TEXT NOT NULL,
          period_end TEXT NOT NULL, scope TEXT NOT NULL, issued_by TEXT NOT NULL,
          metrics_json TEXT NOT NULL, settings_hash TEXT NOT NULL,
          core_commit TEXT NOT NULL, initial_balance REAL NOT NULL,
          created_at TEXT NOT NULL, variant TEXT NOT NULL DEFAULT 'candidate',
          ref_plugin_ref TEXT, ref_content_hash TEXT, mission_id INTEGER
        )
    """)
    legacy.execute(
        "INSERT INTO backtest_runs (plugin_ref,content_hash,kind,pair,timeframe,"
        "source,period_start,period_end,scope,issued_by,metrics_json,settings_hash,"
        "core_commit,initial_balance,created_at) VALUES "
        "('p','h','strategy','USDJPY','1h','test','a','b','in_sample','harness',"
        "'{}','s','c',1,'now')")
    legacy.commit()
    legacy.close()

    migrated = connect(db_path)
    init_db(migrated)
    init_db(migrated)
    row = migrated.execute(
        "SELECT mission_outcome FROM backtest_runs WHERE id=1").fetchone()
    assert row["mission_outcome"] is None
    assert migrated.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='candidate_archives'").fetchone() is not None
    # 段 0 pin (2026-09-10): migration 経路で作った candidate_archives も
    # (mission_id, artifact_hash) UNIQUE を持つ (fresh DDL と同じ来歴の冪等性)。
    from agentic_fx.store import candidate_archives
    from datetime import datetime, timezone
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    kw = dict(mission_id=1, name="n", content_hash="c", artifact_hash="a",
              archive_path=None, pair="USDJPY", metrics={}, now=now, commit=True)
    first = candidate_archives.insert(migrated, **kw)
    assert candidate_archives.insert(migrated, **kw) == first
    assert len(candidate_archives.list_by_mission(migrated, 1)) == 1


def test_fresh_and_migrated_backtest_runs_have_same_columns(tmp_path):
    import sqlite3

    from agentic_fx.store.db import connect, init_db

    fresh = connect(tmp_path / "fresh.db")
    init_db(fresh)
    legacy_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(legacy_path)
    legacy.execute("""
        CREATE TABLE backtest_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          plugin_ref TEXT NOT NULL, content_hash TEXT NOT NULL,
          kind TEXT NOT NULL, pair TEXT NOT NULL, timeframe TEXT NOT NULL,
          source TEXT NOT NULL, base_interval TEXT NOT NULL DEFAULT '1m',
          params_json TEXT NOT NULL DEFAULT '{}', period_start TEXT NOT NULL,
          period_end TEXT NOT NULL,
          scope TEXT NOT NULL CHECK(scope IN ('in_sample','holdout_gate','human_custom')),
          issued_by TEXT NOT NULL CHECK(issued_by IN ('harness','human_cli')),
          metrics_json TEXT NOT NULL, settings_hash TEXT NOT NULL,
          core_commit TEXT NOT NULL, initial_balance REAL NOT NULL,
          created_at TEXT NOT NULL, mission_id INTEGER,
          variant TEXT NOT NULL DEFAULT 'candidate'
            CHECK(variant IN ('candidate','baseline','no_strategy')),
          ref_plugin_ref TEXT, ref_content_hash TEXT
        )
    """)
    legacy.commit()
    legacy.close()
    migrated = connect(legacy_path)
    init_db(migrated)

    def shape(conn):
        return [(r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"])
                for r in conn.execute("PRAGMA table_info(backtest_runs)")]

    assert shape(migrated) == shape(fresh)
