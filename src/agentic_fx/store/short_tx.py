"""worker の短い transaction 1 本を開始から commit まで運ぶ共通部品。

`begin` が transaction を始める (writer admission を包める)。`checkpoint` は
commit の直前に呼ばれ、予算超過なら `OverBudget` を上げる。超過・CAS 不一致・
例外はどれも rollback する。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

Begin = Callable[[sqlite3.Connection], bool]
Checkpoint = Callable[[], None]


def begin_immediate(conn: sqlite3.Connection) -> bool:
    conn.execute("BEGIN IMMEDIATE")
    return True


@dataclass(frozen=True, slots=True)
class TxResult:
    """worker の短い transaction 1 本の結果。

    status: `committed` / `conflict` (CAS 不一致で rollback) / `yielded`
    (`begin` が transaction を始めなかった) / `over_budget` (予算超過で
    rollback)。`done` は複数 transaction に分けた操作 (削除) が終わったか。
    `commit_seconds` は COMMIT の所要。`value` は操作ごとの戻り値。
    """
    status: str
    set_id: int | None = None
    rows: int = 0
    done: bool = False
    commit_seconds: float | None = None
    value: Any = None

    @property
    def committed(self) -> bool:
        return self.status == "committed"


class Conflict(Exception):
    pass


class OverBudget(Exception):
    """`checkpoint` が上げる。transaction は rollback され `over_budget` になる。"""


def short_tx(conn: sqlite3.Connection, begin: Begin, body,
             checkpoint: Checkpoint | None = None) -> TxResult:
    if conn.in_transaction:
        raise RuntimeError("connection already has an open transaction")
    if not begin(conn):
        if conn.in_transaction:
            conn.rollback()
            raise RuntimeError("begin returned False after opening a transaction")
        return TxResult("yielded")
    if not conn.in_transaction:
        raise RuntimeError("begin returned True without opening a transaction")
    try:
        result = body()
        if checkpoint is not None:
            checkpoint()
    except Conflict:
        conn.rollback()
        return TxResult("conflict")
    except OverBudget:
        conn.rollback()
        return TxResult("over_budget")
    except BaseException:
        conn.rollback()
        raise
    started = time.perf_counter()
    try:
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return TxResult("committed", set_id=result.get("set_id"),
                    rows=result.get("rows", 0), done=result.get("done", False),
                    commit_seconds=time.perf_counter() - started,
                    value=result.get("value"))
