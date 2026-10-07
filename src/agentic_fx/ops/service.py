"""HTTP adapter と対話シェルが共有する ops の入口。

このクラスは listener・鍵・socket を所有しない。listener は認証後にここへ
principal と request deadline (monotonic) を渡し、返値を JSON 応答へ、
:class:`OpsError` を固定 code の失敗応答へ変換する。

配線 (service 側の担当) の入口:

- 構築: ``OpsService(ops_conn, decide_conn=..., wall_clock=..., ...)``。ops 用と
  決定用に別の SQLite 接続を渡す (省略時は ops 接続と同じ DB file を別接続で開く)。
- 起動: plugin 切替 journal の reconcile (reconcile → sweep → expire) を済ませた
  後、listener を開く前に :meth:`recover_after_journal` を呼ぶ。policy file を
  canonical record に揃えるなら続けて :meth:`regenerate_policy` を呼ぶ。
- 対話シェル: ``Commands.use_ops_service(ops)`` で同じレーンを使わせる。
- 周期: 30 秒ごとに :meth:`flush_rejections` を呼ぶ (認証失敗等の集約 flush)。
- 停止: listener を閉じた後に :meth:`shutdown` を呼ぶ。
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .activity import SuppressedActivity
from .audit import AcceptedRequest, AuditStore, IdempotencyReplay
from .contracts import (PRINCIPAL_SCOPES, Caller, ErrorCode, OpsError, Principal, Scope,
                        ShellCaller, assert_authorized)
from .events import EventStore
from .jobs import Job, JobRegistry, JobState, new_job_id
from .tail import activity_filter, tail_lines

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OpsLimits:
    """待機と job の上限。V-4 の実測で確定した既定値。"""
    request_deadline_seconds: float = 5.0
    ops_lock_wait_seconds: float = 10.0
    decision_deadline_seconds: float = 35.0
    plugin_lock_wait_seconds: float = 30.0
    ask_deadline_seconds: float = 900.0
    improve_start_deadline_seconds: float = 35.0
    shutdown_join_seconds: float = 5.0
    # 副作用の後に終端を書く待ち。要求 deadline が尽きていても終端を落とさないための独立の予算。
    terminal_write_seconds: float = 2.0
    poll_interval_seconds: float = 0.01


@dataclass(frozen=True, slots=True)
class Endpoint:
    code: str
    route: str
    scope: Scope
    mutating: bool
    idempotent: bool = False
    # autopilot 中に API から呼べない変更 (§5)。シェルには従来どおり掛けない。
    autopilot_restricted: bool = False


ENDPOINTS: dict[str, Endpoint] = {e.code: e for e in (
    Endpoint("status", "GET /v1/status", Scope.STATUS_READ, False),
    Endpoint("log", "GET /v1/log", Scope.LOGS_READ, False),
    Endpoint("activity", "GET /v1/activity", Scope.LOGS_READ, False),
    Endpoint("approval_detail", "GET /v1/approvals/{id}", Scope.APPROVALS_DETAIL, False),
    Endpoint("approval_list", "GET /v1/approvals", Scope.APPROVALS_LIST, False),
    Endpoint("reflection_status", "GET /v1/reflections/{order_id}", Scope.STATUS_READ, False),
    Endpoint("whoami", "GET /v1/whoami", Scope.STATUS_READ, False),
    Endpoint("job_get", "GET /v1/jobs/{id}", Scope.JOBS_OWN, False),
    Endpoint("events", "GET /v1/events", Scope.EVENTS_READ, False),
    Endpoint("ask", "POST /v1/asks", Scope.OPERATE, True, idempotent=True),
    Endpoint("data_resume", "POST /v1/data/resume", Scope.LOCAL_GUARD, True,
             autopilot_restricted=True),
    Endpoint("approve", "POST /v1/approvals/{id}/approve", Scope.DECIDE, True,
             autopilot_restricted=True),
    Endpoint("reject", "POST /v1/approvals/{id}/reject", Scope.DECIDE, True),
    Endpoint("retry", "POST /v1/approvals/{id}/retry", Scope.DECIDE, True,
             autopilot_restricted=True),
    Endpoint("killswitch_reset", "POST /v1/killswitch/reset", Scope.LOCAL_GUARD, True,
             autopilot_restricted=True),
    Endpoint("killswitch_reconcile", "POST /v1/killswitch/reconcile", Scope.LOCAL_GUARD, True),
    Endpoint("reflection_retry", "POST /v1/reflections/{order_id}/retry", Scope.OPERATE, True,
             idempotent=True, autopilot_restricted=True),
    Endpoint("improve", "POST /v1/improve/runs", Scope.OPERATE, True, idempotent=True,
             autopilot_restricted=True),
    Endpoint("backlog_add", "POST /v1/backlog", Scope.OPERATE, True, idempotent=True,
             autopilot_restricted=True),
    Endpoint("backlog_reject", "POST /v1/backlog/{id}/reject", Scope.OPERATE, True,
             autopilot_restricted=True),
    Endpoint("backlog_reopen", "POST /v1/backlog/{id}/reopen", Scope.OPERATE, True,
             autopilot_restricted=True),
    Endpoint("backlog_note", "POST /v1/backlog/{id}/note", Scope.OPERATE, True,
             autopilot_restricted=True),
    Endpoint("policy", "POST /v1/policy", Scope.OPERATE, True, idempotent=True,
             autopilot_restricted=True),
    Endpoint("job_cancel", "POST /v1/jobs/{id}/cancel", Scope.JOBS_OWN, True),
)}

_DECISION_KINDS = ("approve", "reject", "retry")
_BACKLOG_TRANSITIONS = {
    "reject": (("open", "observation"), "rejected", "human_rejected"),
    "note": (("open", "observation"), "note", "human_noted"),
    "reopen": (("done", "rejected"), "open", "reopened"),
}
_DEPLOYED = frozenset({"deployed", "deployed_after_rollback", "invalidated"})


def _is_busy(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return "locked" in text or "busy" in text


_BUSY_ATTEMPT_MS = 50


class _PatientConnection:
    """SQLite busy の待ちを短い試行に刻み、試行の合間に停止合図と期限を見る接続。

    ``PRAGMA busy_timeout`` は一回の blocking 待機で停止合図を観測できないため、
    各試行を 50 ms に抑えて反復する (§5)。期限はレーンが thread ごとに設定し、
    レーン外の利用は呼び出しごとに既定の要求期限を使う。
    """

    def __init__(self, conn: sqlite3.Connection, *, stop_event: threading.Event,
                 monotonic: Callable[[], float], default_wait: float,
                 poll_interval: float) -> None:
        self._raw = conn
        self._stop_event = stop_event
        self._monotonic = monotonic
        self._default_wait = default_wait
        self._poll_interval = poll_interval
        self._local = threading.local()
        conn.execute(f"PRAGMA busy_timeout={_BUSY_ATTEMPT_MS}")

    @property
    def raw(self) -> sqlite3.Connection:
        return self._raw

    def __getattr__(self, name: str):
        return getattr(self._raw, name)

    def __enter__(self):
        self._raw.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._raw.__exit__(*exc_info)

    @contextlib.contextmanager
    def waiting_until(self, deadline: float):
        previous = getattr(self._local, "deadline", None)
        self._local.deadline = deadline
        try:
            yield
        finally:
            self._local.deadline = previous

    def _retry(self, operation: Callable[[], object]):
        deadline = getattr(self._local, "deadline", None)
        if deadline is None:
            deadline = self._monotonic() + self._default_wait
        while True:
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise
                if self._stop_event.is_set():
                    # 途中の transaction を残さない。停止は副作用なしの終端として返す。
                    with contextlib.suppress(Exception):
                        self._raw.rollback()
                    raise OpsError(ErrorCode.UNAVAILABLE) from None
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(self._poll_interval, remaining))

    def execute(self, sql: str, parameters=()):
        return self._retry(lambda: self._raw.execute(sql, parameters))

    def executemany(self, sql: str, parameters):
        rows = list(parameters)
        return self._retry(lambda: self._raw.executemany(sql, rows))

    def commit(self) -> None:
        self._retry(self._raw.commit)


def _authorize(caller: Caller, endpoint_code: str) -> None:
    """endpoint の認可は表の scope だけを参照する (§3.1)。"""
    assert_authorized(caller, ENDPOINTS[endpoint_code].scope)


def _error_response(code: ErrorCode | str) -> dict:
    return {"error": {"code": str(code)}}


def _decided_by(caller: Caller) -> str:
    # decided_by は認証済み主体だけから作る。本文の値は使わない。
    if caller is ShellCaller.SHELL:
        return "shell"
    return f"api:{caller.value}"


def _render_directive(text: str) -> str:
    return "- " + " ".join(text.split())


class OpsService:
    """ops レーン・決定レーン・job 表・監査・event 投影をまとめる HTTP 非依存入口。"""

    def __init__(self, conn: sqlite3.Connection, *, wall_clock: Callable[[], datetime],
                 monotonic: Callable[[], float] = time.monotonic,
                 decide_conn: sqlite3.Connection | None = None,
                 stop_event: threading.Event | None = None,
                 limits: OpsLimits = OpsLimits(),
                 policy_path: Path | None = None, state_store=None,
                 status_provider: Callable[[], dict] | None = None,
                 log_path: Path | None = None, activity_path: Path | None = None,
                 data_resume: Callable[[bool], dict] | None = None,
                 ask_submitter: Callable[[str], object] | None = None,
                 improve_supervisor=None, plugins_root: Path | None = None,
                 settings=None, activity_log=None) -> None:
        self.stop_event = stop_event or threading.Event()
        self._owns_decide_conn = decide_conn is None
        raw_decide = decide_conn if decide_conn is not None else self._open_peer(conn)
        self._conn = _PatientConnection(
            conn, stop_event=self.stop_event, monotonic=monotonic,
            default_wait=limits.request_deadline_seconds,
            poll_interval=limits.poll_interval_seconds)
        self._decide_conn = _PatientConnection(
            raw_decide, stop_event=self.stop_event, monotonic=monotonic,
            default_wait=limits.request_deadline_seconds,
            poll_interval=limits.poll_interval_seconds)
        conn = self._conn
        self.audit = AuditStore(conn, wall_clock=wall_clock)
        self.events = EventStore(conn, wall_clock=wall_clock)
        self._decide_audit = AuditStore(self._decide_conn, wall_clock=wall_clock)
        self._decide_events = EventStore(self._decide_conn, wall_clock=wall_clock)
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self.limits = limits
        self._policy_path = policy_path
        self._state_store = state_store
        self._status_provider = status_provider
        self._log_path = log_path
        self._activity_path = activity_path
        self._data_resume = data_resume
        self._ask_submitter = ask_submitter
        self._improve_supervisor = improve_supervisor
        self._plugins_root = plugins_root
        self._settings = settings
        self._activity_log = activity_log
        self.jobs = JobRegistry(monotonic=monotonic)
        self._ops_lock = threading.RLock()
        self._decide_lock = threading.Lock()
        self._decision_wakeup = threading.Event()
        self._decision_thread: threading.Thread | None = None
        self._worker_start_lock = threading.Lock()
        self._operation_threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()
        self._accepting = True
        self._rejections = SuppressedActivity(wall_clock=wall_clock,
                                              write=self._write_suppressed)

    @property
    def lane_connection(self):
        """ops レーンの接続 (deadline と停止合図に従う)。レーンの中で呼ぶ callback 用。"""
        return self._conn

    @staticmethod
    def _open_peer(conn: sqlite3.Connection) -> sqlite3.Connection:
        from agentic_fx.store import db
        row = conn.execute("PRAGMA database_list").fetchone()
        path = row[2] if row is not None else ""
        if not path:
            raise ValueError("decide lane needs a file-backed database")
        return db.connect(Path(path))

    # ------------------------------------------------------------------ lanes

    def _deadline(self, deadline: float | None) -> float:
        if deadline is not None:
            return deadline
        return self._monotonic() + self.limits.request_deadline_seconds

    def _foreign(self, deadline: float) -> float:
        """注入された monotonic の期限を、StateStore / plugin flock が使う
        ``time.monotonic`` の期限へ残り時間で写す。"""
        return time.monotonic() + (deadline - self._monotonic())

    def _sleep_until(self, deadline: float) -> None:
        remaining = deadline - self._monotonic()
        time.sleep(max(0.0, min(self.limits.poll_interval_seconds, remaining)))

    def _acquire(self, lock, deadline: float, setting: float) -> None:
        """non-blocking の反復取得。上限は min(設定値, caller deadline の残り)。"""
        give_up = min(deadline, self._monotonic() + setting)
        while not lock.acquire(blocking=False):
            if self.stop_event.is_set():
                raise OpsError(ErrorCode.UNAVAILABLE)
            if self._monotonic() >= give_up:
                raise OpsError(ErrorCode.REQUEST_TIMEOUT)
            self._sleep_until(give_up)

    @contextlib.contextmanager
    def _ops_lane(self, deadline: float):
        self._acquire(self._ops_lock, deadline, self.limits.ops_lock_wait_seconds)
        try:
            try:
                with self._conn.waiting_until(deadline):
                    yield
            except sqlite3.OperationalError as exc:
                # busy の待ちは要求の残り時間で打ち切られている。固定 code で返す。
                if not _is_busy(exc):
                    raise
                with contextlib.suppress(Exception):
                    self._conn.rollback()
                raise OpsError(ErrorCode.DATABASE_BUSY) from None
        finally:
            self._ops_lock.release()

    @contextlib.contextmanager
    def _decide_lane(self, deadline: float):
        self._acquire(self._decide_lock, deadline, self.limits.decision_deadline_seconds)
        try:
            with self._decide_conn.waiting_until(deadline):
                yield
        finally:
            self._decide_lock.release()

    def _ensure_accepting(self) -> None:
        if not self._accepting:
            raise OpsError(ErrorCode.UNAVAILABLE)

    def _restricted(self, caller: Caller, endpoint: Endpoint, deadline: float) -> bool:
        if not endpoint.autopilot_restricted or caller is ShellCaller.SHELL:
            return False
        return self._autopilot(deadline)

    def _autopilot(self, deadline: float) -> bool:
        if self._state_store is None:
            return False
        return bool(self._state_store.load(deadline=self._foreign(deadline),
                                           stop_event=self.stop_event).autopilot)

    @staticmethod
    def _map_exception(exc: BaseException) -> ErrorCode | None:
        from agentic_fx.store.state import StateLockCancelled, StateLockTimeout
        if isinstance(exc, (OpsError, _DecisionOutcome)):
            return exc.code
        if isinstance(exc, StateLockTimeout):
            return ErrorCode.REQUEST_TIMEOUT
        if isinstance(exc, StateLockCancelled):
            return ErrorCode.UNAVAILABLE
        if _is_busy(exc):
            return ErrorCode.DATABASE_BUSY
        return None

    # ------------------------------------------------------- audit helpers

    def _accept(self, caller: Caller, endpoint: Endpoint, body: dict, target: str,
                *, key: str | None, job_id: str | None = None,
                audit: AuditStore | None = None) -> AcceptedRequest | IdempotencyReplay:
        audit = audit or self.audit
        if endpoint.idempotent:
            return audit.accept_idempotent(
                endpoint=endpoint.route, principal=caller, asserted_actor=None, key=key,
                body=body, target_ref=target, job_id=job_id)
        return audit.accept(endpoint=endpoint.route, principal=caller, asserted_actor=None,
                            peer_pid=None, peer_exe=None, body=body, target_ref=target,
                            job_id=job_id)

    def _replay(self, replay: IdempotencyReplay) -> dict:
        error = replay.response.get("error")
        if isinstance(error, dict):
            raise OpsError(ErrorCode(error["code"]))
        if replay.in_flight and replay.job_id is not None:
            job = self.jobs.lookup(replay.job_id)
            state = job.state.value if job is not None else JobState.RUNNING.value
            return {"job_id": replay.job_id, "state": state}
        return replay.response

    def _terminal(self, accepted: AcceptedRequest, caller: Caller, endpoint: Endpoint,
                  code: str, response: dict, *, audit: AuditStore | None = None,
                  events: EventStore | None = None) -> None:
        audit = audit or self.audit
        conn = audit.connection
        waiting = getattr(conn, "waiting_until", None)
        # 副作用は済んでいるので、要求 deadline を使い切っていても終端は独立の予算で書く。
        budget = (waiting(self._monotonic() + self.limits.terminal_write_seconds)
                  if waiting is not None else contextlib.nullcontext())
        with budget:
            audit.terminal(accepted.audit_id, code=code, response=response)
            self._project(accepted.audit_id, caller, endpoint, code, events=events)

    def _project(self, audit_id: int, caller: Caller, endpoint: Endpoint, code: str, *,
                 events: EventStore | None = None) -> None:
        (events or self.events).emit_best_effort("ops_request_terminal", f"audit:{audit_id}", {
            "endpoint_code": endpoint.code, "result_code": code,
            "authenticated_principal": caller.value})
        if caller is not ShellCaller.SHELL:
            # シェルは従来の activity 行を自分で書くので、ここでは重ねない。
            self._activity("SYSTEM", f"ops_{endpoint.code}", f"audit={audit_id} result={code}")

    def _activity(self, category: str, event: str, summary: str) -> None:
        if self._activity_log is None:
            return
        try:
            from agentic_fx.activity import Category
            self._activity_log.write(Category(category), event, summary)
        except Exception:
            # activity は人向け投影。失敗しても要求は成功させる。
            _log.warning("ops activity projection failed", exc_info=True)

    def _write_suppressed(self, category: str, event: str, summary: str) -> None:
        self._activity(category, event, summary)

    def _mutate(self, caller: Caller, endpoint: Endpoint, body: dict, target: str,
                action: Callable[[float, AcceptedRequest], dict], *, key: str | None = None,
                deadline: float | None = None) -> dict:
        """同期の変更要求。accepted を commit してから副作用、最後に終端行。"""
        self._ensure_accepting()
        deadline = self._deadline(deadline)
        with self._ops_lane(deadline):
            # lane 待ちの間に停止が始まっていれば、accepted を書かずに断る。
            self._ensure_accepting()
            accepted = self._accept(caller, endpoint, body, target, key=key)
            if isinstance(accepted, IdempotencyReplay):
                return self._replay(accepted)
            try:
                if self._restricted(caller, endpoint, deadline):
                    raise OpsError(ErrorCode.AUTOPILOT_RESTRICTED)
                response = action(deadline, accepted)
            except BaseException as exc:
                code = self._map_exception(exc)
                self._terminal(accepted, caller, endpoint,
                               (code or ErrorCode.INTERNAL).value,
                               _error_response(code or ErrorCode.INTERNAL))
                if code is not None and not isinstance(exc, OpsError):
                    raise OpsError(code) from None
                raise
            self._terminal(accepted, caller, endpoint, "ok", response)
            return response

    # ------------------------------------------------------------------ reads

    def whoami(self, principal: Principal) -> dict:
        _authorize(principal, "whoami")
        return {"authenticated_principal": principal.value, "asserted_actor": None,
                "scopes": sorted(scope.value for scope in PRINCIPAL_SCOPES[principal])}

    def status(self, principal: Caller, *, deadline: float | None = None) -> dict:
        _authorize(principal, "status")
        deadline = self._deadline(deadline)
        data = dict(self._status_provider() if self._status_provider is not None else {})
        if self._state_store is not None:
            try:
                state = self._state_store.load(deadline=self._foreign(deadline), stop_event=self.stop_event)
                marker = self._state_store.reconcile_marker()
                kill_switch = {"latched": state.kill_switch_latched,
                               "generation": state.kill_switch_generation,
                               "latched_at": state.kill_switch_latched_at,
                               "reconcile_required": marker is not None}
                if marker is not None:
                    raw = self._state_store.file_value(deadline=self._foreign(deadline),
                                                       stop_event=self.stop_event)
                    kill_switch.update({"requested_generation": marker.get("requested_generation"),
                                        "started_at": marker.get("started_at"),
                                        "file_latched": raw.kill_switch_latched})
            except BaseException as exc:
                code = self._map_exception(exc)
                if code is None or isinstance(exc, OpsError):
                    raise
                raise OpsError(code) from None
            data.update({"mode": state.mode.value, "autopilot": state.autopilot,
                         "kill_switch": kill_switch})
        return data

    def log(self, principal: Caller, limit: int = 20) -> dict:
        _authorize(principal, "log")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        return self._tail(self._log_path, limit, None)

    def activity(self, principal: Caller, limit: int = 20,
                 category: str | None = None) -> dict:
        _authorize(principal, "activity")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        try:
            keep = activity_filter(category)
        except ValueError:
            raise OpsError(ErrorCode.INVALID_ARGUMENT) from None
        return self._tail(self._activity_path, limit, keep)

    @staticmethod
    def _tail(path: Path | None, limit: int, keep) -> dict:
        if path is None:
            return {"lines": [], "truncated": False}
        result = tail_lines(path, limit, keep=keep)
        return {"lines": result.lines, "truncated": result.truncated}

    def approval_list(self, principal: Caller, *, limit: int = 20,
                      deadline: float | None = None) -> dict:
        _authorize(principal, "approval_list")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        with self._ops_lane(self._deadline(deadline)):
            rows = self._conn.execute(
                "SELECT id,kind,payload_json,created_at FROM approval_requests "
                "WHERE status='pending' ORDER BY id LIMIT ?", (limit,)).fetchall()
            journals = self._conn.execute(
                "SELECT approval_id,name,phase FROM plugin_switch_journal "
                "WHERE phase NOT IN ('decided','reverted') ORDER BY op_id").fetchall()
        from agentic_fx.loops.approval_facts import display_text
        approvals = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, RecursionError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            approvals.append({"id": row["id"], "kind": display_text(row["kind"]),
                              "name": display_text(payload.get("name")),
                              "content_hash": display_text(payload.get("content_hash")),
                              "created_at": row["created_at"]})
        return {"approvals": approvals,
                "open_journals": [{"approval_id": j["approval_id"],
                                   "name": display_text(j["name"]), "phase": j["phase"]}
                                  for j in journals]}

    def approval_detail(self, principal: Caller, approval_id: int, *,
                        deadline: float | None = None) -> dict:
        _authorize(principal, "approval_detail")
        with self._ops_lane(self._deadline(deadline)):
            row = self._conn.execute(
                "SELECT kind,status,payload_json,reason,decided_by,decided_at "
                "FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise OpsError(ErrorCode.NOT_FOUND)
        from agentic_fx.loops import approval_facts
        from agentic_fx.loops.approval_facts import display_text
        from agentic_fx.plugin.switch import payload_sha256
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, RecursionError):
            payload = None
        if not isinstance(payload, dict):
            raise OpsError(ErrorCode.INVALID_STATE)
        return {"id": approval_id, "kind": display_text(row["kind"]),
                "status": row["status"],
                "parent_facts": payload.get("parent_facts"),
                "agent_claims": payload.get("agent_claims"),
                "facts_lines": approval_facts.render_facts_lines(payload),
                "in_sample": payload.get("in_sample"), "holdout": payload.get("holdout"),
                "payload_sha256": payload_sha256(row["payload_json"]),
                "reason": (None if row["reason"] is None else
                           display_text(row["reason"], approval_facts.CLAIM_DISPLAY_LIMIT)),
                "decided_by": row["decided_by"], "decided_at": row["decided_at"],
                "asserted_actor": None}

    def events_after(self, principal: Caller, *, after: int, limit: int,
                     deadline: float | None = None) -> dict:
        _authorize(principal, "events")
        with self._ops_lane(self._deadline(deadline)):
            page = self.events.poll(after=after, limit=limit)
        return {"events": page.events, "high_watermark": page.high_watermark}

    def reflection_status(self, principal: Caller, order_id: int, *,
                          deadline: float | None = None) -> dict:
        _authorize(principal, "reflection_status")
        with self._ops_lane(self._deadline(deadline)):
            return self._reflection_status(order_id)

    def _reflection_status(self, order_id: int) -> dict:
        row = self._conn.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise OpsError(ErrorCode.NOT_FOUND)
        attempt = self._conn.execute(
            "SELECT attempts,last_attempt_at FROM reflection_attempts WHERE order_id=?",
            (order_id,)).fetchone()
        reflection = self._conn.execute(
            "SELECT 1 FROM reflections WHERE order_id=?", (order_id,)).fetchone()
        return {"order_id": order_id, "status": row["status"],
                "reflected": reflection is not None,
                "attempts": 0 if attempt is None else attempt["attempts"],
                "last_attempt_at": None if attempt is None else attempt["last_attempt_at"]}

    def get_job(self, principal: Caller, job_id: str) -> dict:
        _authorize(principal, "job_get")
        return self._job_view(self.jobs.get(job_id, principal, None))

    @staticmethod
    def _job_view(job: Job) -> dict:
        return {"id": job.id, "kind": job.kind, "state": job.state.value,
                "result": job.result, "result_code": job.error_code}

    # ------------------------------------------------------- sync mutations

    def add_policy(self, principal: Caller, idempotency_key: str, text: str, *,
                   deadline: float | None = None) -> dict:
        """canonical record を正にして policy file を毎回再生成する。"""
        _authorize(principal, "policy")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)

        def action(_deadline: float, accepted: AcceptedRequest) -> dict:
            request_id = accepted.request_id
            self._conn.execute(
                "INSERT INTO ops_policies(request_id,directive,created_at) VALUES (?,?,?)",
                (request_id, text, self._wall_clock().isoformat()))
            self._conn.commit()
            self._regenerate_policy()
            # 既存運用との互換で、policy_added だけは先頭 200 文字を残す (§8 の例外)。
            self._activity("SYSTEM", "policy_added", text[:200])
            return {"id": request_id}

        return self._mutate(principal, ENDPOINTS["policy"], {"text": text}, "policy", action,
                            key=idempotency_key, deadline=deadline)

    def regenerate_policy(self) -> None:
        """起動時などに canonical record から policy file を作り直す。"""
        with self._ops_lane(self._deadline(None)):
            self._regenerate_policy()

    def _regenerate_policy(self) -> None:
        if self._policy_path is None:
            return
        rendered = [_render_directive(row["directive"]) for row in self._conn.execute(
            "SELECT directive FROM ops_policies ORDER BY id").fetchall()]
        owned = set(rendered)
        try:
            existing = self._policy_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            existing = []
        # 利用者が手で書いた行は残し、record 由来の行だけを record 集合から作り直す。
        kept = [line for line in existing if line not in owned]
        payload = "".join(f"{line}\n" for line in kept + rendered).encode()
        self._policy_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=self._policy_path.name + ".",
                                         suffix=".tmp", dir=self._policy_path.parent)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self._policy_path)
            directory = os.open(self._policy_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise

    def add_backlog(self, principal: Caller, idempotency_key: str, idea: str, *,
                    deadline: float | None = None) -> dict:
        _authorize(principal, "backlog_add")
        if not isinstance(idea, str) or not 1 <= len(idea) <= 2000 or not idea.strip():
            raise OpsError(ErrorCode.INVALID_ARGUMENT)

        def action(_deadline: float, _accepted: AcceptedRequest) -> dict:
            from agentic_fx.store import backlog
            return {"id": backlog.add(self._conn, idea, "user", self._wall_clock())}

        return self._mutate(principal, ENDPOINTS["backlog_add"], {"idea": idea}, "backlog",
                            action, key=idempotency_key, deadline=deadline)

    def transition_backlog(self, principal: Caller, backlog_id: int, action_name: str, *,
                           deadline: float | None = None) -> dict:
        # 遷移名から endpoint を決めないと認可の scope を表から引けない。
        if not isinstance(action_name, str) or action_name not in _BACKLOG_TRANSITIONS:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        _authorize(principal, f"backlog_{action_name}")
        if type(backlog_id) is not int:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        allowed, target, result = _BACKLOG_TRANSITIONS[action_name]

        def action(_deadline: float, _accepted: AcceptedRequest) -> dict:
            from agentic_fx.store import backlog
            now = self._wall_clock()
            # note からの reopen は従来どおり human_reopened として区別する。
            changed = action_name == "reopen" and backlog.transition(
                self._conn, backlog_id, allowed_from=("note",), target=target,
                last_result="human_reopened", now=now)
            if not changed and not backlog.transition(
                    self._conn, backlog_id, allowed_from=allowed, target=target,
                    last_result=result, now=now):
                row = self._conn.execute(
                    "SELECT 1 FROM improvement_backlog WHERE id=?", (backlog_id,)).fetchone()
                raise OpsError(ErrorCode.INVALID_STATE if row is not None
                               else ErrorCode.NOT_FOUND)
            return {"id": backlog_id, "status": target}

        return self._mutate(principal, ENDPOINTS[f"backlog_{action_name}"],
                            {"id": backlog_id}, f"backlog:{backlog_id}", action,
                            deadline=deadline)

    def retry_reflection(self, principal: Caller, idempotency_key: str, order_id: int,
                         *, expected_attempts: int, expected_last_attempt_at: str | None,
                         deadline: float | None = None) -> dict:
        """試行記録の識別子が台帳の現在値と一致するときだけ clear する。"""
        _authorize(principal, "reflection_retry")
        if type(order_id) is not int or type(expected_attempts) is not int \
                or expected_attempts < 0 \
                or not (expected_last_attempt_at is None
                        or isinstance(expected_last_attempt_at, str)) \
                or (expected_attempts == 0) != (expected_last_attempt_at is None):
            raise OpsError(ErrorCode.INVALID_ARGUMENT)

        def action(_deadline: float, _accepted: AcceptedRequest) -> dict:
            current = self._reflection_status(order_id)
            if current["status"] != "closed" or current["reflected"]:
                raise OpsError(ErrorCode.INVALID_STATE)
            if (current["attempts"], current["last_attempt_at"]) != (
                    expected_attempts, expected_last_attempt_at):
                raise OpsError(ErrorCode.ATTEMPT_CHANGED)
            if expected_attempts:
                cur = self._conn.execute(
                    "DELETE FROM reflection_attempts WHERE order_id=? AND attempts=? "
                    "AND last_attempt_at=?",
                    (order_id, expected_attempts, expected_last_attempt_at))
                if cur.rowcount != 1:
                    self._conn.rollback()
                    raise OpsError(ErrorCode.ATTEMPT_CHANGED)
                self._conn.commit()
            return {"order_id": order_id, "requeued": True}

        return self._mutate(
            principal, ENDPOINTS["reflection_retry"],
            {"order_id": order_id, "expected_attempts": expected_attempts,
             "expected_last_attempt_at": expected_last_attempt_at},
            f"reflection:{order_id}", action, key=idempotency_key, deadline=deadline)

    def resume_data(self, principal: Caller, *, acknowledge: bool,
                    deadline: float | None = None) -> dict:
        _authorize(principal, "data_resume")
        if type(acknowledge) is not bool:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)

        def action(_deadline: float, _accepted: AcceptedRequest) -> dict:
            if self._data_resume is None:
                raise OpsError(ErrorCode.INVALID_STATE)
            response = self._data_resume(acknowledge)
            self._guard_event("data_feed", "resume_requested", None)
            return response

        return self._mutate(principal, ENDPOINTS["data_resume"],
                            {"acknowledge": acknowledge}, "data", action, deadline=deadline)

    def reset_kill_switch(self, principal: Caller, expected_generation: int, *,
                          deadline: float | None = None) -> dict:
        _authorize(principal, "killswitch_reset")
        if type(expected_generation) is not int or expected_generation < 0:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)

        def action(deadline_: float, _accepted: AcceptedRequest) -> dict:
            from agentic_fx.store.state import (GenerationMismatch, NotLatched,
                                                ResetNotApplied, StateUncertain)
            if self._state_store is None:
                raise OpsError(ErrorCode.INVALID_STATE)
            try:
                state = self._state_store.reset_kill_switch(
                    expected_generation, deadline=self._foreign(deadline_), stop_event=self.stop_event)
            except GenerationMismatch:
                raise OpsError(ErrorCode.GENERATION_MISMATCH) from None
            except NotLatched:
                raise OpsError(ErrorCode.NOT_LATCHED) from None
            except StateUncertain:
                raise OpsError(ErrorCode.INVALID_STATE) from None
            except ResetNotApplied:
                # 保存に失敗してラッチは残った。解除は適用されていない。
                raise OpsError(ErrorCode.UNAVAILABLE) from None
            self._guard_event("kill_switch", "released", state.kill_switch_generation)
            return {"latched": state.kill_switch_latched,
                    "generation": state.kill_switch_generation}

        return self._mutate(principal, ENDPOINTS["killswitch_reset"],
                            {"expected_generation": expected_generation}, "killswitch",
                            action, deadline=deadline)

    def reconcile_kill_switch(self, principal: Caller, *,
                              deadline: float | None = None) -> dict:
        """解除の途中で止まった印を、ラッチ中として確定する。解除側には倒さない。"""
        _authorize(principal, "killswitch_reconcile")

        def action(deadline_: float, _accepted: AcceptedRequest) -> dict:
            if self._state_store is None or self._state_store.reconcile_marker() is None:
                raise OpsError(ErrorCode.INVALID_STATE)
            state = self._state_store.confirm_latched(deadline=self._foreign(deadline_),
                                                      stop_event=self.stop_event)
            self._guard_event("kill_switch", "latched", state.kill_switch_generation)
            return {"latched": state.kill_switch_latched,
                    "generation": state.kill_switch_generation}

        return self._mutate(principal, ENDPOINTS["killswitch_reconcile"], {}, "killswitch",
                            action, deadline=deadline)

    def cancel_job(self, principal: Caller, job_id: str, *,
                   deadline: float | None = None) -> dict:
        _authorize(principal, "job_cancel")
        if not isinstance(job_id, str) or not job_id:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)

        def action(_deadline: float, _accepted: AcceptedRequest) -> dict:
            job = self.jobs.cancel(job_id, principal, None)
            if job.state is JobState.CANCELLED and job.audit_id is not None:
                self._finish_job_audit(job, self.audit, self.events)
            self._job_event(job, self.events)
            return {"id": job.id, "state": job.state.value}

        return self._mutate(principal, ENDPOINTS["job_cancel"], {"job_id": job_id},
                            f"job:{job_id}", action, deadline=deadline)

    def _guard_event(self, guard_kind: str, state: str, generation: int | None) -> None:
        self.events.emit_best_effort("guard_state_changed", "instance", {
            "guard_kind": guard_kind, "state": state, "generation": generation})

    # -------------------------------------------------- ask / improve jobs

    def ask(self, principal: Caller, idempotency_key: str, question: str, *,
            deadline: float | None = None) -> dict:
        """mission slot を受理時に原子的に取り、取れなければ job を作らず 409。"""
        _authorize(principal, "ask")
        if not isinstance(question, str) or not 1 <= len(question) <= 4000 \
                or not question.strip():
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        endpoint = ENDPOINTS["ask"]
        self._ensure_accepting()
        deadline = self._deadline(deadline)
        job_id = new_job_id()
        with self._ops_lane(deadline):
            self._ensure_accepting()
            accepted = self._accept(principal, endpoint, {"question": question}, "ask",
                                    key=idempotency_key, job_id=job_id)
            if isinstance(accepted, IdempotencyReplay):
                return self._replay(accepted)
            try:
                if self._ask_submitter is None:
                    raise OpsError(ErrorCode.INVALID_STATE)
                if self.jobs.has_active("ask"):
                    raise OpsError(ErrorCode.MISSION_BUSY)
                future = self._ask_submitter(question)
                if future is None:
                    raise OpsError(ErrorCode.MISSION_BUSY)
                job = self.jobs.enqueue_single(
                    principal, None, "ask", self._ask_action(future),
                    deadline_seconds=self.limits.ask_deadline_seconds,
                    busy_code=ErrorCode.MISSION_BUSY, job_id=job_id,
                    audit_id=accepted.audit_id)
            except BaseException as exc:
                code = self._map_exception(exc) or ErrorCode.INTERNAL
                self._terminal(accepted, principal, endpoint, code.value,
                               _error_response(code))
                raise
            self._start_thread(job)
            return {"job_id": job.id, "state": job.state.value}

    def _ask_action(self, future) -> Callable[[Job], dict]:
        def run(job: Job) -> dict:
            while True:
                if self.stop_event.is_set():
                    raise OpsError(ErrorCode.UNAVAILABLE)
                remaining = job.deadline_mono - self._monotonic()
                if remaining <= 0:
                    raise OpsError(ErrorCode.REQUEST_TIMEOUT)
                try:
                    answer = future.result(timeout=min(0.1, remaining))
                except concurrent.futures.TimeoutError:
                    continue
                return {"answer": answer}
        return run

    def improve(self, principal: Caller, idempotency_key: str, *,
                deadline: float | None = None) -> dict:
        _authorize(principal, "improve")
        endpoint = ENDPOINTS["improve"]
        self._ensure_accepting()
        deadline = self._deadline(deadline)
        job_id = new_job_id()
        with self._ops_lane(deadline):
            self._ensure_accepting()
            accepted = self._accept(principal, endpoint, {}, "improve",
                                    key=idempotency_key, job_id=job_id)
            if isinstance(accepted, IdempotencyReplay):
                return self._replay(accepted)
            try:
                if self._improve_supervisor is None:
                    raise OpsError(ErrorCode.INVALID_STATE)
                if self._restricted(principal, endpoint, deadline):
                    raise OpsError(ErrorCode.AUTOPILOT_RESTRICTED)
                job = self.jobs.enqueue_single(
                    principal, None, "improve", self._improve_action,
                    deadline_seconds=self.limits.improve_start_deadline_seconds,
                    busy_code=ErrorCode.IMPROVE_RUNNING, job_id=job_id,
                    audit_id=accepted.audit_id)
            except BaseException as exc:
                code = self._map_exception(exc) or ErrorCode.INTERNAL
                self._terminal(accepted, principal, endpoint, code.value,
                               _error_response(code))
                raise
            self._start_thread(job)
            return {"job_id": job.id, "state": job.state.value}

    def _improve_action(self, job: Job) -> dict:
        # 開始直前に StateStore の flock 内で autopilot を読み直す (§5)。
        if job.authenticated_principal is not ShellCaller.SHELL \
                and self._autopilot_at_start(job):
            raise OpsError(ErrorCode.AUTOPILOT_RESTRICTED)
        if self.stop_event.is_set():
            raise OpsError(ErrorCode.UNAVAILABLE)

        def prepared(mission_id: int) -> None:
            job.result = {"mission_id": mission_id}

        mission_id = self._improve_supervisor.submit_manual(on_prepared=prepared)
        return {"mission_id": mission_id}

    def _start_thread(self, job: Job) -> None:
        thread = threading.Thread(target=self._run_single, args=(job,),
                                  name=f"afx-ops-{job.kind}", daemon=True)
        with self._threads_lock:
            self._operation_threads.add(thread)
        thread.start()

    def _run_single(self, job: Job) -> None:
        try:
            try:
                result = job.action(job)
            except BaseException as exc:
                code = self._map_exception(exc) or ErrorCode.INTERNAL
                if code is ErrorCode.INTERNAL:
                    _log.exception("ops job failed kind=%s", job.kind)
                state = (JobState.SHUTDOWN if code is ErrorCode.UNAVAILABLE
                         and self.stop_event.is_set() else JobState.FAILED)
                self.jobs.finish(job, state=state, error_code=code.value, result=job.result)
            else:
                self.jobs.finish(job, state=JobState.DONE, result=result)
            self._close_job_with_ops_lane(job)
        finally:
            with self._threads_lock:
                self._operation_threads.discard(threading.current_thread())

    def _close_job_with_ops_lane(self, job: Job) -> None:
        try:
            with self._ops_lane(self._monotonic() + self.limits.request_deadline_seconds):
                self._finish_job_audit(job, self.audit, self.events)
                self._job_event(job, self.events)
        except Exception:
            # 終端行を書けなければ accepted のまま残り、次の起動時回復が
            # outcome_unknown を追記する。
            _log.warning("ops job terminal audit failed job=%s", job.id, exc_info=True)

    def _finish_job_audit(self, job: Job, audit: AuditStore, events: EventStore) -> None:
        if job.audit_id is None:
            return
        code = "ok" if job.state is JobState.DONE else (job.error_code or job.state.value)
        response = {"job_id": job.id, "state": job.state.value, "result_code": code}
        if audit.terminal(job.audit_id, code=code, response=response):
            self._project(job.audit_id, job.authenticated_principal,
                          ENDPOINTS[job.kind], code, events=events)

    def _job_event(self, job: Job, events: EventStore) -> None:
        events.emit_best_effort("job_state_changed", f"job:{job.id}", {
            "job_kind": job.kind, "state": job.state.value,
            "result_code": "ok" if job.state is JobState.DONE else job.error_code})

    def _autopilot_at_start(self, job: Job) -> bool:
        if self._state_store is None:
            return False
        return bool(self._state_store.autopilot_at_start(
            deadline=self._foreign(job.deadline_mono), stop_event=self.stop_event))

    # ------------------------------------------------------------- decisions

    def approve(self, principal: Caller, approval_id: int, payload_sha256: str, *,
                deadline: float | None = None) -> dict:
        return self._submit_decision(principal, "approve", approval_id, payload_sha256,
                                     None, deadline)

    def reject(self, principal: Caller, approval_id: int, reason: str | None = None, *,
               deadline: float | None = None) -> dict:
        return self._submit_decision(principal, "reject", approval_id, None, reason, deadline)

    def retry(self, principal: Caller, approval_id: int, payload_sha256: str, *,
              deadline: float | None = None) -> dict:
        return self._submit_decision(principal, "retry", approval_id, payload_sha256,
                                     None, deadline)

    def _submit_decision(self, caller: Caller, kind: str, approval_id: int,
                         digest: str | None, reason: str | None,
                         deadline: float | None) -> dict:
        if kind not in _DECISION_KINDS:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        _authorize(caller, kind)
        if type(approval_id) is not int:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        if kind != "reject" and (not isinstance(digest, str) or len(digest) != 64):
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        if reason is not None and (not isinstance(reason, str) or len(reason) > 2000):
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        endpoint = ENDPOINTS[kind]
        self._ensure_accepting()
        deadline = self._deadline(deadline)
        job_id = new_job_id()
        # reject reason は approval_requests.reason だけに残す。監査本文には入れない。
        body = {"approval_id": approval_id}
        if digest is not None:
            body["payload_sha256"] = digest
        with self._ops_lane(deadline):
            self._ensure_accepting()
            accepted = self._accept(caller, endpoint, body, f"approval:{approval_id}",
                                    key=None, job_id=job_id)
            try:
                self._preflight_decision(self._conn, approval_id, kind, digest)
                if self._restricted(caller, endpoint, deadline):
                    raise OpsError(ErrorCode.AUTOPILOT_RESTRICTED)
                action = self._decision_action(kind, approval_id, digest, reason,
                                               _decided_by(caller))
                job = self.jobs.enqueue_decision(
                    caller, None, kind, action,
                    deadline_seconds=self.limits.decision_deadline_seconds,
                    target=f"approval:{approval_id}", job_id=job_id,
                    audit_id=accepted.audit_id)
            except BaseException as exc:
                code = self._map_exception(exc) or ErrorCode.INTERNAL
                self._terminal(accepted, caller, endpoint, code.value, _error_response(code))
                raise
        self.start_decision_worker()
        self._decision_wakeup.set()
        return {"job_id": job.id, "state": job.state.value}

    @staticmethod
    def _preflight_decision(conn: sqlite3.Connection, approval_id: int, kind: str,
                            digest: str | None) -> None:
        from agentic_fx.plugin.switch import payload_sha256
        row = conn.execute(
            "SELECT kind,status,payload_json FROM approval_requests WHERE id=?",
            (approval_id,)).fetchone()
        if row is None:
            raise OpsError(ErrorCode.NOT_FOUND)
        if row["kind"] != "plugin":
            raise OpsError(ErrorCode.UNSUPPORTED_KIND)
        if row["status"] != "pending":
            raise OpsError(ErrorCode.ALREADY_DECIDED)
        if kind != "reject" and payload_sha256(row["payload_json"]) != digest:
            raise OpsError(ErrorCode.PAYLOAD_CHANGED)

    def _decision_action(self, kind: str, approval_id: int, digest: str | None,
                         reason: str | None, decided_by: str) -> Callable[[Job], dict]:
        def run(job: Job) -> dict:
            from agentic_fx.plugin import switch
            from agentic_fx.store.approvals import AlreadyDecidedError, ApprovalNotFoundError
            if self._plugins_root is None or self._settings is None:
                raise OpsError(ErrorCode.INVALID_STATE)
            # 実行時の kind / digest 照合。受理時の照合は _preflight_decision、
            # lock 内の照合は switch 側が行う。
            self._preflight_decision(self._decide_conn, approval_id, kind, digest)
            flock_deadline = min(job.deadline_mono,
                                 self._monotonic() + self.limits.plugin_lock_wait_seconds)
            try:
                if kind == "reject":
                    decided = switch.reject_candidate(
                        self._decide_conn, approval_id, decided_by=decided_by,
                        reason=reason or "", now=self._wall_clock(),
                        plugins_root=self._plugins_root, activity=self._activity_log,
                        deadline=self._foreign(flock_deadline), stop_event=self.stop_event,
                        require_plugin_kind=True)
                    if not decided:
                        raise OpsError(ErrorCode.INVALID_STATE)
                    outcome = None
                    result = {"approval_id": approval_id, "outcome": "rejected",
                              "status": "rejected"}
                else:
                    outcome = switch.approve_candidate(
                        self._decide_conn, approval_id, decided_by=decided_by,
                        now=self._wall_clock(), plugins_root=self._plugins_root,
                        settings=self._settings, activity=self._activity_log,
                        deadline=self._foreign(flock_deadline), stop_event=self.stop_event,
                        require_plugin_kind=True, expected_payload_sha256=digest)
                    result = {"approval_id": approval_id, "outcome": outcome.outcome,
                              "status": outcome.status}
            except switch.PluginBusyError:
                raise OpsError(ErrorCode.UNAVAILABLE if self.stop_event.is_set()
                               else ErrorCode.PLUGIN_BUSY) from None
            except switch.ApprovalKindUnsupported:
                raise OpsError(ErrorCode.UNSUPPORTED_KIND) from None
            except switch.ApprovalPayloadChanged:
                raise OpsError(ErrorCode.PAYLOAD_CHANGED) from None
            except ApprovalNotFoundError:
                raise OpsError(ErrorCode.NOT_FOUND) from None
            except AlreadyDecidedError:
                raise OpsError(ErrorCode.ALREADY_DECIDED) from None
            job.detail = outcome
            if outcome is not None and outcome.outcome == "already_decided":
                raise _DecisionOutcome(ErrorCode.ALREADY_DECIDED, result, outcome)
            if outcome is not None and outcome.outcome not in _DEPLOYED:
                raise _DecisionOutcome(ErrorCode.INVALID_STATE, result, outcome)
            self._decide_events.emit_best_effort(
                "approval_state_changed", f"approval:{approval_id}",
                {"state": result["status"], "decision_code": kind, "decided_by": decided_by})
            return result
        return run

    def start_decision_worker(self) -> None:
        """決定 worker を 1 本だけ起動する。何度呼んでもよい。"""
        with self._worker_start_lock:
            if self._decision_thread is not None and self._decision_thread.is_alive():
                return
            if self.stop_event.is_set():
                return
            self._decision_thread = threading.Thread(target=self._decision_loop,
                                                     name="afx-ops-decide", daemon=True)
            self._decision_thread.start()

    def _decision_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.run_next_decision() is None:
                self._decision_wakeup.wait(0.05)
                self._decision_wakeup.clear()

    def run_next_decision(self) -> Job | None:
        """単一 decide worker が呼ぶ一回分の FIFO 実行。"""
        job = self.jobs.take_next_decision()
        if job is None:
            return None
        try:
            self._execute_decision(job)
        except BaseException as exc:
            code = self._map_exception(exc) or ErrorCode.INTERNAL
            if code is ErrorCode.INTERNAL:
                _log.exception("decision job failed kind=%s", job.kind)
            result, detail = None, job.detail
            if isinstance(exc, _DecisionOutcome):
                result, detail = exc.result, exc.outcome
            state = (JobState.SHUTDOWN if code is ErrorCode.UNAVAILABLE
                     and self.stop_event.is_set() else JobState.FAILED)
            self.jobs.finish(job, state=state, error_code=code.value, result=result,
                             detail=detail)
        self._close_decision(job)
        return job

    def _execute_decision(self, job: Job) -> None:
        if self.stop_event.is_set():
            raise OpsError(ErrorCode.UNAVAILABLE)
        if self._monotonic() >= job.deadline_mono:
            raise OpsError(ErrorCode.REQUEST_TIMEOUT)
        with self._decide_lane(job.deadline_mono):
            # 開始直前に StateStore の flock 内で autopilot を読み直す。切替が先なら
            # ここで止まり、ここが先なら切替前に始まった決定として終端まで進む。
            if job.kind in ("approve", "retry") \
                    and job.authenticated_principal is not ShellCaller.SHELL \
                    and self._autopilot_at_start(job):
                raise OpsError(ErrorCode.AUTOPILOT_RESTRICTED)
            result = job.action(job)
        self.jobs.finish(job, state=JobState.DONE, result=result, detail=job.detail)

    def _close_decision(self, job: Job) -> None:
        try:
            self._finish_job_audit(job, self._decide_audit, self._decide_events)
            self._job_event(job, self._decide_events)
        except Exception:
            _log.warning("decision terminal audit failed job=%s", job.id, exc_info=True)

    def decide_from_shell(self, kind: str, approval_id: int, reason: str | None = None,
                          *, timeout: float | None = None) -> Job:
        """対話シェルの approve / reject / retry。API と同じ決定レーンを通る。

        表示中の payload を受理時の digest とし、job の終端まで待って返す。
        """
        digest = None
        if kind != "reject":
            from agentic_fx.plugin.switch import payload_sha256
            with self._ops_lane(self._deadline(None)):
                row = self._conn.execute(
                    "SELECT payload_json FROM approval_requests WHERE id=?",
                    (approval_id,)).fetchone()
            if row is None:
                raise OpsError(ErrorCode.NOT_FOUND)
            digest = payload_sha256(row["payload_json"])
        reply = self._submit_decision(ShellCaller.SHELL, kind, approval_id, digest, reason,
                                      None)
        job = self.jobs.lookup(reply["job_id"])
        wait = timeout if timeout is not None else self.limits.decision_deadline_seconds + 5.0
        if job is None or not job.done.wait(wait):
            raise OpsError(ErrorCode.REQUEST_TIMEOUT)
        return job

    def wait_for_job(self, job_id: str, *, timeout: float) -> dict:
        job = self.jobs.lookup(job_id)
        if job is None:
            raise OpsError(ErrorCode.NOT_FOUND)
        if not job.done.wait(timeout):
            raise TimeoutError(job_id)
        return self._job_view(job)

    # -------------------------------------------------------------- lifecycle

    def record_rejection(self, category: str) -> None:
        """認証失敗・peer 拒否・上限拒否の記録。分類ごとに 1 分 5 件まで個別に残す。"""
        self._rejections.record(category)

    def flush_rejections(self) -> None:
        """30 秒周期で呼ぶ。分境界を越えていれば抑止件数を書き出す。"""
        self._rejections.flush_due()

    def shutdown(self, *, join_timeout: float | None = None) -> None:
        """listener を閉じた後に呼ぶ停止順序の ops 側部分。

        (1) 新規受付を止める → (2) queued job を shutdown 終端にする →
        (3) running job に停止合図を渡す → (4) thread を bounded join する。
        """
        budget = self.limits.shutdown_join_seconds if join_timeout is None else join_timeout
        deadline = self._monotonic() + max(0.0, budget)
        self._accepting = False
        for job in self.jobs.shutdown_queued():
            try:
                with self._ops_lane(deadline):
                    self._finish_job_audit(job, self.audit, self.events)
                    self._job_event(job, self.events)
            except Exception:
                _log.warning("shutdown terminal audit failed job=%s", job.id, exc_info=True)
        self.stop_event.set()
        self._decision_wakeup.set()
        threads = []
        if self._decision_thread is not None:
            threads.append(self._decision_thread)
        with self._threads_lock:
            threads.extend(self._operation_threads)
        for thread in threads:
            if thread is threading.current_thread():
                continue
            thread.join(max(0.0, deadline - self._monotonic()))
        try:
            self._rejections.shutdown()
        except Exception:
            _log.warning("rejection flush failed at shutdown", exc_info=True)
        worker = self._decision_thread
        if self._owns_decide_conn and (worker is None or not worker.is_alive()):
            # 自分で開いた決定用接続は、worker が抜けた後に閉じる (fd を残さない)。
            with contextlib.suppress(Exception):
                self._decide_conn.close()
            self._owns_decide_conn = False

    def recover_after_journal(self) -> list[int]:
        """起動時回復。plugin 切替 journal の reconcile の後、listener を開く前に呼ぶ。

        決定の結果の正は approval_requests と切替 journal であり、ここはそれを読んで
        終端行を追記するだけで、副作用を再実行しない。journal reconcile が
        ``system_reconcile`` で完遂した決定はその結果を、journal を巻き戻した決定は
        未決定のままであることを追記する。それ以外の accepted-only は全て
        ``outcome_unknown`` にする (成功らしく見えても推測で success にしない)。
        """
        recovered: list[int] = []
        for row in self.audit.unfinished():
            audit_id = int(row["id"])
            resolved = self._journal_resolution(row)
            if resolved is not None:
                phase, code, response = resolved
                written = self.audit.terminal(audit_id, code=code, response=response,
                                              state=phase)
            else:
                code = ErrorCode.OUTCOME_UNKNOWN.value
                written = self.audit.terminal(audit_id, code=code,
                                              response=_error_response(code),
                                              state="outcome_unknown")
            if written:
                recovered.append(audit_id)
                self.events.emit_best_effort("ops_request_terminal", f"audit:{audit_id}", {
                    "endpoint_code": self._endpoint_code(row["endpoint"]),
                    "result_code": code,
                    "authenticated_principal": row["authenticated_principal"]})
        return recovered

    @staticmethod
    def _endpoint_code(route: str) -> str:
        for endpoint in ENDPOINTS.values():
            if endpoint.route == route:
                return endpoint.code
        return "unknown"

    def _journal_resolution(self, row) -> tuple[str, str, dict] | None:
        code = self._endpoint_code(row["endpoint"])
        target = row["target_ref"] or ""
        if code not in ("approve", "retry") or not target.startswith("approval:"):
            return None
        try:
            approval_id = int(target.split(":", 1)[1])
        except ValueError:
            return None
        approval = self._conn.execute(
            "SELECT status,decided_by FROM approval_requests WHERE id=?",
            (approval_id,)).fetchone()
        if approval is None:
            return None
        if approval["status"] == "approved" and approval["decided_by"] == "system_reconcile":
            return ("succeeded", "ok", {"approval_id": approval_id, "status": "approved",
                                        "decided_by": "system_reconcile"})
        if approval["status"] == "pending":
            decided_by = ("shell" if row["authenticated_principal"] == "shell"
                          else f"api:{row['authenticated_principal']}")
            reverted = self._conn.execute(
                "SELECT 1 FROM plugin_switch_journal WHERE approval_id=? AND phase='reverted' "
                "AND actor=? AND updated_at>=?",
                (approval_id, decided_by, row["created_at"])).fetchone()
            if reverted is not None:
                return ("failed", ErrorCode.INVALID_STATE.value,
                        {"approval_id": approval_id, "status": "pending",
                         "journal": "reverted"})
        return None


class _DecisionOutcome(Exception):
    """lock 内で確定した決定以外の outcome。job を失敗終端にしつつ結果を残す。"""

    def __init__(self, code: ErrorCode, result: dict, outcome) -> None:
        super().__init__(code.value)
        self.code, self.result, self.outcome = code, result, outcome
