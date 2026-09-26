"""cron mission が前進させた (pair, 判断足) ごとの確定足 (provenance)。"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone


def _iso(value: datetime) -> str:
    # cron_cursor._iso と同じ規律: naive datetime は `.astimezone()` が
    # ローカル時刻とみなして黙って解釈してしまうため、明示的に拒否する。
    if value.tzinfo is None:
        raise ValueError("mission_decision_bars: naive datetime は受け付けない")
    return value.astimezone(timezone.utc).isoformat()


def insert_many(conn: sqlite3.Connection, mission_id: int,
                bars: Mapping[tuple[str, str], datetime]) -> None:
    """commit しない。missions.start(commit=False) と同じ transaction で
    呼び出し側が commit / rollback する。"""
    conn.executemany(
        "INSERT INTO mission_decision_bars (mission_id, pair, interval, "
        "bar_time) VALUES (?,?,?,?)",
        [(mission_id, pair, interval, _iso(bar_time))
         for (pair, interval), bar_time in sorted(bars.items())])


def for_mission(conn: sqlite3.Connection,
                mission_id: int) -> dict[tuple[str, str], datetime]:
    rows = conn.execute(
        "SELECT pair, interval, bar_time FROM mission_decision_bars "
        "WHERE mission_id=?", (mission_id,)).fetchall()
    result: dict[tuple[str, str], datetime] = {}
    for r in rows:
        bar_time = datetime.fromisoformat(r["bar_time"])
        if bar_time.tzinfo is None:
            raise ValueError(
                "mission_decision_bars: naive datetime は受け付けない")
        result[(r["pair"], r["interval"])] = bar_time.astimezone(timezone.utc)
    return result
