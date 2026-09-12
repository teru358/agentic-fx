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


def find_by_mission_content(conn: sqlite3.Connection, *, mission_id: int,
                            content_hash: str) -> dict[str, Any] | None:
    """approval-quality 設計書 §C ([archive-artifact-hash-vs-submitted]):
    人間向け導線・監査ツールの archive 引き当ては `(mission_id,
    content_hash)` で行う — `artifact_hash` では引かない。

    `artifact_hash` は backtest 時点 (archive snapshot) と提出時点
    (approval payload) の両方で計算されるが、agent が提出前に self-test
    (`test_plugin.py`、`artifact_hash` に含まれる) を書き直すと両者が
    ずれる (run12 観測 C、`content_hash` は plugin.py/config.yaml 相当
    のみをカバーするため両時点で不変)。`(mission_id, artifact_hash)` の
    UNIQUE 制約自体はそのまま — この関数は検索キーだけを `content_hash`
    に限定する。同一 mission に複数候補があるときも `content_hash` の
    一致行 1 件だけを返す。"""
    row = conn.execute(
        "SELECT * FROM candidate_archives WHERE mission_id=? "
        "AND content_hash=? ORDER BY id LIMIT 1",
        (mission_id, content_hash)).fetchone()
    if row is None:
        return None
    return _decode(row)
