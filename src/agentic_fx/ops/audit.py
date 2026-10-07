"""append-only 監査と durable な idempotency 状態機械。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from .contracts import Caller, ErrorCode, OpsError


# 再送すれば成功し得る失敗。冪等行を残すと同じ key の再送が永久に同じ失敗になる。
TRANSIENT_FAILURES = frozenset({
    ErrorCode.DATABASE_BUSY.value, ErrorCode.REQUEST_TIMEOUT.value,
    ErrorCode.UNAVAILABLE.value, ErrorCode.MISSION_BUSY.value,
    ErrorCode.DECISION_IN_PROGRESS.value, ErrorCode.IMPROVE_RUNNING.value,
    ErrorCode.PLUGIN_BUSY.value})


def _digest(body: object) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AcceptedRequest:
    audit_id: int
    request_id: str


@dataclass(frozen=True, slots=True)
class IdempotencyReplay:
    """同じ key / 本文の再送。``in_flight`` は受理済みで終端前の job を指す。"""
    response: dict
    job_id: str | None
    in_flight: bool = False


class AuditStore:
    def __init__(self, conn: sqlite3.Connection, *, wall_clock: Callable[[], datetime]) -> None:
        self._conn, self._wall_clock = conn, wall_clock
        # Accepted records define the point after which a side effect may start.
        # Keep that boundary durable even when the caller's normal DB connection
        # uses a throughput-oriented synchronous setting.
        self._conn.execute("PRAGMA synchronous=FULL")

    @property
    def connection(self):
        return self._conn

    def accept(self, *, endpoint: str, principal: Caller, asserted_actor: str | None,
               peer_pid: int | None, peer_exe: str | None, body: object,
               target_ref: str | None, idempotency_key: str | None = None,
               job_id: str | None = None, commit: bool = True) -> AcceptedRequest:
        request_id = uuid.uuid4().hex
        cur = self._conn.execute(
            "INSERT INTO ops_requests(request_id,endpoint,authenticated_principal,"
            "asserted_actor,peer_pid,peer_exe,idempotency_key,body_sha256,target_ref,"
            "phase,job_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,'accepted',?,?)",
            (request_id, endpoint, principal.value, asserted_actor, peer_pid, peer_exe,
             idempotency_key, _digest(body), target_ref, job_id,
             self._wall_clock().isoformat()))
        if commit:
            self._conn.commit()
        return AcceptedRequest(int(cur.lastrowid), request_id)

    def accept_idempotent(self, *, endpoint: str, principal: Caller,
                          asserted_actor: str | None, key: str, body: object,
                          target_ref: str | None, job_id: str | None = None,
                          peer_pid: int | None = None, peer_exe: str | None = None,
                          ) -> AcceptedRequest | IdempotencyReplay:
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        digest = _digest(body)
        row = self._conn.execute(
            "SELECT body_sha256,state,response_json,expires_at,accepted_request_id "
            "FROM ops_idempotency "
            "WHERE authenticated_principal=? AND endpoint=? AND idempotency_key=?",
            (principal.value, endpoint, key)).fetchone()
        if row is not None and datetime.fromisoformat(row["expires_at"]) <= self._wall_clock():
            self._conn.execute(
                "DELETE FROM ops_idempotency WHERE authenticated_principal=? AND endpoint=? AND idempotency_key=?",
                (principal.value, endpoint, key))
            self._conn.commit()
            row = None
        if row is not None:
            if row["body_sha256"] != digest:
                raise OpsError(ErrorCode.IDEMPOTENCY_MISMATCH)
            if row["state"] == "accepted":
                job = self._conn.execute(
                    "SELECT job_id FROM ops_requests WHERE request_id=?",
                    (row["accepted_request_id"],)).fetchone()
                if job is not None and job["job_id"] is not None:
                    # 受理済みで走行中の job。同じ job id を返し、二度目の副作用を作らない。
                    return IdempotencyReplay({"job_id": job["job_id"]}, job["job_id"],
                                             in_flight=True)
                raise OpsError(ErrorCode.OUTCOME_UNKNOWN)
            if row["state"] == "outcome_unknown":
                raise OpsError(ErrorCode.OUTCOME_UNKNOWN)
            response = json.loads(row["response_json"])
            return IdempotencyReplay(response, response.get("job_id"))
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            accepted = self.accept(
                endpoint=endpoint, principal=principal, asserted_actor=asserted_actor,
                peer_pid=peer_pid, peer_exe=peer_exe, body=body, target_ref=target_ref,
                idempotency_key=key, job_id=job_id, commit=False)
            self._conn.execute(
                "INSERT INTO ops_idempotency(authenticated_principal,endpoint,"
                "idempotency_key,body_sha256,state,accepted_request_id,expires_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (principal.value, endpoint, key, digest, "accepted", accepted.request_id,
                 (self._wall_clock() + timedelta(hours=24)).isoformat()))
            self._conn.commit()
            return accepted
        except BaseException:
            self._conn.rollback()
            raise

    def terminal(self, audit_id: int, *, code: str, response: dict,
                 state: str | None = None) -> bool:
        """終端行を 1 回だけ追記する。既に終端があれば何もせず False。"""
        phase = state or ("succeeded" if code == "ok" else "failed")
        payload = json.dumps(response, sort_keys=True, separators=(",", ":"))
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            request = self._conn.execute(
                "SELECT request_id,authenticated_principal,endpoint,idempotency_key,job_id "
                "FROM ops_requests a WHERE id=? AND phase='accepted' AND NOT EXISTS "
                "(SELECT 1 FROM ops_requests t WHERE t.target_ref='audit:' || a.id)",
                (audit_id,)).fetchone()
            if request is None:
                self._conn.rollback()
                return False
            self._conn.execute(
                "INSERT INTO ops_requests(request_id,endpoint,authenticated_principal,"
                "body_sha256,target_ref,phase,result_code,job_id,response_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, request["endpoint"], request["authenticated_principal"],
                 "", f"audit:{audit_id}", phase, code, request["job_id"], payload,
                 self._wall_clock().isoformat()))
            if request["idempotency_key"] is not None and code in TRANSIENT_FAILURES:
                # 監査の終端行は残し、冪等行だけを同じ transaction で消す。
                self._conn.execute(
                    "DELETE FROM ops_idempotency WHERE accepted_request_id=?",
                    (request["request_id"],))
            elif request["idempotency_key"] is not None:
                self._conn.execute(
                    "UPDATE ops_idempotency SET state=?,response_json=? WHERE accepted_request_id=?",
                    (phase, payload, request["request_id"]))
            self._conn.commit()
            return True
        except BaseException:
            self._conn.rollback()
            raise

    def unfinished(self) -> list[sqlite3.Row]:
        """accepted だけで終端行の無い要求 (起動時回復の対象)。"""
        return self._conn.execute(
            "SELECT * FROM ops_requests a WHERE phase='accepted' AND NOT EXISTS "
            "(SELECT 1 FROM ops_requests t WHERE t.target_ref='audit:' || a.id) "
            "ORDER BY id").fetchall()

    def recover_unfinished(self) -> list[int]:
        ids = [int(row["id"]) for row in self.unfinished()]
        for audit_id in ids:
            self.terminal(audit_id, code=ErrorCode.OUTCOME_UNKNOWN.value,
                          response={"error": {"code": ErrorCode.OUTCOME_UNKNOWN.value}},
                          state="outcome_unknown")
        return ids
