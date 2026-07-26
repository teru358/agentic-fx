from __future__ import annotations

import sqlite3
from datetime import datetime

from agentic_fx.core.contracts import Bar


def upsert_bars(conn: sqlite3.Connection, bars: list[Bar]) -> int:
    conn.executemany(
        "INSERT INTO ohlcv (symbol, interval, bar_time, open, high, low, close, "
        "volume) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol, interval, bar_time) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume",
        [(b.symbol, b.interval, b.ts.isoformat(), b.open, b.high, b.low,
          b.close, b.volume) for b in bars])
    conn.commit()
    return len(bars)


def load_bars(conn: sqlite3.Connection, symbol: str, interval: str,
              since: datetime | None = None) -> list[Bar]:
    q = "SELECT * FROM ohlcv WHERE symbol=? AND interval=?"
    args: list = [symbol, interval]
    if since is not None:
        q += " AND bar_time >= ?"
        args.append(since.isoformat())
    rows = conn.execute(q + " ORDER BY bar_time", args).fetchall()
    return [Bar(r["symbol"], r["interval"],
                datetime.fromisoformat(r["bar_time"]),
                r["open"], r["high"], r["low"], r["close"], r["volume"])
            for r in rows]
