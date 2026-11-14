"""履歴を書く処理 (取込・修復・置換) 同士を直列化する advisory lease。

DB の write transaction を長く持つ代わりに、DB と同じ directory の lock file に
`flock(LOCK_EX | LOCK_NB)` をかける。待たない: 取れなければ `HistoryLeaseBusy`。
取得の順序は lease → (WriterAdmission) → BEGIN。取得 (network) の間は持たない。
scheduler の通常の取引 DB 書込みと読取りは対象外で、履歴 writer 同士だけを排他する。
"""
from __future__ import annotations

import fcntl
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LEASE_FILE = "history-mutation.lock"

_memory_lock = threading.Lock()


class HistoryLeaseBusy(RuntimeError):
    """別の履歴 writer が lease を持っている。"""


def _db_file(conn: sqlite3.Connection) -> Path | None:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2]) if row[2] else None
    return None


@contextmanager
def history_mutation_lease(conn: sqlite3.Connection) -> Iterator[None]:
    """`conn` の DB に対する履歴 writer の排他を取る。取れなければ HistoryLeaseBusy。"""
    db_file = _db_file(conn)
    if db_file is None:                       # 一時 DB (file なし): process 内だけ排他
        if not _memory_lock.acquire(blocking=False):
            raise HistoryLeaseBusy("history mutation lease is held")
        try:
            yield
        finally:
            _memory_lock.release()
        return
    fh = open(db_file.parent / LEASE_FILE, "a")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise HistoryLeaseBusy("history mutation lease is held") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()
