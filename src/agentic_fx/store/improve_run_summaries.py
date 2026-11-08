"""改善 mission 1 回ごとの要約 (`improve_run_summaries`) の書込みと読出し。

行は終端と同じ tx で 1 度だけ書き、後から変えない (UPDATE / DELETE は
trigger が拒む)。中身 (`summary_json`) の組み立てと検査は
`agentic_fx.loops.improve_run_summary` が持ち、ここは保存だけを担う。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

SUMMARY_VERSION = 1
STATE_OK = "ok"
STATE_EMPTY = "empty"
STATE_BUILD_FAILED = "build_failed"
STATES = (STATE_OK, STATE_EMPTY, STATE_BUILD_FAILED)


@dataclass(frozen=True)
class SummaryRecord:
    """保存する要約 1 件。`summary_json` は直列化済みの文字列。"""
    state: str
    summary_json: str

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"unknown summary state: {self.state!r}")
        if not isinstance(self.summary_json, str):
            raise TypeError("summary_json must be a serialized string")


EMPTY_SUMMARY = SummaryRecord(STATE_EMPTY, "{}")


def insert(conn: sqlite3.Connection, mission_id: int, record: SummaryRecord,
           *, now: datetime, ignore_existing: bool = False) -> None:
    """要約を 1 行書く。`ignore_existing` は起動時の回収だけが使う。"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("improve_run_summaries: naive datetime は受け付けない")
    verb = "INSERT OR IGNORE" if ignore_existing else "INSERT"
    conn.execute(
        f"{verb} INTO improve_run_summaries (mission_id, summary_version, "
        "state, summary_json, created_at) VALUES (?,?,?,?,?)",
        (mission_id, SUMMARY_VERSION, record.state, record.summary_json,
         now.astimezone(timezone.utc).isoformat()))


def fetch(conn: sqlite3.Connection, mission_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT mission_id, summary_version, state, summary_json, created_at "
        "FROM improve_run_summaries WHERE mission_id=?",
        (mission_id,)).fetchone()
