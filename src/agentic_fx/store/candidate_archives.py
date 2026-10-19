"""Mission 中に評価した候補 archive の来歴 CRUD。"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Iterable

_log = logging.getLogger("agentic_fx.store.candidate_archives")

# ライフサイクル専用の分類。DDL の CHECK 句 (`store/db.py`) と同じ 14 値。
OUTCOME_CLASSES = (
    "not_submitted", "pending", "approved", "rejected_by_human", "expired",
    "invalidated", "revoked", "unprofitable", "insufficient_trades",
    "gate_failed", "noop_copy", "duplicate_logic", "duplicate_metrics",
    "family_redirect")

# 検出属性。人間の決定後も消えない。
QUALITY_FLAGS = frozenset({"family_detected", "legacy_unlinked"})

# 許される遷移は 13 本だけ。書き手は `settle` のみ (終端 tx の
# `not_submitted` 起点、`apply_decision` の `pending` 起点、
# `append_revocation` の `approved` 起点がそれぞれ呼ぶ)。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "not_submitted": frozenset({
        "pending", "unprofitable", "insufficient_trades", "gate_failed",
        "noop_copy", "duplicate_logic", "duplicate_metrics",
        "family_redirect"}),
    "pending": frozenset({
        "approved", "rejected_by_human", "expired", "invalidated"}),
    "approved": frozenset({"revoked"}),
}

# approval_requests.status → 台帳の分類 (旧行の照合用)
_APPROVAL_STATUS_TO_OUTCOME = {
    "pending": "pending", "approved": "approved",
    "rejected": "rejected_by_human", "expired": "expired",
    "invalidated": "invalidated"}


class InvalidTransitionError(ValueError):
    """`outcome_class` の許されない遷移、または集合外の値。"""


def _checked_flags(flags: Iterable[str]) -> list[str]:
    values = sorted(set(flags))
    unknown = [f for f in values if f not in QUALITY_FLAGS]
    if unknown:
        raise ValueError(f"unsupported quality_flags: {unknown!r}")
    return values


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["metrics"] = json.loads(result.pop("metrics_json"))
    if "quality_flags" in result:
        result["quality_flags"] = json.loads(result["quality_flags"])
    return result


def insert(conn: sqlite3.Connection, *, mission_id: int, name: str,
           content_hash: str, artifact_hash: str, archive_path: str | None,
           pair: str | None, metrics: dict, now: datetime,
           logic_hash: str | None = None, family_key: str | None = None,
           quality_flags: Iterable[str] = (),
           commit: bool = False) -> int:
    """行を追加する。同じ Mission/artifact は既存 id を返す。

    `outcome_class` は常に初期値 `not_submitted`。`pair` は属性で、
    indicator 候補は None。`quality_flags` は集合外の要素を拒否する
    (`INSERT OR IGNORE` は制約違反も黙って無視するため、先に検査する)。"""
    flags = _checked_flags(quality_flags)
    cur = conn.execute(
        "INSERT OR IGNORE INTO candidate_archives "
        "(mission_id,name,content_hash,artifact_hash,archive_path,pair,"
        "metrics_json,created_at,logic_hash,family_key,quality_flags) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (mission_id, name, content_hash, artifact_hash, archive_path, pair,
         json.dumps(metrics, sort_keys=True), now.isoformat(),
         logic_hash, family_key, json.dumps(flags)))
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


def list_by_mission(conn: sqlite3.Connection, mission_id: int, *,
                    evaluated_only: bool = False) -> list[dict]:
    """Mission の行を id 順で返す。`evaluated_only` は backtest の成績を持つ
    行だけに絞る (提出直前に作る最終 artifact の行は成績を持たない)。"""
    query = "SELECT * FROM candidate_archives WHERE mission_id=?"
    if evaluated_only:
        query += " AND metrics_json != '{}'"
    return [_decode(row) for row in conn.execute(
        query + " ORDER BY id", (mission_id,))]


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
    に限定する。同一 mission/content_hash で `artifact_hash` の異なる
    行が複数あり得る (self-test 書き直し等) ため、選択規約は
    `archive_path IS NOT NULL` を最優先し (実在する archive を「不明」
    と誤表示しない、codex 1周目 I2 是正)、同点 (すべて NULL、または
    複数の有効行) なら `id` 降順 (最新) を返す。"""
    row = conn.execute(
        "SELECT * FROM candidate_archives WHERE mission_id=? "
        "AND content_hash=? "
        "ORDER BY (archive_path IS NOT NULL) DESC, id DESC LIMIT 1",
        (mission_id, content_hash)).fetchone()
    if row is None:
        return None
    return _decode(row)


def get(conn: sqlite3.Connection, archive_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM candidate_archives WHERE id=?", (archive_id,)).fetchone()
    return None if row is None else _decode(row)


def settle(conn: sqlite3.Connection, archive_id: int, outcome: str, *,
           expected_from: str = "not_submitted",
           add_flags: Iterable[str] = (), commit: bool = False) -> bool:
    """`outcome_class` を `expected_from` → `outcome` へ CAS で更新する。

    `outcome_class` を書く唯一の関数。遷移表 (`ALLOWED_TRANSITIONS`) にない組は
    `InvalidTransitionError`。現在値が `expected_from` でなければ更新せず
    False を返す。`add_flags` は検出属性を足す (集合外は拒否)。"""
    if outcome not in OUTCOME_CLASSES or expected_from not in OUTCOME_CLASSES:
        raise InvalidTransitionError(
            f"unknown outcome_class: {expected_from!r} -> {outcome!r}")
    if outcome not in ALLOWED_TRANSITIONS.get(expected_from, frozenset()):
        raise InvalidTransitionError(
            f"transition not allowed: {expected_from!r} -> {outcome!r}")
    extra = _checked_flags(add_flags)
    row = conn.execute(
        "SELECT quality_flags FROM candidate_archives WHERE id=?",
        (archive_id,)).fetchone()
    if row is None:
        return False
    merged = _checked_flags([*json.loads(row["quality_flags"]), *extra])
    cur = conn.execute(
        "UPDATE candidate_archives SET outcome_class=?, quality_flags=? "
        "WHERE id=? AND outcome_class=?",
        (outcome, json.dumps(merged), archive_id, expected_from))
    if commit:
        conn.commit()
    return cur.rowcount == 1


def resolve(conn: sqlite3.Connection, payload: Any) -> int | None:
    """承認 payload から台帳の行 id を引く。

    (1) `candidate_archive_id` が有効な整数ならそれが正で、行が無ければ
    None (別の行へ結ばない)。id を持たない旧 payload だけ、(2) `(mission_id,
    artifact_hash)` の完全一致、(3) `(mission_id, content_hash)` でちょうど
    1 行のときに限り採用する。曖昧な行を結ばない。"""
    if not isinstance(payload, dict):
        return None
    candidate_id = payload.get("candidate_archive_id")
    if isinstance(candidate_id, int) and not isinstance(candidate_id, bool):
        row = conn.execute(
            "SELECT id FROM candidate_archives WHERE id=?",
            (candidate_id,)).fetchone()
        return None if row is None else row["id"]
    mission_id = payload.get("mission_id")
    if not isinstance(mission_id, int) or isinstance(mission_id, bool):
        return None
    artifact_hash = payload.get("artifact_hash")
    if isinstance(artifact_hash, str):
        row = conn.execute(
            "SELECT id FROM candidate_archives WHERE mission_id=? "
            "AND artifact_hash=?", (mission_id, artifact_hash)).fetchone()
        if row is not None:
            return row["id"]
    content_hash = payload.get("content_hash")
    if isinstance(content_hash, str):
        rows = conn.execute(
            "SELECT id FROM candidate_archives WHERE mission_id=? "
            "AND content_hash=?", (mission_id, content_hash)).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
    return None


def backfill_outcome_from_approvals(conn: sqlite3.Connection) -> None:
    """旧行の `outcome_class` を承認との照合で埋める (migration 専用)。

    遷移表の外で直接設定してよいのはこの関数だけ。呼び出し元の tx に参加し、
    commit しない。結べなかった承認の mission の行は `not_submitted` のまま
    `legacy_unlinked` を足す。"""
    approvals = conn.execute(
        "SELECT id, status, payload_json FROM approval_requests "
        "WHERE kind='plugin' ORDER BY id").fetchall()
    decoded: dict[int, dict] = {}
    unlinked_missions: set[int] = set()
    revocations: list[dict] = []
    for approval in approvals:
        try:
            payload = json.loads(approval["payload_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        decoded[approval["id"]] = payload
        if payload.get("revokes_approval_id") is not None:
            revocations.append(payload)
            continue
        outcome = _APPROVAL_STATUS_TO_OUTCOME.get(approval["status"])
        archive_id = resolve(conn, payload)
        if archive_id is None:
            mission_id = payload.get("mission_id")
            if isinstance(mission_id, int) and not isinstance(mission_id, bool):
                unlinked_missions.add(mission_id)
            continue
        if outcome is not None:
            conn.execute(
                "UPDATE candidate_archives SET outcome_class=? WHERE id=?",
                (outcome, archive_id))
    for revocation in revocations:
        target = decoded.get(revocation.get("revokes_approval_id"))
        archive_id = resolve(conn, target)
        if archive_id is not None:
            conn.execute(
                "UPDATE candidate_archives SET outcome_class='revoked' "
                "WHERE id=? AND outcome_class='approved'", (archive_id,))
    for mission_id in sorted(unlinked_missions):
        for row in conn.execute(
                "SELECT id, quality_flags FROM candidate_archives "
                "WHERE mission_id=? AND outcome_class='not_submitted'",
                (mission_id,)).fetchall():
            flags = _checked_flags(
                [*json.loads(row["quality_flags"]), "legacy_unlinked"])
            conn.execute(
                "UPDATE candidate_archives SET quality_flags=? WHERE id=?",
                (json.dumps(flags), row["id"]))
