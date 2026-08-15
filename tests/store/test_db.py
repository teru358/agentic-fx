import sqlite3
from datetime import datetime, timezone

import pytest

from agentic_fx.store.db import TABLE_NAMES, connect, connect_readonly, init_db

EXPECTED = {
    "ohlcv_cache", "ohlcv_history", "missions", "trade_intents", "orders",
    "reflections", "account_snapshots", "improvement_backlog",
    "improvement_runs", "econ_events", "approval_requests", "news_sources",
    "backtest_runs", "analysis_runs", "signals", "reflection_attempts",
}


def test_init_creates_all_16_tables(tmp_path):
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


def test_init_creates_ohlcv_cache_bar_time_index(tmp_path):
    """プラン 9 束 C: prune_cache の `WHERE bar_time < ?` は PK
    (symbol, interval, bar_time, source) の先頭列に当たらないため、索引が
    無いと autoindex のカバリングスキャンになる。定常状態では cutoff を
    跨ぐ行が少なく LIMIT に届かないので、毎 tick (60s、core_lock 保持下)
    索引全体を走り切る (実測 20 万行 8.8ms / 100 万行 35.9ms → 索引後
    0.0055ms)。インデックスの存在そのものをピンする (既存 DB へは
    init_db の executescript が無条件に適用する)。
    """
    conn = connect(tmp_path / "agentic.db")
    init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='ix_ohlcv_cache_bar_time'").fetchall()
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


def test_migrate_ohlcv_split_rolls_back_partial_writes_on_conflict(tmp_path):
    """衝突で中断したとき、分割先への部分書き込みも巻き戻る。

    既存の衝突テストは `ohlcv` が温存されることしか見ておらず、
    `except` の `conn.rollback()` を削除する変異が生き残る。巻き戻らないと
    接続に未コミットの部分行が残り、後続の commit で確定してしまう。
    """
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "split_rollback.db")
    conn.executescript(db_v2_ddl_for_test())
    # 1 行目は衝突しない (=分割先へ書かれる)、2 行目が衝突して例外を起こす
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100,'dukascopy',NULL)")
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:01:00+00:00',1,2,0.5,1.5,100,'yfinance',NULL)")
    conn.commit()
    conn.executescript(db_module._OHLCV_CACHE_DDL + db_module._OHLCV_HISTORY_DDL)
    conn.execute("INSERT INTO ohlcv_cache VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:01:00+00:00',999,2,0.5,1.5,100,'yfinance')")
    conn.commit()

    with pytest.raises(RuntimeError, match="一致しない"):
        db_module._migrate_ohlcv_split(conn)

    # 衝突しなかった dukascopy 行が history へ残っていない = 巻き戻っている
    n = conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0]
    assert n == 0


def test_migrate_ohlcv_split_backup_uses_split_suffix(tmp_path):
    """分割 migration のバックアップ名は `.bak-ohlcv-split`。

    衝突時のエラーメッセージが復元先としてこの名前を案内しているので、
    suffix がずれると案内が実在しないファイルを指す。v1→v2 移行の
    `.bak-ohlcv-v2` とは別物なので、分割側は分割側で固定する。
    """
    from agentic_fx.store import db as db_module

    db = tmp_path / "split_backup.db"
    conn = connect(db)
    conn.executescript(db_v2_ddl_for_test())
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100,'yfinance',NULL)")
    conn.commit()
    conn.executescript(db_module._OHLCV_CACHE_DDL + db_module._OHLCV_HISTORY_DDL)

    db_module._migrate_ohlcv_split(conn)

    assert (tmp_path / "split_backup.db.bak-ohlcv-split").exists()


def _legacy_signals_ddl() -> str:
    """FK 追加前 (Task 13 以前) の signals DDL。claimed_by_mission_id に
    REFERENCES が無い。"""
    return (
        "CREATE TABLE signals ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "plugin TEXT NOT NULL, content_hash TEXT NOT NULL,"
        "pair TEXT NOT NULL, timeframe TEXT NOT NULL, bar_ts TEXT NOT NULL,"
        "kind TEXT NOT NULL, payload_json TEXT NOT NULL,"
        "status TEXT NOT NULL DEFAULT 'pending'"
        "  CHECK(status IN ('pending','claimed','consumed','abandoned')),"
        "claimed_by_mission_id INTEGER, claimed_at TEXT,"
        "requeue_count INTEGER NOT NULL DEFAULT 0,"
        "created_at TEXT NOT NULL,"
        "UNIQUE(plugin, content_hash, pair, timeframe, bar_ts))")


def _legacy_signals_and_missions_conn(tmp_path):
    """レガシー (FK 無し) signals + 標準スキーマの missions 等を持つ接続を
    返す (Task 13 の repair 単体テスト用)。`_SCHEMA` は signals を
    `CREATE TABLE IF NOT EXISTS` で作るため、先に手動でレガシー signals を
    作っておけば `executescript(_SCHEMA)` はそれを温存したまま missions 等
    他テーブルだけを作る。"""
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_signals_ddl())
    conn.commit()
    conn.executescript(db_module._SCHEMA)
    return conn


def _insert_legacy_signal(conn, *, content_hash, status,
                          claimed_by_mission_id, claimed_at):
    conn.execute(
        "INSERT INTO signals (plugin, content_hash, pair, timeframe, "
        "bar_ts, kind, payload_json, status, claimed_by_mission_id, "
        "claimed_at, requeue_count, created_at) VALUES "
        "('p', ?, 'USDJPY', '1h', '2026-08-03T12:00:00+00:00', 'signal', "
        "'{}', ?, ?, ?, 0, '2026-08-03T11:00:00+00:00')",
        (content_hash, status, claimed_by_mission_id, claimed_at))
    conn.commit()


# --- 受入条件: FK 自体 ------------------------------------------------

def test_init_db_fresh_signals_table_has_missions_fk(tmp_path):
    conn = connect(tmp_path / "fresh.db")
    init_db(conn)
    fks = conn.execute("PRAGMA foreign_key_list(signals)").fetchall()
    assert any(fk["table"] == "missions"
              and fk["from"] == "claimed_by_mission_id" for fk in fks)


def test_signals_fk_rejects_nonexistent_mission_id_after_migration(tmp_path):
    conn = connect(tmp_path / "fresh.db")
    init_db(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO signals (plugin, content_hash, pair, timeframe, "
            "bar_ts, kind, payload_json, status, claimed_by_mission_id, "
            "claimed_at, requeue_count, created_at) VALUES "
            "('p','h','USDJPY','1h','2026-08-03T12:00:00+00:00','signal',"
            "'{}','claimed', 12345, '2026-08-03T12:00:00+00:00', 0, "
            "'2026-08-03T11:00:00+00:00')")


# --- 受入条件: legacy DB からの移行 (エンドツーエンド) -------------------

def test_init_db_migrates_legacy_signals_to_v2(tmp_path):
    """旧 (FK 無し) signals を持つ DB に init_db を流すと FK 付きへ
    再構築され、宙吊り行 (missions 行が無い claimed) が修復されること。"""
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_signals_ddl())
    conn.execute(
        "INSERT INTO signals (plugin, content_hash, pair, timeframe, "
        "bar_ts, kind, payload_json, status, claimed_by_mission_id, "
        "claimed_at, requeue_count, created_at) VALUES "
        "('p','h','USDJPY','1h','2026-08-03T12:00:00+00:00','signal','{}',"
        "'claimed', 999, '2026-08-03T11:30:00+00:00', 0, "
        "'2026-08-03T11:00:00+00:00')")
    conn.commit()

    init_db(conn)  # missions は空のまま作られるので 999 は宙吊り

    row = conn.execute(
        "SELECT status, claimed_by_mission_id, claimed_at "
        "FROM signals").fetchone()
    assert row["status"] == "pending"
    assert row["claimed_by_mission_id"] is None
    assert row["claimed_at"] is None
    fks = conn.execute("PRAGMA foreign_key_list(signals)").fetchall()
    assert any(fk["table"] == "missions" for fk in fks)


def test_signals_migration_is_idempotent(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_signals_ddl())
    conn.execute(
        "INSERT INTO signals (plugin, content_hash, pair, timeframe, "
        "bar_ts, kind, payload_json, status, requeue_count, created_at) "
        "VALUES ('p','h','USDJPY','1h','2026-08-03T12:00:00+00:00','signal',"
        "'{}','pending', 0, '2026-08-03T11:00:00+00:00')")
    conn.commit()

    init_db(conn)
    init_db(conn)  # 2 回目でも例外なし

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "signals_v1" not in names
    assert conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"] == 1


# --- 受入条件: status ごとの修復規則 (D5、1 検査目的 1 テスト) -----------

def test_migrate_signals_fk_repairs_dangling_claimed_row(tmp_path):
    """D5: status='claimed' かつ参照先 missions 行が無い → pending に戻し
    claimed_by_mission_id/claimed_at を NULL にする。"""
    from agentic_fx.store import db as db_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    _insert_legacy_signal(conn, content_hash="dangling_claimed",
                          status="claimed", claimed_by_mission_id=999,
                          claimed_at="2026-08-03T11:30:00+00:00")

    db_module._migrate_signals_fk(conn)

    row = conn.execute(
        "SELECT status, claimed_by_mission_id, claimed_at FROM signals "
        "WHERE content_hash='dangling_claimed'").fetchone()
    assert row["status"] == "pending"
    assert row["claimed_by_mission_id"] is None
    assert row["claimed_at"] is None


def test_migrate_signals_fk_leaves_claimed_row_with_valid_mission_untouched(
        tmp_path):
    """claimed かつ参照先 missions 行が実在する場合は無変更 — repair が
    dangling 行だけに当たり、有効な claim の監査情報を壊さないことの
    ピン (D5 の変異リストには無いが、"claimed の修復を落とす" の逆方向の
    検査として必要)。"""
    from agentic_fx.store import db as db_module
    from agentic_fx.store import missions

    conn = _legacy_signals_and_missions_conn(tmp_path)
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    mid = missions.start(conn, "trade", "local", "m", now)
    _insert_legacy_signal(conn, content_hash="valid_claimed",
                          status="claimed", claimed_by_mission_id=mid,
                          claimed_at="2026-08-03T11:30:00+00:00")

    db_module._migrate_signals_fk(conn)

    row = conn.execute(
        "SELECT status, claimed_by_mission_id, claimed_at FROM signals "
        "WHERE content_hash='valid_claimed'").fetchone()
    assert row["status"] == "claimed"
    assert row["claimed_by_mission_id"] == mid
    assert row["claimed_at"] == "2026-08-03T11:30:00+00:00"


def test_migrate_signals_fk_repairs_dangling_consumed_row_without_reviving(
        tmp_path):
    """D5: status='consumed' かつ dangling → claimed_by_mission_id のみ
    NULL、status は 'consumed' のまま (終端状態を蘇らせない)。claimed_at は
    温存する (F3: レビュー 1 周目・muse 指摘 — claimed_at の CASE から
    `status='claimed' AND` を削る変異は終端状態の claimed_at まで NULL 化
    してしまうため、元の値のまま残ることを直接ピンする)。"""
    from agentic_fx.store import db as db_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    _insert_legacy_signal(conn, content_hash="dangling_consumed",
                          status="consumed", claimed_by_mission_id=999,
                          claimed_at="2026-08-03T11:30:00+00:00")

    db_module._migrate_signals_fk(conn)

    row = conn.execute(
        "SELECT status, claimed_by_mission_id, claimed_at FROM signals "
        "WHERE content_hash='dangling_consumed'").fetchone()
    assert row["status"] == "consumed"  # pending に戻さない
    assert row["claimed_by_mission_id"] is None
    assert row["claimed_at"] == "2026-08-03T11:30:00+00:00"  # 温存 (F3)


def test_migrate_signals_fk_repairs_dangling_abandoned_row_without_reviving(
        tmp_path):
    """D5: status='abandoned' かつ dangling → claimed_by_mission_id のみ
    NULL、status は 'abandoned' のまま。claimed_at は温存する (F3、上と
    同型の pin)。"""
    from agentic_fx.store import db as db_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    _insert_legacy_signal(conn, content_hash="dangling_abandoned",
                          status="abandoned", claimed_by_mission_id=999,
                          claimed_at="2026-08-03T11:30:00+00:00")

    db_module._migrate_signals_fk(conn)

    row = conn.execute(
        "SELECT status, claimed_by_mission_id, claimed_at FROM signals "
        "WHERE content_hash='dangling_abandoned'").fetchone()
    assert row["status"] == "abandoned"
    assert row["claimed_by_mission_id"] is None
    assert row["claimed_at"] == "2026-08-03T11:30:00+00:00"  # 温存 (F3)


def test_migrate_signals_fk_repairs_claimed_row_with_null_owner(tmp_path):
    """F1 (レビュー 1 周目 Important): status='claimed' だが
    claimed_by_mission_id が既に NULL の旧スキーマ行 (所有者無しの claimed)
    も pending に戻し claimed_at も NULL にする。旧 CASE 条件は
    `claimed_by_mission_id IS NOT NULL AND ... NOT IN (...)` を要求して
    おり、owner が NULL のこの行を素通りさせていた — 素通りすると
    `recover_interrupted` は owner で引くため拾えず、claimed_at も NULL の
    行は `reclaim_expired` (`datetime(claimed_at)` で選ぶ) でも回収されず
    永久滞留する (claimed_at 非 NULL なら lease 満了で回収はされる)。

    missions テーブルに 1 行実在させておく — `NOT IN (SELECT id FROM
    missions)` のサブクエリが空だと `NULL NOT IN ()` が SQL の空リスト
    特例で vacuous-true になり、`IS NULL OR` を落とす変異でも本テストが
    誤って green になってしまう (実測で確認)。非空サブクエリなら `NULL
    NOT IN (非空集合)` は unknown (偽扱い) になるため、`IS NULL OR` が
    無いと条件全体が偽になり変異を正しく red にできる。
    """
    from agentic_fx.store import db as db_module
    from agentic_fx.store import missions as missions_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    missions_module.start(conn, "trade", "local", "m", now)
    _insert_legacy_signal(conn, content_hash="null_owner_claimed",
                          status="claimed", claimed_by_mission_id=None,
                          claimed_at="2026-08-03T11:30:00+00:00")

    db_module._migrate_signals_fk(conn)

    row = conn.execute(
        "SELECT status, claimed_by_mission_id, claimed_at FROM signals "
        "WHERE content_hash='null_owner_claimed'").fetchone()
    assert row["status"] == "pending"
    assert row["claimed_by_mission_id"] is None
    assert row["claimed_at"] is None


def test_migrate_signals_fk_leaves_pending_row_unchanged(tmp_path):
    """D5: status='pending' (claimed_by_mission_id は元々 NULL) → 無変更。"""
    from agentic_fx.store import db as db_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    _insert_legacy_signal(conn, content_hash="already_pending",
                          status="pending", claimed_by_mission_id=None,
                          claimed_at=None)

    db_module._migrate_signals_fk(conn)

    row = conn.execute(
        "SELECT status, claimed_by_mission_id, claimed_at FROM signals "
        "WHERE content_hash='already_pending'").fetchone()
    assert row["status"] == "pending"
    assert row["claimed_by_mission_id"] is None
    assert row["claimed_at"] is None


def test_migrate_signals_fk_leaves_foreign_key_check_clean(tmp_path):
    """移行後は PRAGMA foreign_key_check(signals) が空であること (D5 の
    受入条件の直接ピン)。"""
    from agentic_fx.store import db as db_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    _insert_legacy_signal(conn, content_hash="dangling",
                          status="claimed", claimed_by_mission_id=999,
                          claimed_at="2026-08-03T11:30:00+00:00")

    db_module._migrate_signals_fk(conn)

    violations = conn.execute("PRAGMA foreign_key_check(signals)").fetchall()
    assert violations == []


# --- 受入条件: UNIQUE / CHECK / id / sqlite_sequence の温存 ------------

def test_signals_migration_preserves_unique_constraint(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_signals_ddl())
    conn.execute(
        "INSERT INTO signals (plugin, content_hash, pair, timeframe, "
        "bar_ts, kind, payload_json, status, requeue_count, created_at) "
        "VALUES ('p','h','USDJPY','1h','2026-08-03T12:00:00+00:00','signal',"
        "'{}','pending', 0, '2026-08-03T11:00:00+00:00')")
    conn.commit()

    init_db(conn)

    from agentic_fx.store import signals as signals_module
    dup = signals_module.add(
        conn, plugin="p", content_hash="h", pair="USDJPY", timeframe="1h",
        bar_ts="2026-08-03T12:00:00+00:00", kind="signal", payload={"x": 2},
        now=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc))
    assert dup is None  # UNIQUE(plugin,content_hash,pair,timeframe,bar_ts)


def test_signals_migration_preserves_status_check(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_signals_ddl())
    conn.commit()

    init_db(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO signals (plugin, content_hash, pair, timeframe, "
            "bar_ts, kind, payload_json, status, requeue_count, created_at) "
            "VALUES ('p','h','USDJPY','1h','2026-08-03T12:00:00+00:00',"
            "'signal','{}','bogus_status', 0, '2026-08-03T11:00:00+00:00')")


def test_signals_migration_preserves_ids_and_autoincrement_sequence(
        tmp_path):
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_signals_ddl())
    conn.execute(
        "INSERT INTO signals (id, plugin, content_hash, pair, timeframe, "
        "bar_ts, kind, payload_json, status, claimed_by_mission_id, "
        "claimed_at, requeue_count, created_at) VALUES "
        "(7,'p','h','USDJPY','1h','2026-08-03T12:00:00+00:00','signal','{}',"
        "'pending', NULL, NULL, 0, '2026-08-03T11:00:00+00:00')")
    conn.commit()

    init_db(conn)

    row = conn.execute("SELECT id FROM signals").fetchone()
    assert row["id"] == 7  # id は書き換えられない

    from agentic_fx.store import signals as signals_module
    new_id = signals_module.add(
        conn, plugin="p2", content_hash="h2", pair="USDJPY", timeframe="1h",
        bar_ts="2026-08-03T13:00:00+00:00", kind="signal", payload={},
        now=datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc))
    assert new_id > 7  # AUTOINCREMENT シーケンスが id=7 を踏まえて続く


# --- PRAGMA foreign_keys の復元 (BEGIN 前後のトグル順序のピン) ---------

def test_migrate_signals_fk_restores_foreign_keys_pragma_after_failure(
        tmp_path):
    """SQLite はトランザクション開始後の `PRAGMA foreign_keys` 変更を
    無視する (実測確認済み)。migration は BEGIN の**外側**で OFF にし、
    成功・失敗を問わず ON に戻さねばならない — 戻し忘れると以降の
    プロセス全体で FK 保護が無効になる。INSERT を意図的に失敗させ、
    例外後も `PRAGMA foreign_keys` が 1 (ON) であることを確認する。"""
    from agentic_fx.store import db as db_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    _insert_legacy_signal(conn, content_hash="x", status="pending",
                          claimed_by_mission_id=None, claimed_at=None)

    class _FailingConn:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            if str(sql).strip().startswith("INSERT OR IGNORE INTO signals "):
                raise sqlite3.OperationalError("injected failure")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with pytest.raises(sqlite3.OperationalError):
        db_module._migrate_signals_fk(_FailingConn(conn))

    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_signals_fk_disables_foreign_keys_before_begin(tmp_path):
    """順序そのものを観測し、BEGIN 後へ OFF を移す変異を殺す。"""
    from agentic_fx.store import db as db_module
    real = _legacy_signals_and_missions_conn(tmp_path)

    class _OrderConn:
        def __init__(self, conn):
            self._conn, self.events = conn, []
        def execute(self, sql, *args, **kwargs):
            normalized = " ".join(str(sql).split()).upper()
            if normalized in {"PRAGMA FOREIGN_KEYS=OFF", "BEGIN IMMEDIATE"}:
                self.events.append(normalized)
            return self._conn.execute(sql, *args, **kwargs)
        def __getattr__(self, name):
            return getattr(self._conn, name)

    observed = _OrderConn(real)
    db_module._migrate_signals_fk(observed)
    assert observed.events.index("PRAGMA FOREIGN_KEYS=OFF") < \
           observed.events.index("BEGIN IMMEDIATE")


def test_migrate_signals_fk_restores_foreign_keys_pragma_after_success(
        tmp_path):
    """成功経路でも PRAGMA foreign_keys が ON に戻ること。

    **着手前検証で追加。** 失敗経路だけを見る `..._after_failure` では、
    `finally` を `except BaseException` に変えて「成功時は OFF のまま」に
    する変異が `tests/` 全 1946 件 green で生存する (実測)。戻し忘れると、
    既存 DB から起動した以降のプロセス全体で FK 保護が無効になり、Task 13
    が追加した防御そのものが機能しなくなる (実測: bogus mission_id での
    INSERT が通ってしまう)。fresh DB では `_migrate_signals_fk` が early
    return して pragma に触らないため、legacy 経路で見る必要がある。
    """
    from agentic_fx.store import db as db_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    _insert_legacy_signal(conn, content_hash="x", status="pending",
                          claimed_by_mission_id=None, claimed_at=None)

    db_module._migrate_signals_fk(conn)

    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    # pragma が実際に効いていることまで見る (値だけでは強制の有無は分からない)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO signals (plugin, content_hash, pair, timeframe, "
            "bar_ts, kind, payload_json, status, claimed_by_mission_id, "
            "claimed_at, requeue_count, created_at) VALUES "
            "('p','after','USDJPY','1h','2026-08-03T12:00:00+00:00','signal',"
            "'{}','claimed', 424242, '2026-08-03T12:00:00+00:00', 0, "
            "'2026-08-03T11:00:00+00:00')")


def test_migrate_signals_fk_resumes_after_crash_between_rename_and_copy(
        tmp_path):
    """RENAME + CREATE の直後にクラッシュした DB から再開できること。

    **着手前検証で追加。** signals は FK 付きで空、signals_v1 に行が残って
    いる状態。冪等ガードを `if fk_present: return` に弱める変異は、この形が
    無いと `tests/` 全 1946 件 green で生存し、再開時に全行を失う (実測:
    signals 0 行 + signals_v1 が孤児として残存)。
    `test_signals_migration_is_idempotent` はクリーンな legacy DB 上でしか
    回らず、どちらのガード形でも 2 周目に early return するため殺せない。
    """
    from agentic_fx.store import db as db_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    _insert_legacy_signal(conn, content_hash="survivor", status="pending",
                          claimed_by_mission_id=None, claimed_at=None)
    # クラッシュ状態を再現: RENAME 済み + 新表 CREATE 済み + コピー前
    conn.execute("ALTER TABLE signals RENAME TO signals_v1")
    conn.execute(db_module._SIGNALS_V2_DDL)
    conn.commit()

    db_module._migrate_signals_fk(conn)

    rows = conn.execute("SELECT content_hash FROM signals").fetchall()
    assert [r["content_hash"] for r in rows] == ["survivor"]
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "signals_v1" not in names


# --- 移行前バックアップ (他 2 つの rebuild と規約統一) -------------------

def test_migrate_signals_fk_backup_uses_signals_fk_suffix(tmp_path):
    """signals FK migration のバックアップ名は `.bak-signals-fk`。

    **着手前検証で追加 (指揮者裁定)。** `db.py` の他 2 つの table rebuild
    (`_migrate_ohlcv_v2` = `.bak-ohlcv-v2` / `_migrate_ohlcv_split` =
    `.bak-ohlcv-split`) と同じ規約で、移行の種類ごとに別ファイルへ退避する。
    suffix がずれると別移行のバックアップを上書きしかねない。
    `test_migrate_ohlcv_split_backup_uses_split_suffix` と同型の pin。
    """
    from agentic_fx.store import db as db_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    _insert_legacy_signal(conn, content_hash="x", status="pending",
                          claimed_by_mission_id=None, claimed_at=None)

    db_module._migrate_signals_fk(conn)

    assert (tmp_path / "legacy.db.bak-signals-fk").exists()


def test_migrate_signals_fk_skips_backup_when_already_migrated(tmp_path):
    """冪等ガードで抜ける通常起動ではバックアップを取らないこと。

    **着手前検証で追加 (指揮者裁定)。** backup 呼び出しの位置がガードより
    前へずれると、毎起動で DB 全体のコピーコストが乗る。
    """
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "fresh.db")
    init_db(conn)          # fresh は _SCHEMA 側で FK 付き → 移行不要
    assert not (tmp_path / "fresh.db.bak-signals-fk").exists()


def _legacy_improvement_runs_ddl() -> str:
    """pr_url 列を持つ旧 (Task 19 以前) improvement_runs DDL。"""
    return (
        "CREATE TABLE improvement_runs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "backlog_id INTEGER REFERENCES improvement_backlog(id),"
        "result TEXT,"
        "pr_url TEXT, approval_id INTEGER, report_path TEXT,"
        "started_at TEXT NOT NULL, finished_at TEXT)")


def _legacy_improvement_runs_conn(tmp_path):
    """レガシー (pr_url 付き) improvement_runs + 標準スキーマの他テーブルを
    持つ接続を返す。

    **着手前検証 B2 で修正。** `connect()` は `PRAGMA foreign_keys=ON` を
    張るため、参照先 `improvement_backlog` を先に用意しないと旧表への
    INSERT が `no such table: main.improvement_backlog` で落ちる (実測)。
    `_SCHEMA` は `CREATE TABLE IF NOT EXISTS` なので、先に作ったレガシー
    `improvement_runs` は温存したまま他テーブルだけが揃う。Task 13 の
    `_legacy_signals_and_missions_conn` と同じ形。
    """
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_improvement_runs_ddl())
    conn.commit()
    conn.executescript(db_module._SCHEMA)
    return conn


def test_init_db_migrates_legacy_improvement_runs_drops_pr_url(tmp_path):
    """旧 PR 経路の名残 (pr_url 列) が落ち、result の値域が CHECK で
    固定される (D7)。"""
    conn = _legacy_improvement_runs_conn(tmp_path)
    conn.execute(
        "INSERT INTO improvement_runs (result, report_path, started_at, "
        "finished_at) VALUES ('report', 'reports/x.md', "
        "'2026-08-11T09:00:00+00:00', '2026-08-11T09:05:00+00:00')")
    conn.commit()

    init_db(conn)

    cols = {r["name"] for r in
           conn.execute("PRAGMA table_info(improvement_runs)")}
    assert "pr_url" not in cols
    row = conn.execute(
        "SELECT result, report_path FROM improvement_runs").fetchone()
    assert row["result"] == "report"
    assert row["report_path"] == "reports/x.md"


def test_improvement_runs_migration_is_idempotent(tmp_path):
    conn = _legacy_improvement_runs_conn(tmp_path)
    conn.execute(
        "INSERT INTO improvement_runs (result, started_at) VALUES "
        "('report', '2026-08-11T09:00:00+00:00')")
    conn.commit()

    init_db(conn)
    init_db(conn)  # 2 回目でも例外なし

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "improvement_runs_v1" not in names
    assert conn.execute(
        "SELECT COUNT(*) c FROM improvement_runs").fetchone()["c"] == 1


def test_improvement_runs_migration_aborts_on_legacy_pr_result_with_null_pr_url(
        tmp_path):
    """codex I6: result='pr', pr_url=NULL は現スキーマで合法 (result に
    CHECK が無いため) — pr_url だけ見るガードはこの行を見逃す。result 側
    の検査が要ることの直接ピン。"""
    conn = _legacy_improvement_runs_conn(tmp_path)
    conn.execute(
        "INSERT INTO improvement_runs (result, pr_url, started_at) "
        "VALUES ('pr', NULL, '2026-08-11T09:00:00+00:00')")
    conn.commit()

    with pytest.raises(RuntimeError, match="旧 PR 経路"):
        init_db(conn)

    # 中断時は旧テーブルのまま (pr_url 列が残っている) — 部分適用しない。
    # **この assert はガードの実行「位置」については何も保証しない**
    # (rollback が RENAME を巻き戻すため — 着手前検証 B3 で実測)。位置は
    # test_improvement_runs_guard_runs_before_any_table_rebuild が守る。
    cols = {r["name"] for r in
           conn.execute("PRAGMA table_info(improvement_runs)")}
    assert "pr_url" in cols


def test_improvement_runs_migration_aborts_on_nonnull_pr_url_with_other_result(
        tmp_path):
    """result が 'pr' でなくても pr_url が非NULLなら中断する
    (result='pr' OR pr_url IS NOT NULL の OR のもう半分)。"""
    conn = _legacy_improvement_runs_conn(tmp_path)
    conn.execute(
        "INSERT INTO improvement_runs (result, pr_url, started_at) "
        "VALUES ('report', 'https://example/pr/1', "
        "'2026-08-11T09:00:00+00:00')")
    conn.commit()

    with pytest.raises(RuntimeError, match="旧 PR 経路"):
        init_db(conn)


def test_improvement_runs_migration_error_names_offending_row_ids(tmp_path):
    """「黙って捨てない」は人間が該当行を見つけられて初めて意味を持つ —
    エラーメッセージに対象 id を含めることを直接ピンする。"""
    conn = _legacy_improvement_runs_conn(tmp_path)
    conn.execute(
        "INSERT INTO improvement_runs (result, started_at) VALUES "
        "('pr', '2026-08-11T09:00:00+00:00')")
    conn.commit()
    bad_id = conn.execute(
        "SELECT id FROM improvement_runs").fetchone()["id"]

    with pytest.raises(RuntimeError, match=f"id={bad_id}"):
        init_db(conn)


def test_improvement_runs_migration_error_names_all_offending_row_ids(
        tmp_path):
    """F4 (Minor, KAT+muse 一致): bad 行が複数あるとき、エラーメッセージに
    **両方の** id が含まれること。列挙を先頭 1 件に潰す変異
    (`bad_rows[0]` 相当) の killer。"""
    conn = _legacy_improvement_runs_conn(tmp_path)
    conn.execute(
        "INSERT INTO improvement_runs (id, result, started_at) VALUES "
        "(1, 'pr', '2026-08-11T09:00:00+00:00')")
    conn.execute(
        "INSERT INTO improvement_runs "
        "(id, result, pr_url, started_at) VALUES "
        "(2, 'report', 'https://example.invalid/pr/2', "
        "'2026-08-11T09:00:00+00:00')")
    conn.commit()

    with pytest.raises(RuntimeError) as excinfo:
        init_db(conn)

    assert "id=1, 2" in str(excinfo.value)


def test_improvement_runs_check_rejects_invalid_result_after_migration(
        tmp_path):
    conn = connect(tmp_path / "fresh.db")
    init_db(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO improvement_runs (result, started_at) VALUES "
            "('pr', '2026-08-11T09:00:00+00:00')")


def test_improvement_runs_check_allows_null_result_for_unfinished_run(
        tmp_path):
    """CHECK(result IN (...)) は NULL を拒否しない (SQLite の CHECK は
    NULL に対して常に通過する) — start() 直後 (未 finish) の行が新スキーマ
    でも作れることのピン。NOT NULL や `result IS NOT NULL` を書き足す
    誤実装だけがこれを壊す。"""
    from agentic_fx.store import improve_runs

    conn = connect(tmp_path / "fresh.db")
    init_db(conn)
    rid = improve_runs.start(conn, None, datetime(2026, 8, 11, 9, 0,
                                                   tzinfo=timezone.utc))
    row = conn.execute(
        "SELECT result FROM improvement_runs WHERE id=?", (rid,)).fetchone()
    assert row["result"] is None


def test_improvement_runs_guard_runs_before_any_table_rebuild(tmp_path):
    """ガードは RENAME/CREATE より前に実行されること (**着手前検証 B3 で追加**)。

    中断後の状態検査ではこの順序を観測できない — migration 全体が rollback に
    包まれているため、ガードを RENAME の後へ移す変異が
    `assert "pr_url" in cols` を素通りすることを実測で確認済み。実行された
    SQL を直接観測して順序そのものを固定する。
    """
    conn = _legacy_improvement_runs_conn(tmp_path)
    conn.execute(
        "INSERT INTO improvement_runs (result, started_at) VALUES "
        "('pr', '2026-08-11T09:00:00+00:00')")
    conn.commit()

    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        with pytest.raises(RuntimeError, match="旧 PR 経路"):
            init_db(conn)
    finally:
        conn.set_trace_callback(None)

    assert not any("RENAME TO improvement_runs_v1" in s for s in seen), \
        f"ガードより前に RENAME が実行された: {seen}"


def test_improvement_runs_migration_preserves_each_rows_result(tmp_path):
    """コピーが result 列を行ごとに保存すること (定数で潰さない)。
    **着手前検証で追加** — 単一行のテストでは列を定数に潰す変異が生存する。"""
    conn = _legacy_improvement_runs_conn(tmp_path)
    conn.execute(
        "INSERT INTO improvement_runs (id, result, started_at) VALUES "
        "(1, 'report', '2026-08-11T09:00:00+00:00')")
    conn.execute(
        "INSERT INTO improvement_runs (id, result, started_at) VALUES "
        "(2, 'approval', '2026-08-11T09:00:00+00:00')")
    conn.commit()

    init_db(conn)

    rows = {r["id"]: r["result"] for r in
            conn.execute("SELECT id, result FROM improvement_runs")}
    assert rows == {1: "report", 2: "approval"}


def test_schema_improvement_runs_definition_has_no_pr_url():
    """`_SCHEMA` 側の inline 定義も新 DDL に置き換わっていること
    (**着手前検証で追加**)。

    migration が後から作り直すため振る舞いでは差が出ず、`_SCHEMA` だけ旧定義に
    戻す変異 (Step 5 の半分を実施しない) が生存する。定義そのものを検査する。
    """
    from agentic_fx.store import db as db_module

    assert "pr_url" not in db_module._SCHEMA
    assert db_module._IMPROVEMENT_RUNS_V2_DDL in db_module._SCHEMA


def test_improvement_runs_migration_aborts_on_leftover_v1_table(tmp_path):
    """中断痕跡 `improvement_runs_v1` が残っていたら表名を挙げて中断する
    (**着手前検証 M1 で追加**)。

    残存表の行は新スキーマへコピーされていない可能性があり、黙って早期
    return すると取り残した行を見捨てる経路になる (変異ノート #7 の理念に
    反する)。「移行済みだから何もしない」で済ませないことを直接ピンする。
    """
    conn = _legacy_improvement_runs_conn(tmp_path)
    conn.execute(
        "INSERT INTO improvement_runs (result, started_at) VALUES "
        "('report', '2026-08-11T09:00:00+00:00')")
    conn.commit()
    init_db(conn)   # 正常に移行済みの状態にする

    # 前回の移行が中断した痕跡を人為的に作る
    conn.execute(
        "CREATE TABLE improvement_runs_v1 "
        "(id INTEGER PRIMARY KEY, result TEXT)")
    conn.commit()

    with pytest.raises(RuntimeError, match="improvement_runs_v1"):
        init_db(conn)


def test_migrate_improvement_runs_backup_uses_improvement_runs_suffix(tmp_path):
    """バックアップ名は `.bak-improvement-runs-v2` (**着手前検証 B4 で追加**)。

    `db.py` の他 3 つの table rebuild (`_migrate_ohlcv_v2` = `.bak-ohlcv-v2` /
    `_migrate_ohlcv_split` = `.bak-ohlcv-split` / `_migrate_signals_fk` =
    `.bak-signals-fk`) と同じ規約で、移行の種類ごとに別ファイルへ退避する。
    suffix がずれると別移行のバックアップを上書きしかねない。
    `test_migrate_signals_fk_backup_uses_signals_fk_suffix` と同型の pin。
    """
    from agentic_fx.store import db as db_module

    conn = _legacy_improvement_runs_conn(tmp_path)
    conn.execute(
        "INSERT INTO improvement_runs (result, started_at) VALUES "
        "('report', '2026-08-11T09:00:00+00:00')")
    conn.commit()

    db_module._migrate_improvement_runs_v2(conn)

    assert (tmp_path / "legacy.db.bak-improvement-runs-v2").exists()


def test_migrate_improvement_runs_skips_backup_when_already_migrated(tmp_path):
    """冪等ガードで抜ける通常起動ではバックアップを取らないこと
    (**着手前検証 B4 で追加**)。毎起動で DB 全体をコピーしては困る。"""
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "fresh.db")
    init_db(conn)   # 新規 DB — pr_url は最初から無い

    db_module._migrate_improvement_runs_v2(conn)

    assert not (tmp_path / "fresh.db.bak-improvement-runs-v2").exists()


def test_migrate_improvement_runs_restores_foreign_keys_pragma(tmp_path):
    """FK トグルを成功パスで必ず元に戻すこと (**着手前検証 B4 で追加**)。
    戻し忘れると以降のプロセス全体で FK 保護が無効になる。"""
    conn = _legacy_improvement_runs_conn(tmp_path)
    conn.execute(
        "INSERT INTO improvement_runs (result, started_at) VALUES "
        "('report', '2026-08-11T09:00:00+00:00')")
    conn.commit()

    init_db(conn)

    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_improvement_runs_copies_rows_with_dangling_backlog_id(tmp_path):
    """参照切れの backlog_id があってもコピーが FK 違反で落ちないこと
    (**着手前検証 B4 で追加**)。improvement_runs は improvement_backlog を
    参照する **子** 側なので、FK を OFF にせずコピーすると
    `IntegrityError: FOREIGN KEY constraint failed` になる (実測)。"""
    conn = _legacy_improvement_runs_conn(tmp_path)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO improvement_runs (backlog_id, result, started_at) "
        "VALUES (999, 'report', '2026-08-11T09:00:00+00:00')")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")

    init_db(conn)

    row = conn.execute(
        "SELECT backlog_id FROM improvement_runs").fetchone()
    assert row["backlog_id"] == 999


def test_init_db_fresh_creates_reflection_attempts(tmp_path):
    conn = connect(tmp_path / "fresh.db")
    init_db(conn)
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(reflection_attempts)")}
    assert cols == {"order_id", "attempts", "last_attempt_at", "last_reason"}


def test_init_db_existing_database_adds_reflection_attempts(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    conn.executescript(
        "CREATE TABLE missions (id INTEGER PRIMARY KEY, loop TEXT NOT NULL, "
        "runner TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, "
        "output_json TEXT, transcript_json TEXT, started_at TEXT NOT NULL, "
        "finished_at TEXT);"
        "CREATE TABLE trade_intents (id INTEGER PRIMARY KEY, mission_id INTEGER "
        "NOT NULL, payload_json TEXT NOT NULL, gate_result TEXT, reject_reason "
        "TEXT, created_at TEXT NOT NULL);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, intent_id INTEGER, "
        "pair TEXT NOT NULL, direction TEXT NOT NULL, entry_type TEXT NOT NULL, "
        "horizon TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL);"
    )
    init_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM reflection_attempts").fetchone()[0] == 0


def test_reflection_attempts_migration_is_idempotent(tmp_path):
    conn = connect(tmp_path / "existing.db")
    init_db(conn)
    init_db(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name='reflection_attempts'").fetchone()[0] == 1
