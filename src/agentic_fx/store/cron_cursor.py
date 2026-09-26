"""判断足 cron の処理済み watermark (L) の永続化。

行は (pair, interval) 単位。bar_time は既存値と新値の max でのみ前進する。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("cron_cursor: naive datetime は受け付けない")
    return value.astimezone(timezone.utc).isoformat()


def upsert(conn: sqlite3.Connection, pair: str, interval: str,
           bar_time: datetime, *, now: datetime) -> None:
    # with conn: 例外時は ROLLBACK し、呼び出し側の接続に未確定の
    # transaction を残さない。
    with conn:
        conn.execute(
            "INSERT INTO cron_cursor (pair, interval, bar_time, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(pair, interval) DO UPDATE SET "
            "bar_time=MAX(cron_cursor.bar_time, excluded.bar_time), "
            "updated_at=excluded.updated_at",
            (pair, interval, _iso(bar_time), _iso(now)))


def load_all(conn: sqlite3.Connection) -> dict[tuple[str, str], datetime]:
    rows = conn.execute(
        "SELECT pair, interval, bar_time FROM cron_cursor").fetchall()
    return {(r["pair"], r["interval"]):
            datetime.fromisoformat(r["bar_time"]).astimezone(timezone.utc)
            for r in rows}
