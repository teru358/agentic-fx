"""SQLite 接続 + 11 テーブルスキーマ — 設計書 §12。"""
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
"""

TABLE_NAMES = frozenset({
    "ohlcv", "missions", "trade_intents", "orders", "reflections",
    "account_snapshots", "improvement_backlog", "improvement_runs",
    "econ_events", "approval_requests", "news_sources",
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


def _migrate_ohlcv_v2(conn: sqlite3.Connection) -> None:
    """ohlcv を v1 (PK: symbol,interval,bar_time) から v2 (source/spread 列・
    PK に source を含む) へ table rebuild する。

    SQLite は既存テーブルの PK を ALTER できないため rebuild が必要。
    `_ensure_column` (単純な列追加) とは別関数にしてある。

    トランザクション内で完結させる (BEGIN IMMEDIATE → RENAME → CREATE →
    INSERT..SELECT → DROP → COMMIT、例外時 ROLLBACK)。途中失敗時の再開性:
    - RENAME 前に失敗 → ROLLBACK で v1 のまま。次回起動時は `source` 列なし
      と判定して最初からやり直す。
    - RENAME 後 (`ohlcv_v1` 存在) かつ v2 の `ohlcv` も存在する状態で失敗
      (DROP 前) → 次回起動時は `ohlcv` が既に v2 (source 列あり) なので
      「移行不要」に見えてしまう。それを防ぐため、呼び出し側 (init_db) は
      `source` 列の有無だけでなく `ohlcv_v1` の残存も見て、残っていれば
      INSERT..SELECT からやり直す (このテーブルは INSERT OR IGNORE で
      冪等)。

    **運用手順 (実 DB への適用時)**: サービス停止中に行うこと。適用前に
    `cp data/agentic.db data/agentic.db.bak-YYYYMMDD` でバックアップを取る
    こと (トランザクション内 rebuild で ROLLBACK は保証するが、ディスク破損
    やプロセス kill -9 等トランザクション保護の外側の事故に備える)。
    """
    _log.warning(
        "ohlcv テーブルを v2 (source/spread 列・PK 再構築) へ移行します。"
        "サービス停止中に実行し、実行前に "
        "'cp data/agentic.db data/agentic.db.bak-YYYYMMDD' でバックアップを"
        "取得済みであることを確認してください。")
    conn.execute("BEGIN IMMEDIATE")
    try:
        v1_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='ohlcv_v1'").fetchone() is not None
        if not v1_exists:
            conn.execute("ALTER TABLE ohlcv RENAME TO ohlcv_v1")
        conn.execute(_OHLCV_V2_DDL)
        conn.execute(
            "INSERT OR IGNORE INTO ohlcv (symbol, interval, bar_time, open, "
            "high, low, close, volume, source, spread) "
            "SELECT symbol, interval, bar_time, open, high, low, close, "
            "volume, 'yfinance', NULL FROM ohlcv_v1")
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
