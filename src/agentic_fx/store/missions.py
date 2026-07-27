from __future__ import annotations

import json
import sqlite3
from datetime import datetime


def start(conn: sqlite3.Connection, loop: str, runner: str, model: str,
          now: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO missions (loop, runner, model, status, started_at) "
        "VALUES (?,?,?,'running',?)", (loop, runner, model, now.isoformat()))
    conn.commit()
    return cur.lastrowid


def finish(conn: sqlite3.Connection, mission_id: int, status: str,
           output: dict | None, transcript: list, now: datetime) -> None:
    conn.execute(
        "UPDATE missions SET status=?, output_json=?, transcript_json=?, "
        "finished_at=? WHERE id=?",
        (status,
         json.dumps(output, ensure_ascii=False) if output is not None else None,
         json.dumps(transcript, ensure_ascii=False),
         now.isoformat(), mission_id))
    conn.commit()


def loop_of(conn: sqlite3.Connection, mission_id: int) -> str | None:
    """mission の loop 種別。存在しなければ None。

    executor が「その intent は取引判断 Mission の出力か」を DB で照合する
    ために使う (設計書 §5)。origin は呼び出し側が渡す enum 値に過ぎず、
    任意の内部コードが Origin.SCHEDULER を構成できてしまうため、origin 検証
    だけでは「scheduler が起動した取引判断 Mission だけ」という性質を担保
    できない (codex レビュー 4)。
    """
    row = conn.execute(
        "SELECT loop FROM missions WHERE id=?", (mission_id,)).fetchone()
    return row["loop"] if row is not None else None


def recent(conn: sqlite3.Connection, n: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM missions ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows]
