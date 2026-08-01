from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from agentic_fx.core.contracts import Bar

_log = logging.getLogger("agentic_fx.store.ohlcv")

_FLOAT_TOL = 1e-9


def upsert_bars(conn: sqlite3.Connection, bars: list[Bar], *,
                source: str) -> int:
    """live キャッシュ用。形成中バーの更新があるため常に上書きする
    (既存行不変の import_bars とは別契約 — spec §6 の live 例外)。"""
    conn.executemany(
        "INSERT INTO ohlcv (symbol, interval, bar_time, open, high, low, "
        "close, volume, source) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol, interval, bar_time, source) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume",
        [(b.symbol, b.interval, b.ts.isoformat(), b.open, b.high, b.low,
          b.close, b.volume, source) for b in bars])
    conn.commit()
    return len(bars)


def load_bars(conn: sqlite3.Connection, symbol: str, interval: str, *,
              source: str, since: datetime | None = None,
              until: datetime | None = None) -> list[Bar]:
    q = "SELECT * FROM ohlcv WHERE symbol=? AND interval=? AND source=?"
    args: list = [symbol, interval, source]
    if since is not None:
        q += " AND bar_time >= ?"
        args.append(since.isoformat())
    if until is not None:
        q += " AND bar_time <= ?"
        args.append(until.isoformat())
    rows = conn.execute(q + " ORDER BY bar_time", args).fetchall()
    return [Bar(r["symbol"], r["interval"],
                datetime.fromisoformat(r["bar_time"]),
                r["open"], r["high"], r["low"], r["close"], r["volume"])
            for r in rows]


def load_spread(conn: sqlite3.Connection, symbol: str, bar_time_iso: str, *,
                source: str) -> float | None:
    """指定 (symbol, bar_time, source) の spread。

    シグネチャは brief 通り `interval` を取らない。ohlcv の PK は
    (symbol, interval, bar_time, source) の 4 列なので、複数 interval が
    同じ bar_time を共有する行が (symbol, bar_time, source) だけでは一意に
    絞り込めない場合がある (例: 1m と 1h の bar_time がたまたま一致する
    瞬間)。`ORDER BY interval` で決定的な行を選ぶことで非決定的な結果は
    避けるが、「どの interval の spread か」を呼び出し側が区別できない
    ことに変わりはない — 複数 interval を横断して同じ bar_time の spread を
    使い分けたい呼び出し側は、このまま使わず `load_bars` 等で interval を
    絞ってから読むこと (task-1-report.md の逸脱に記録)。
    """
    row = conn.execute(
        "SELECT spread FROM ohlcv WHERE symbol=? AND bar_time=? AND source=? "
        "ORDER BY interval LIMIT 1",
        (symbol, bar_time_iso, source)).fetchone()
    return row["spread"] if row is not None else None


@dataclass(frozen=True)
class ImportResult:
    inserted: int
    unchanged: int
    conflicted: int


def _close_enough(a: float | None, b: float | None) -> bool:
    """既存行 vs 入力行の 1 列比較。spread の NULL 同士は一致、NULL vs 数値は
    conflicted (spec §6)。"""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < _FLOAT_TOL


def import_bars(conn: sqlite3.Connection, rows: list[tuple], *,
                source: str) -> ImportResult:
    """インポータ用。既存行は不変 — 同一キー同一値は無変更 (unchanged)、
    値が異なる既存行は入力をスキップして件数だけ計上する (conflicted)。

    rows: (symbol, interval, bar_time_iso, o, h, l, c, volume, spread|None)
    """
    inserted = 0
    unchanged = 0
    conflicted = 0
    for (symbol, interval, bar_time_iso, o, h, l, c, volume,  # noqa: E741
         spread) in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO ohlcv (symbol, interval, bar_time, open, "
            "high, low, close, volume, source, spread) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (symbol, interval, bar_time_iso, o, h, l, c, volume, source,
             spread))
        if cur.rowcount == 1:
            inserted += 1
            continue
        # 既に同一キーの行がある (INSERT OR IGNORE が無視した) → 既存行と
        # 値を比較する。行は変更しない。
        existing = conn.execute(
            "SELECT open, high, low, close, volume, spread FROM ohlcv "
            "WHERE symbol=? AND interval=? AND bar_time=? AND source=?",
            (symbol, interval, bar_time_iso, source)).fetchone()
        same = (
            _close_enough(existing["open"], o)
            and _close_enough(existing["high"], h)
            and _close_enough(existing["low"], l)
            and _close_enough(existing["close"], c)
            and _close_enough(existing["volume"], volume)
            and _close_enough(existing["spread"], spread)
        )
        if same:
            unchanged += 1
        else:
            conflicted += 1
    conn.commit()
    if conflicted > 0:
        _log.warning("import_bars: %d row(s) conflicted with existing data "
                     "for source=%s (existing rows left unchanged)",
                     conflicted, source)
    return ImportResult(inserted, unchanged, conflicted)
