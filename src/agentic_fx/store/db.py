"""SQLite 接続 + 13 テーブルスキーマ — 設計書 §12。"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

_log = logging.getLogger("agentic_fx.store.db")

_OHLCV_V2_DDL = """
CREATE TABLE IF NOT EXISTS ohlcv (
  symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
  close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'yfinance', spread REAL,
  PRIMARY KEY (symbol, interval, bar_time, source)
);
"""

_SCHEMA = _OHLCV_V2_DDL + """
CREATE TABLE IF NOT EXISTS missions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loop TEXT NOT NULL,            -- trade | improve | ask | reflection
  runner TEXT NOT NULL, model TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  trigger TEXT,                  -- cron | signal:<plugin> (Phase 2)。loop='trade' 以外は NULL
  output_json TEXT, transcript_json TEXT,
  started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS trade_intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id INTEGER NOT NULL REFERENCES missions(id),
  payload_json TEXT NOT NULL,
  gate_result TEXT,              -- accepted | rejected (NULL = 未判定)
  reject_reason TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id INTEGER REFERENCES trade_intents(id),
  approval_id INTEGER REFERENCES approval_requests(id),
  client_order_id TEXT UNIQUE,
  pair TEXT NOT NULL, direction TEXT NOT NULL,
  entry_type TEXT NOT NULL, horizon TEXT NOT NULL,
  status TEXT NOT NULL,
  quantity REAL, filled_quantity REAL DEFAULT 0,
  remaining_quantity REAL, avg_fill_price REAL,
  requested_price REAL, close_price REAL,
  stop_loss REAL, take_profit REAL,
  fees_swap REAL DEFAULT 0, realized_pnl REAL, close_reason TEXT,
  broker_order_id TEXT, broker_position_id TEXT, broker_synced_at TEXT,
  expires_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  filled_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS reflections (
  order_id INTEGER PRIMARY KEY REFERENCES orders(id),
  content TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, balance REAL NOT NULL, equity REAL NOT NULL,
  hwm REAL NOT NULL, cashflow REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'paper'
);
-- Task 12 Fix Round 1 (ユーザー裁定): snapshots.latest() の
-- `ORDER BY ts DESC, id DESC LIMIT 1` が無索引で全表スキャンになり、tick
-- 毎に呼ばれる実運用・バックテスト双方で行数に対し劣化する (O(n^2) 実測 —
-- cProfile で latest() が実行時間の 92%)。ASC インデックスで十分 (SQLite
-- は逆方向スキャン対応のため DESC 指定は不要)。
CREATE INDEX IF NOT EXISTS ix_account_snapshots_ts_id
  ON account_snapshots(ts, id);
CREATE TABLE IF NOT EXISTS improvement_backlog (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idea TEXT NOT NULL,
  source TEXT NOT NULL,          -- user | agent | research
  status TEXT NOT NULL DEFAULT 'open',  -- open | selected | done | rejected
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS improvement_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  backlog_id INTEGER REFERENCES improvement_backlog(id),
  result TEXT,                   -- pr | approval | report
  pr_url TEXT, approval_id INTEGER, report_path TEXT,
  started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS econ_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, country TEXT NOT NULL, name TEXT NOT NULL,
  importance INTEGER NOT NULL DEFAULT 0,
  actual TEXT, forecast TEXT, previous TEXT,
  UNIQUE (ts, country, name)
);
CREATE TABLE IF NOT EXISTS approval_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,            -- tech_plugin | news_source | live_trade
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  -- pending | approved | rejected | expired | invalidated
  reason TEXT, decided_by TEXT, decided_at TEXT,
  message_id TEXT, expires_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  fetcher TEXT NOT NULL,         -- feed | web
  url TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 0,
  added_by TEXT NOT NULL,        -- user | agent
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plugin_ref TEXT NOT NULL, content_hash TEXT NOT NULL, kind TEXT NOT NULL,
  pair TEXT NOT NULL, timeframe TEXT NOT NULL, source TEXT NOT NULL,
  period_start TEXT NOT NULL, period_end TEXT NOT NULL,
  scope TEXT NOT NULL
    CHECK(scope IN ('in_sample','holdout_gate','human_custom')),
  issued_by TEXT NOT NULL CHECK(issued_by IN ('harness','human_cli')),
  metrics_json TEXT NOT NULL, settings_hash TEXT NOT NULL,
  core_commit TEXT NOT NULL, initial_balance REAL NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  params_json TEXT NOT NULL, trial_count INTEGER NOT NULL,
  source TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

TABLE_NAMES = frozenset({
    "ohlcv", "missions", "trade_intents", "orders", "reflections",
    "account_snapshots", "improvement_backlog", "improvement_runs",
    "econ_events", "approval_requests", "news_sources", "backtest_runs",
    "analysis_runs",
})


def connect(db_path: Path, *, check_same_thread: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str,
                   ddl: str) -> None:
    """存在しない列を追加する (SQLite は ADD COLUMN IF NOT EXISTS を持たない)。

    init_db は CREATE TABLE IF NOT EXISTS なので、既存 DB のテーブル定義は
    更新されない。列追加は PRAGMA で検査して ALTER する必要がある。
    """
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


_FLOAT_TOL = 1e-9


def _values_match(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < _FLOAT_TOL


def _backup_before_migration(conn: sqlite3.Connection) -> None:
    """移行が必要と判定された時だけ、移行前スナップショットを取る
    (fix round 1 F8 — コメント頼みの手動バックアップ手順をやめる)。

    `sqlite3` の backup API を使う (WAL の未チェックポイント分もこれなら
    安全に含めて複製できる — 生ファイルを `cp` するだけだと WAL 中身が
    抜ける可能性がある)。in-memory / パス解決できない接続 (`PRAGMA
    database_list` の file が空文字) はバックアップ対象が無いのでスキップ
    する。再試行 (途中失敗からの再開) でバックアップファイルが既に存在する
    場合は上書きしない — 上書きすると「本当に移行前の状態」ではなく
    「前回失敗後の状態」を複製してしまい、復元の意味が薄れる。
    """
    main = next((r for r in conn.execute("PRAGMA database_list")
                if r["name"] == "main"), None)
    db_file = main["file"] if main is not None else ""
    if not db_file:
        return
    src_path = Path(db_file)
    bak_path = src_path.with_name(src_path.name + ".bak-ohlcv-v2")
    if bak_path.exists():
        return
    bak_conn = sqlite3.connect(bak_path)
    try:
        conn.backup(bak_conn)
    finally:
        bak_conn.close()
    _log.warning("ohlcv v2 移行前のバックアップを作成しました: %s", bak_path)


def _migrate_ohlcv_v2(conn: sqlite3.Connection) -> None:
    """ohlcv を v1 (PK: symbol,interval,bar_time) から v2 (source/spread 列・
    PK に source を含む) へ table rebuild する。

    SQLite は既存テーブルの PK を ALTER できないため rebuild が必要。
    `_ensure_column` (単純な列追加) とは別関数にしてある。

    トランザクション内で完結させる (BEGIN IMMEDIATE → RENAME → CREATE →
    INSERT..SELECT → DROP → COMMIT、例外時 ROLLBACK)。

    **F2 (fix round 1, codex Critical): 並行 migration の再検証**。
    呼び出し側 (init_db) の判定と、ここで実際に `BEGIN IMMEDIATE` の
    書き込みロックを取るまでの間に、別接続が先に移行を完了させている
    ことがある (2 プロセス/2 接続が同時に起動した場合等)。ロック取得後に
    もう一度「`source` 列あり かつ `ohlcv_v1` 無し (= 完全に移行済み)」を
    再検査し、真なら何もせず抜ける。再検査せずに再構築すると
    `INSERT..SELECT` が `source='yfinance'` に決め打ちしているため、既に
    複数 source が入っている v2 テーブルを壊す (dukascopy 等の一括
    インポート行が yfinance 名義に化ける)。

    途中失敗時の再開性:
    - RENAME 前に失敗 → ROLLBACK で v1 のまま。次回起動時は `source` 列なし
      と判定して最初からやり直す。
    - RENAME 後 (`ohlcv_v1` 存在) かつ v2 の `ohlcv` も存在する状態で失敗
      (DROP 前) → 次回起動時は `ohlcv` が既に v2 (source 列あり) なので
      「移行不要」に見えてしまう。それを防ぐため、呼び出し側 (init_db) は
      `source` 列の有無だけでなく `ohlcv_v1` の残存も見て、残っていれば
      INSERT..SELECT からやり直す (このテーブルは INSERT OR IGNORE で
      冪等)。

    **F3 (fix round 1, codex Critical): 再開の安全化**。上記の再開時、
    `INSERT OR IGNORE` は同一キーの行を無条件に無視する。無視された行が
    「本当に同じ値」なのか「v2 側に別の値が既に入っている (破損 / 想定外の
    書き込み)」なのかを区別せずに `ohlcv_v1` を DROP すると、後者を黙って
    捨てることになる。DROP の前に `ohlcv_v1` と `ohlcv` の同一キー行を
    JOIN して値を比較し、1 件でも不一致があれば `ohlcv_v1` を DROP せず
    例外を送出して停止する (バックアップからの復元と手動調査を促す)。
    `ohlcv_v1` が v1 形式 (source 列なし) なら合成 source は 'yfinance'
    固定、`ohlcv_v1` 自体が v2 形式で残っていた場合 (想定外だが F2 適用前の
    バイナリが作った残骸の可能性) は列をそのまま引き継いで比較する。

    実 DB 適用時のバックアップは `_backup_before_migration` (F8) が自動で
    取る (このバックアップは「移行前スナップショット」— F3 の例外時の
    手動復元先)。
    """
    _backup_before_migration(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv)")}
        v1_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='ohlcv_v1'").fetchone() is not None
        if "source" in cols and not v1_exists:
            # F2: ロック取得までの間に別接続が移行を完了させていた。
            conn.commit()
            return
        if not v1_exists:
            conn.execute("ALTER TABLE ohlcv RENAME TO ohlcv_v1")
        conn.execute(_OHLCV_V2_DDL)

        v1_cols = {r["name"] for r in
                  conn.execute("PRAGMA table_info(ohlcv_v1)")}
        if "source" in v1_cols:
            # 想定外の残骸 (v2 形式のまま ohlcv_v1 になっている) — 列を
            # そのまま引き継ぐ
            conn.execute(
                "INSERT OR IGNORE INTO ohlcv (symbol, interval, bar_time, "
                "open, high, low, close, volume, source, spread) "
                "SELECT symbol, interval, bar_time, open, high, low, close, "
                "volume, source, spread FROM ohlcv_v1")
            join_source = "v2.source = v1.source"
            select_src = ("v1.source AS src_name, v1.spread AS spread1, "
                         "v2.spread AS spread2")
        else:
            conn.execute(
                "INSERT OR IGNORE INTO ohlcv (symbol, interval, bar_time, "
                "open, high, low, close, volume, source, spread) "
                "SELECT symbol, interval, bar_time, open, high, low, close, "
                "volume, 'yfinance', NULL FROM ohlcv_v1")
            join_source = "v2.source = 'yfinance'"
            select_src = ("'yfinance' AS src_name, NULL AS spread1, "
                         "v2.spread AS spread2")

        # F3: 同一キー行の値一致を検証する (INSERT OR IGNORE で無視された
        # 行が「同じ値だから無視された」のか「破損データと衝突して無視
        # された」のかを区別する)。fresh migration (ohlcv_v1 が今回の
        # RENAME で作られた場合) では ohlcv は直前まで空なので、JOIN は
        # 常に「自分がいま入れた行」同士の比較になり無条件に一致する。
        rows = conn.execute(
            f"SELECT v1.symbol, v1.interval, v1.bar_time, {select_src}, "
            "v1.open AS open1, v2.open AS open2, "
            "v1.high AS high1, v2.high AS high2, "
            "v1.low AS low1, v2.low AS low2, "
            "v1.close AS close1, v2.close AS close2, "
            "v1.volume AS volume1, v2.volume AS volume2 "
            "FROM ohlcv_v1 v1 JOIN ohlcv v2 "
            "ON v2.symbol = v1.symbol AND v2.interval = v1.interval "
            f"AND v2.bar_time = v1.bar_time AND {join_source}"
        ).fetchall()
        for r in rows:
            ok = (_values_match(r["open1"], r["open2"])
                  and _values_match(r["high1"], r["high2"])
                  and _values_match(r["low1"], r["low2"])
                  and _values_match(r["close1"], r["close2"])
                  and _values_match(r["volume1"], r["volume2"])
                  and _values_match(r["spread1"], r["spread2"]))
            if not ok:
                raise RuntimeError(
                    "ohlcv migration: ohlcv_v1 と ohlcv (v2) で値が一致しない"
                    f"行があります ({r['symbol']}/{r['interval']}/"
                    f"{r['bar_time']}/{r['src_name']})。ohlcv_v1 は温存しま"
                    "した。data/agentic.db.bak-ohlcv-v2 (または手動バック"
                    "アップ) からの復元と手動調査が必要です。")

        conn.execute("DROP TABLE ohlcv_v1")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv)")}
    v1_leftover = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='ohlcv_v1'").fetchone() is not None
    if "source" not in cols or v1_leftover:
        _migrate_ohlcv_v2(conn)
    _ensure_column(conn, "missions", "trigger", "trigger TEXT")
    conn.commit()
