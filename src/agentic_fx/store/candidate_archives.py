"""Mission 中に評価した候補 archive の来歴 CRUD。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["metrics"] = json.loads(result.pop("metrics_json"))
    return result


def insert(conn: sqlite3.Connection, *, mission_id: int, name: str,
           content_hash: str, artifact_hash: str, archive_path: str | None,
           pair: str, metrics: dict, now: datetime,
           commit: bool = False) -> int:
    """行を追加する。同じ Mission/artifact は既存 id を返す。"""
    cur = conn.execute(
        "INSERT OR IGNORE INTO candidate_archives "
        "(mission_id,name,content_hash,artifact_hash,archive_path,pair,"
        "metrics_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (mission_id, name, content_hash, artifact_hash, archive_path, pair,
         json.dumps(metrics, sort_keys=True), now.isoformat()))
    if cur.rowcount == 1:
        archive_id = cur.lastrowid
    else:
        row = conn.execute(
            "SELECT id FROM candidate_archives WHERE mission_id=? "
            "AND artifact_hash=?", (mission_id, artifact_hash)).fetchone()
        archive_id = row["id"]
    if commit:
        conn.commit()
    return archive_id


def list_by_mission(conn: sqlite3.Connection, mission_id: int) -> list[dict]:
    return [_decode(row) for row in conn.execute(
        "SELECT * FROM candidate_archives WHERE mission_id=? ORDER BY id",
        (mission_id,))]


def clear_path(conn: sqlite3.Connection, archive_id: int, *,
               commit: bool = False) -> None:
    conn.execute(
        "UPDATE candidate_archives SET archive_path=NULL WHERE id=?",
        (archive_id,))
    if commit:
        conn.commit()


def find_without_path(conn: sqlite3.Connection) -> list[dict]:
    return [_decode(row) for row in conn.execute(
        "SELECT * FROM candidate_archives WHERE archive_path IS NULL ORDER BY id")]
