"""固定 schema の event 投影と、client 側の cursor 規律。

永続化された event は cursor の後から何度でも同じ id で読める (at-least-once)。
生成は best-effort で、INSERT に失敗しても元の操作は失敗させない。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .contracts import ErrorCode, OpsError

FIELDS = {
    "ops_request_terminal": frozenset({"endpoint_code", "result_code", "authenticated_principal"}),
    "approval_state_changed": frozenset({"state", "decision_code", "decided_by"}),
    "job_state_changed": frozenset({"job_kind", "state", "result_code"}),
    "guard_state_changed": frozenset({"guard_kind", "state", "generation"}),
    "service_health_changed": frozenset({"component_code", "health_code"}),
    "event_stream_health": frozenset({"failure_count", "first_failed_at", "last_failed_at"}),
}

# 値は code・識別子・時刻・数だけ。空白や非 ASCII を含む自由文は入れられない。
_SAFE_VALUE = re.compile(r"[A-Za-z0-9_.:+\-]{0,64}")
MAX_POLL_LIMIT = 500

# ref は code ごとに決まった識別子だけ (§4.3)。自由文を ref 経由で永続化させない。
_REF = {
    "ops_request_terminal": re.compile(r"audit:[0-9]{1,18}"),
    "approval_state_changed": re.compile(r"approval:[0-9]{1,18}"),
    "job_state_changed": re.compile(r"job:[0-9a-f]{32}"),
    "guard_state_changed": re.compile(r"instance"),
    "service_health_changed": re.compile(r"instance"),
    "event_stream_health": re.compile(r"instance"),
}


class EventSchemaError(ValueError):
    """固定 schema 外の field または自由文を event に入れようとした。"""


def validate_fields(code: str, fields: dict, ref: str | None = None) -> None:
    if code not in FIELDS or set(fields) != FIELDS[code]:
        raise EventSchemaError(code)
    if ref is not None and (not isinstance(ref, str) or _REF[code].fullmatch(ref) is None):
        raise EventSchemaError(code)
    for value in fields.values():
        if value is None or (isinstance(value, int) and not isinstance(value, bool)):
            continue
        if not isinstance(value, str) or _SAFE_VALUE.fullmatch(value) is None:
            raise EventSchemaError(code)


@dataclass(frozen=True, slots=True)
class EventPage:
    events: list[dict]
    high_watermark: int


@dataclass(slots=True)
class _Failures:
    count: int = 0
    first: str | None = None
    last: str | None = None


class EventStore:
    def __init__(self, conn: sqlite3.Connection, *, wall_clock: Callable[[], datetime]) -> None:
        self._conn, self._wall_clock = conn, wall_clock
        self._failures = _Failures()
        self._failures_lock = threading.Lock()

    def emit(self, code: str, ref: str, fields: dict) -> int:
        """1 件を永続化する。schema 違反は呼び出し側の誤りなので例外にする。"""
        validate_fields(code, fields, ref)
        cur = self._conn.execute(
            "INSERT INTO ops_events(ts,code,ref,fields_json) VALUES (?,?,?,?)",
            (self._wall_clock().isoformat(), code, ref,
             json.dumps(fields, sort_keys=True, separators=(",", ":"))))
        self._conn.commit()
        return int(cur.lastrowid)

    def emit_best_effort(self, code: str, ref: str, fields: dict) -> int | None:
        """INSERT 障害を元操作へ伝えない。失敗はメモリ上で数えるだけにする。

        失敗した event には id が割り当てられない。次に INSERT が通ったとき、
        観測できた失敗の件数を ``event_stream_health`` として 1 回だけ試みる。
        """
        validate_fields(code, fields, ref)
        try:
            event_id = self.emit(code, ref, fields)
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            now = self._wall_clock().isoformat()
            with self._failures_lock:
                self._failures.count += 1
                self._failures.first = self._failures.first or now
                self._failures.last = now
            return None
        with self._failures_lock:
            pending, self._failures = self._failures, _Failures()
        if pending.count:
            try:
                self.emit("event_stream_health", "instance", {
                    "failure_count": pending.count, "first_failed_at": pending.first,
                    "last_failed_at": pending.last})
            except Exception:
                # 健康 event 自体も best-effort。数え直しはしない。
                try:
                    self._conn.rollback()
                except Exception:
                    pass
        return event_id

    def poll(self, *, after: int, limit: int) -> EventPage:
        if type(after) is not int or after < 0 or type(limit) is not int \
                or not 1 <= limit <= MAX_POLL_LIMIT:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        high = self._conn.execute("SELECT COALESCE(MAX(id),0) AS id FROM ops_events").fetchone()["id"]
        rows = self._conn.execute(
            "SELECT * FROM ops_events WHERE id>? ORDER BY id LIMIT ?", (after, limit)).fetchall()
        return EventPage([{"id": row["id"], "ts": row["ts"], "code": row["code"],
                           "ref": row["ref"], "fields": json.loads(row["fields_json"])}
                          for row in rows], int(high))


@dataclass(slots=True)
class EventCursor:
    """client が保存する cursor。表示を event id で冪等化してから進める。

    high watermark が前回より小さい page は DB の巻き戻りなどの異常なので、
    cursor を進めず ``health_error`` を立てる。
    """
    position: int = 0
    high_watermark: int = 0
    health_error: bool = False
    _seen: set[int] = field(default_factory=set)

    def consume(self, page: EventPage) -> list[dict]:
        if page.high_watermark < self.high_watermark:
            self.health_error = True
            return []
        fresh = [event for event in page.events
                 if event["id"] > self.position and event["id"] not in self._seen]
        for event in fresh:
            self._seen.add(event["id"])
        if fresh:
            self.position = max(event["id"] for event in fresh)
            self._seen = {i for i in self._seen if i > self.position - MAX_POLL_LIMIT}
        self.high_watermark = page.high_watermark
        return fresh
