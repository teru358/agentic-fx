"""SQLite 接続 + 11 テーブルスキーマ — 設計書 §12。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv (
  symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
  close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (symbol, interval, bar_time)
);
CREATE TABLE IF NOT EXISTS missions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loop TEXT NOT NULL,            -- trade | improve | ask | reflection
  runner TEXT NOT NULL, model TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
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


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()
