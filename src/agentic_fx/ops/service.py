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
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from agentic_fx.core import improve_repeat as ir
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.policy_projection import (POLICY_TEXT_LIMIT, MigrationRecordUnknown, insert_policy,
                                          inspect_policy_file, reconcile_policy_file,
                                          regenerate_policy_file, unmanaged_policy_lines)

from . import redactor
from .activity import SuppressedActivity
from .audit import AcceptedRequest, AuditStore, IdempotencyReplay
from .contracts import (ACTOR_PRINCIPALS, PRINCIPAL_SCOPES, Caller, ErrorCode, OpsError,
                        Principal, Scope, ShellCaller, assert_authorized,
                        validate_asserted_actor)
from .events import EventSchemaError, EventStore
from .jobs import Job, JobRegistry, JobState, new_job_id
from .tail import activity_filter, tail_lines

_log = logging.getLogger(__name__)

#: 引数が渡されなかったことの印 (None は「渡された null」として断る)。
_UNSET = object()


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


class PolicyProjectionUnknown(RuntimeError):
    """A committed policy mutation may not have reached its file projection."""


ENDPOINTS: dict[str, Endpoint] = {e.code: e for e in (
    Endpoint("status", "GET /v1/status", Scope.STATUS_READ, False),
    Endpoint("log", "GET /v1/log", Scope.LOGS_READ, False),
    Endpoint("activity", "GET /v1/activity", Scope.LOGS_READ, False),
    Endpoint("approval_detail", "GET /v1/approvals/{id}", Scope.APPROVALS_DETAIL, False),
    Endpoint("approval_change", "GET /v1/approvals/{id}/change", Scope.APPROVALS_DETAIL, False),
    Endpoint("approval_list", "GET /v1/approvals", Scope.APPROVALS_LIST, False),
    Endpoint("improve_run", "GET /v1/improve/runs/{id}", Scope.APPROVALS_DETAIL, False),
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
    Endpoint("backlog_list", "GET /v1/backlog", Scope.GUIDANCE_READ, False),
    Endpoint("backlog_reject", "POST /v1/backlog/{id}/reject", Scope.OPERATE, True,
             autopilot_restricted=True),
    Endpoint("backlog_reopen", "POST /v1/backlog/{id}/reopen", Scope.OPERATE, True,
             autopilot_restricted=True),
    Endpoint("backlog_note", "POST /v1/backlog/{id}/note", Scope.OPERATE, True,
             autopilot_restricted=True),
    Endpoint("policy", "POST /v1/policy", Scope.OPERATE, True, idempotent=True,
             autopilot_restricted=True),
    Endpoint("policy_show", "GET /v1/policy", Scope.GUIDANCE_READ, False),
    Endpoint("policy_remove", "POST /v1/policy/{id}/remove", Scope.OPERATE, True,
             idempotent=True, autopilot_restricted=True),
    Endpoint("policy_reconcile", "POST /v1/policy/reconcile", Scope.OPERATE, True,
             idempotent=True),
    Endpoint("job_cancel", "POST /v1/jobs/{id}/cancel", Scope.JOBS_OWN, True),
)}

POLICY_SHOW_LIMIT = 200

_DECISION_KINDS = ("approve", "reject", "retry")
_BACKLOG_TRANSITIONS = {
    "reject": (("open", "observation"), "rejected", "human_rejected"),
    "note": (("open", "observation"), "note", "human_noted"),
    "reopen": (("done", "rejected"), "open", "reopened"),
}
_DEPLOYED = frozenset({"deployed", "deployed_after_rollback", "invalidated"})
_BACKLOG_TEXT_LIMIT = 4096
_BACKLOG_TRUNCATION = "…[truncated]"


def _precut(value: object) -> tuple[object, bool]:
    """Cut an oversized string to the redactor's input bound on a character boundary,
    backing up to the last whitespace so a token is never split. Without any
    whitespace nothing of the text is kept."""
    if type(value) is not str:
        return value, False
    raw = value.encode("utf-8", "surrogatepass")
    if len(raw) <= redactor.MAX_REDACT_VALUE_BYTES:
        return value, False
    kept = raw[:redactor.MAX_REDACT_VALUE_BYTES].decode("utf-8", "ignore")
    for index in range(len(kept) - 1, -1, -1):
        if _is_token_boundary(kept[index]):
            return kept[:index], True
    # 区切りが無いと最後の token が途中で切れているかを判別できないので、本文は見せない。
    return "", True


def _is_token_boundary(ch: str) -> bool:
    """redactor の正規化後も残る空白だけを token の区切りとみなす。redactor が除く制御文字
    (CR、VT、FF、情報分離子など) は token の中を繋ぐので区切りではない。"""
    return ch.isspace() and redactor.strip_controls(ch) == ch


def _backlog_text(value: object) -> tuple[str | None, bool]:
    """Redact and bound user guidance before it crosses the API boundary."""
    if value is None:
        return None, False
    value, precut = _precut(value)
    text = redactor.redact_value(value)
    text = " ".join(text.split())
    encoded = text.encode("utf-8")
    if len(encoded) <= _BACKLOG_TEXT_LIMIT and not precut:
        return text, False
    suffix = _BACKLOG_TRUNCATION.encode("utf-8")
    kept = encoded[:_BACKLOG_TEXT_LIMIT - len(suffix)].decode("utf-8", "ignore")
    return kept + _BACKLOG_TRUNCATION, True


def _is_busy(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return "locked" in text or "busy" in text


_BUSY_ATTEMPT_MS = 50
# 終端の監査行を書けなかった job の固定 code。
AUDIT_WRITE_FAILED = "audit_write_failed"


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


IMPROVE_OUTBOX_WINDOW = timedelta(days=7)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError:
        return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _elapsed_sec(started_at: object, notice) -> int | None:
    """snapshot の作成時刻 - mission の開始時刻 (整数秒、0.5 秒は切り上げ)。

    マイクロ秒の整数で計算し float を使わない。再起動回収の終端 (停止していた
    時間を含む)、時刻が読めない、終了が開始より前のときは None。"""
    if notice is None or (notice["result_code"], notice["reason_code"]) == (
            "failed", "interrupted"):
        return None
    start, end = _parse_time(started_at), _parse_time(notice["created_at"])
    if start is None or end is None:
        return None
    delta = end - start
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return None if micros < 0 else (micros + 500_000) // 1_000_000


def _notice_text(value: object) -> str | None:
    """観察文を 1 行に畳む。制御文字・改行は空白へ、空なら空文字。"""
    if not isinstance(value, str):
        return None
    from agentic_fx.improve_notice_codes import OBSERVATION_TEXT_MAX
    from agentic_fx.loops.approval_facts import display_text
    shown = display_text(value, OBSERVATION_TEXT_MAX)
    if shown == "-" and not any(ch.isprintable() and not ch.isspace() for ch in value):
        return ""
    return shown


def _approval_of(notice, row) -> dict | None:
    if notice is None or notice["approval_id"] is None or row is None:
        return None
    from agentic_fx.loops.approval_facts import display_text
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, ValueError, RecursionError):
        payload = None
    name = payload.get("name") if isinstance(payload, dict) else None
    from . import safe_open
    plugin = (display_text(name) if isinstance(name, str)
              and safe_open.NAME_RE.fullmatch(name) else None)
    kind = row["kind"]
    return {"id": row["id"],
            "plugin": plugin,
            "kind": display_text(kind) if isinstance(kind, str) and kind.strip() else None}


def _authorize(caller: Caller, endpoint_code: str) -> None:
    """endpoint の認可は表の scope だけを参照する (§3.1)。"""
    assert_authorized(caller, ENDPOINTS[endpoint_code].scope)


def _actor(caller: Caller, actor: str | None, *, required: bool) -> str | None:
    """申告主体の検査。申告できる principal の決定作成と job 操作では欠落を許さない。"""
    actor = validate_asserted_actor(caller, actor)
    if required and actor is None and caller in ACTOR_PRINCIPALS:
        raise OpsError(ErrorCode.INVALID_ARGUMENT)
    return actor


def _error_response(code: ErrorCode | str) -> dict:
    return {"error": {"code": str(code)}}


def _decided_by(caller: Caller) -> str:
    # decided_by は認証済み主体だけから作る。本文の値は使わない。
    if caller is ShellCaller.SHELL:
        return "shell"
    return f"api:{caller.value}"


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
                 settings=None, activity_log=None,
                 improve_reports_dir: Path | None = None) -> None:
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
        self._improve_reports_dir = improve_reports_dir
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
                audit: AuditStore | None = None,
                asserted_actor: str | None = None) -> AcceptedRequest | IdempotencyReplay:
        audit = audit or self.audit
        if endpoint.idempotent:
            return audit.accept_idempotent(
                endpoint=endpoint.route, principal=caller, asserted_actor=asserted_actor,
                key=key, body=body, target_ref=target, job_id=job_id)
        return audit.accept(endpoint=endpoint.route, principal=caller,
                            asserted_actor=asserted_actor,
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
                  code: str, response: dict, *, state: str | None = None,
                  audit: AuditStore | None = None, events: EventStore | None = None) -> None:
        audit = audit or self.audit
        conn = audit.connection
        waiting = getattr(conn, "waiting_until", None)
        # 副作用は済んでいるので、要求 deadline を使い切っていても終端は独立の予算で書く。
        budget = (waiting(self._monotonic() + self.limits.terminal_write_seconds)
                  if waiting is not None else contextlib.nullcontext())
        with budget:
            audit.terminal(accepted.audit_id, code=code, response=response, state=state)
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
                deadline: float | None = None, asserted_actor: str | None = None) -> dict:
        """同期の変更要求。accepted を commit してから副作用、最後に終端行。"""
        self._ensure_accepting()
        deadline = self._deadline(deadline)
        with self._ops_lane(deadline):
            # lane 待ちの間に停止が始まっていれば、accepted を書かずに断る。
            self._ensure_accepting()
            accepted = self._accept(caller, endpoint, body, target, key=key,
                                    asserted_actor=asserted_actor)
            if isinstance(accepted, IdempotencyReplay):
                return self._replay(accepted)
            try:
                if self._restricted(caller, endpoint, deadline):
                    raise OpsError(ErrorCode.AUTOPILOT_RESTRICTED)
                response = action(deadline, accepted)
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    # 割り込みは終端行を書かず伝播させる (再起動時の復旧が outcome_unknown にする)。
                    raise
                if isinstance(exc, PolicyProjectionUnknown):
                    self._terminal(accepted, caller, endpoint, ErrorCode.OUTCOME_UNKNOWN.value,
                                   _error_response(ErrorCode.OUTCOME_UNKNOWN),
                                   state="outcome_unknown")
                    raise OpsError(ErrorCode.OUTCOME_UNKNOWN) from None
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

    def whoami(self, principal: Principal, *, asserted_actor: str | None = None) -> dict:
        _authorize(principal, "whoami")
        actor = _actor(principal, asserted_actor, required=False)
        return {"authenticated_principal": principal.value, "asserted_actor": actor,
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
        deploy_ops = self._deploy_ops_status(deadline)
        if deploy_ops is not None:
            data["plugin_deploy_ops"] = deploy_ops
        return data

    def _deploy_ops_status(self, deadline: float) -> dict | None:
        """Blocking deploy ops and ops needing attention, or an ``unavailable`` marker."""
        try:
            with self._ops_lane(deadline):
                return deploy_store.status_summary(self._conn, now=self._wall_clock())
        except (OpsError, sqlite3.Error) as exc:
            _log.warning("status plugin_deploy_ops unavailable: %s", type(exc).__name__)
            reason = exc.code.value if isinstance(exc, OpsError) else "database_error"
            return {"unavailable": True, "reason": reason}

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

    def list_backlog(self, principal: Caller, *, all_statuses: bool = False,
                     limit: int = 100, deadline: float | None = None) -> dict:
        _authorize(principal, "backlog_list")
        if type(all_statuses) is not bool or type(limit) is not int or not 1 <= limit <= 500:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        deadline = self._deadline(deadline)
        try:
            with self._ops_lane(deadline):
                from agentic_fx.store import backlog
                rows, has_more = backlog.list_items(
                    self._conn, all_statuses=all_statuses, limit=limit)
        except BaseException as exc:
            code = self._map_exception(exc)
            if code is None or isinstance(exc, OpsError):
                raise
            raise OpsError(code) from None
        items = []
        for row in rows:
            idea, idea_truncated = _backlog_text(row.get("idea"))
            last_result, result_truncated = _backlog_text(row.get("last_result"))
            items.append({"id": row["id"], "status": row["status"], "source": row["source"],
                          "updated_at": row["updated_at"], "idea": idea,
                          "idea_truncated": idea_truncated, "last_result": last_result,
                          "last_result_truncated": result_truncated})
        return {"items": items, "limit": limit, "has_more": has_more}

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
                "SELECT kind,status,payload_json,reason,decided_by,decided_at,expires_at "
                "FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
            journals = self._decided_journals(approval_id)
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
        change = self._change_of(approval_id, row["status"], payload, journals,
                                 include_diff=False).summary
        return {"id": approval_id, "kind": display_text(row["kind"]),
                "status": row["status"], "change": change,
                "parent_facts": payload.get("parent_facts"),
                "agent_claims": payload.get("agent_claims"),
                "facts_lines": approval_facts.render_facts_lines(payload),
                "in_sample": payload.get("in_sample"), "holdout": payload.get("holdout"),
                "payload_sha256": payload_sha256(row["payload_json"]),
                "reason": (None if row["reason"] is None else
                           display_text(row["reason"], approval_facts.CLAIM_DISPLAY_LIMIT)),
                "decided_by": row["decided_by"], "decided_at": row["decided_at"],
                "expires_at": row["expires_at"],
                "asserted_actor": None}

    def _decided_journals(self, approval_id: int) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT op_id,old_kind,old_target,phase FROM plugin_switch_journal "
            "WHERE approval_id=? AND phase='decided' ORDER BY op_id",
            (approval_id,)).fetchall()]

    def _change_of(self, approval_id: int, status: str, payload: dict,
                   journals: list[dict], *, include_diff: bool):
        """変更要約は読み出しのたびに計算し、どこにも保存しない。失敗は固定の reason code に
        落とし、例外の本文・path をログにも応答にも出さない。"""
        from . import change as change_mod
        from .change_limits import UNSAFE_SOURCE
        now = self._wall_clock().isoformat()
        try:
            return change_mod.compute_change(
                status=status, payload=payload, journal_rows=journals,
                plugins_root=self._plugins_root, now=now, include_diff=include_diff)
        except Exception:  # noqa: BLE001 — 本文を出さず固定 code にする
            _log.error("approval change computation failed")
            summary = change_mod.failure_summary(payload, now, UNSAFE_SOURCE)
            diff = {"files": [], "omitted_lines": 0} if include_diff else None
            return change_mod.ChangeResult(summary, diff)

    def approval_change(self, principal: Caller, approval_id: int, *,
                        deadline: float | None = None) -> dict:
        _authorize(principal, "approval_change")
        with self._ops_lane(self._deadline(deadline)):
            row = self._conn.execute(
                "SELECT status,payload_json FROM approval_requests WHERE id=?",
                (approval_id,)).fetchone()
            journals = self._decided_journals(approval_id)
        if row is None:
            raise OpsError(ErrorCode.NOT_FOUND)
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, RecursionError):
            payload = None
        if not isinstance(payload, dict):
            raise OpsError(ErrorCode.INVALID_STATE)
        from . import change as change_mod
        result = self._change_of(approval_id, row["status"], payload, journals,
                                 include_diff=True)
        response = {"id": approval_id, "change": result.summary,
                    "claims": change_mod.claims_of(payload), "diff": result.diff}
        change_mod.fit_response_to_bytes(response)
        return response

    # ---- 改善 mission の通知材料 -------------------------------------

    def improve_run(self, principal: Caller, mission_id: int, *,
                    deadline: float | None = None) -> dict:
        """改善 mission 1 件の通知材料。終端 snapshot を正本に、mission 行・run 行・
        承認・床未達の数値(公開済みレポートから)を足す。監査行は書かない。"""
        _authorize(principal, "improve_run")
        with self._ops_lane(self._deadline(deadline)):
            mission = self._conn.execute(
                "SELECT loop,runner,model,started_at FROM missions WHERE id=?",
                (mission_id,)).fetchone()
            if mission is None or mission["loop"] != "improve":
                raise OpsError(ErrorCode.NOT_FOUND)
            notice = self._conn.execute(
                "SELECT trigger,result_code,reason_code,tool_calls,observation_text,"
                "approval_id,created_at FROM improve_run_notices WHERE mission_id=?",
                (mission_id,)).fetchone()
            run = self._conn.execute(
                "SELECT report_state,report_path FROM improvement_runs "
                "WHERE mission_id=? ORDER BY id DESC LIMIT 1", (mission_id,)).fetchone()
            approval = None
            if notice is not None and notice["approval_id"] is not None:
                approval = self._conn.execute(
                    "SELECT id,kind,payload_json FROM approval_requests WHERE id=?",
                    (notice["approval_id"],)).fetchone()
            report_state = run["report_state"] if run is not None else "none"
            floor = None
            if (notice is not None and run is not None
                    and (notice["result_code"], notice["reason_code"])
                    == ("gate_failed", "unprofitable")):
                floor = self._floor_of(mission_id, report_state, run["report_path"])
        from agentic_fx.loops.approval_facts import display_text
        snapshot = None
        if notice is not None:
            snapshot = {
                "trigger": notice["trigger"], "result_code": notice["result_code"],
                "reason_code": notice["reason_code"], "tool_calls": notice["tool_calls"],
                "observation_text": _notice_text(notice["observation_text"])}
        return {"id": mission_id,
                "backend": display_text(mission["runner"]),
                "model": display_text(mission["model"]),
                "elapsed_sec": _elapsed_sec(mission["started_at"], notice),
                "snapshot": snapshot,
                "report_state": report_state,
                "approval": _approval_of(notice, approval),
                "floor": floor}

    def _floor_of(self, mission_id: int, report_state: str,
                  report_path: object) -> dict | None:
        """公開済み床未達レポートの数値。読めないときは None で、理由は固定語だけを警告に残す
        (path・数値・例外文面は出さない)。ops lane の中で呼ぶ。"""
        from agentic_fx.loops.improve_loop import parse_floor_report
        from . import safe_open
        from .change_limits import TOO_LARGE, UNAVAILABLE
        reason = None
        detail = None
        if report_state != "published":
            reason = "unpublished"
        elif self._improve_reports_dir is None or not isinstance(report_path, str):
            reason = "missing"
        else:
            try:
                text = safe_open.read_improve_report(
                    self._improve_reports_dir, mission_id, report_path).decode("utf-8")
            except safe_open.SourceError as exc:
                reason = {UNAVAILABLE: "missing", TOO_LARGE: "oversized"}.get(
                    exc.code, "unsafe")
            except Exception:  # noqa: BLE001
                reason = "unsafe"
            else:
                detail = parse_floor_report(text, mission_id=mission_id)
                if detail is None:
                    reason = "malformed"
        if detail is None:
            _log.warning("improve_run floor unavailable mission=%s reason=%s",
                         mission_id, reason)
            return None
        return {"stages": [
            {"stage": stage.stage,
             "pairs": [{"pair": row.pair, "trades": row.trades, "pf": row.pf,
                        "avg_r": row.avg_r, "evaluable": row.evaluable}
                       for row in stage.pairs]}
            for stage in detail.stages]}

    def emit_pending_improve_notices(self, *, enabled: bool) -> int:
        """終端 snapshot を outbox として、まだ ``finished`` event が無い mission の
        event を出す。起動時に listener を開く前と、scheduler の tick ごとに呼ぶ。出した件数を返す。
        ``enabled`` が偽、または失敗したときは何も出さない (失敗は固定語だけ記録)。"""
        if not enabled:
            return 0
        try:
            with self._ops_lane(self._deadline(None)):
                return self._emit_pending_improve_notices()
        except OpsError:
            return 0  # lane が空かなかった。次の周期に回す
        except Exception:  # noqa: BLE001
            _log.error("improve_notice_skipped reason=outbox_failed")
            return 0

    def _emit_pending_improve_notices(self) -> int:
        floor = _aware(self._wall_clock()) - IMPROVE_OUTBOX_WINDOW
        since = floor.astimezone(timezone.utc).isoformat()
        emitted = 0
        for row in self.events.unannounced_improve_finishes(since=since):
            mission_id = row["mission_id"]
            try:
                event_id = self.events.emit_best_effort(
                    "improve_run_changed", f"improve:{mission_id}",
                    {"state": "finished", "trigger": row["trigger"],
                     "result_code": row["result_code"], "reason_code": row["reason_code"],
                     "tool_calls": row["tool_calls"]})
            except EventSchemaError:
                _log.error("improve_notice_skipped reason=schema")
                continue
            emitted += event_id is not None
        return emitted

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

    def get_job(self, principal: Caller, job_id: str, *,
                asserted_actor: str | None = None) -> dict:
        _authorize(principal, "job_get")
        actor = _actor(principal, asserted_actor, required=True)
        self.jobs.check_owner(job_id, principal, actor)
        return self._job_view(self.jobs.get(job_id, principal, actor))

    @staticmethod
    def _job_view(job: Job) -> dict:
        result = job.result
        return {"id": job.id, "kind": job.kind, "state": job.state.value,
                "result": dict(result) if result is not None else None,
                "result_code": job.error_code}

    # ------------------------------------------------------- sync mutations

    def show_policy(self, principal: Caller, *, deadline: float | None = None) -> dict:
        _authorize(principal, "policy_show")
        deadline = self._deadline(deadline)
        try:
            with self._ops_lane(deadline):
                return self._show_policy()
        except BaseException as exc:
            code = self._map_exception(exc)
            if code is None or isinstance(exc, OpsError):
                raise
            raise OpsError(code) from None

    def _show_policy(self) -> dict:
        rows = self._conn.execute(
            "SELECT id,created_at,directive FROM ops_policies ORDER BY id").fetchall()
        managed = [{"id": int(row["id"]), "created_at": row["created_at"],
                    "directive": row["directive"]} for row in rows]
        unmanaged = ([] if self._policy_path is None else [
            {"line": line, "directive": directive, "removable": False}
            for line, directive in unmanaged_policy_lines(self._policy_path)
        ])
        listed_managed = managed[:POLICY_SHOW_LIMIT]
        remaining = POLICY_SHOW_LIMIT - len(listed_managed)
        listed_unmanaged = unmanaged[:remaining]
        projection = (inspect_policy_file(self._conn, self._policy_path)
                      if self._policy_path is not None else None)
        return {"managed": listed_managed, "unmanaged": listed_unmanaged,
                "limit": POLICY_SHOW_LIMIT,
                "has_more": len(managed) + len(unmanaged) > POLICY_SHOW_LIMIT,
                "projection": {"status": projection.status if projection else "ok",
                               "reasons": list(projection.reasons) if projection else []}}

    def _reconcile_policy(self) -> bool:
        if self._policy_path is None:
            return False
        return reconcile_policy_file(self._conn, self._policy_path)

    def _reconcile_before_policy_mutation(self) -> None:
        if self._reconcile_policy():
            self._activity("SYSTEM", "policy_reconciled", "")

    def add_policy(self, principal: Caller, idempotency_key: str, text: str, *,
                   deadline: float | None = None) -> dict:
        """canonical record を正にして policy file を毎回再生成する。"""
        _authorize(principal, "policy")
        if not isinstance(text, str) or not text.strip() or len(text) > POLICY_TEXT_LIMIT:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)

        def action(_deadline: float, accepted: AcceptedRequest) -> dict:
            self._reconcile_before_policy_mutation()
            request_id = accepted.request_id
            insert_policy(self._conn, text, request_id=request_id, now=self._wall_clock())
            try:
                self._regenerate_policy()
            except Exception as exc:
                raise PolicyProjectionUnknown() from exc
            # 既存運用との互換で、policy_added だけは先頭 200 文字を残す (§8 の例外)。
            self._activity("SYSTEM", "policy_added", text[:200])
            return {"id": request_id}

        return self._mutate(principal, ENDPOINTS["policy"], {"text": text}, "policy", action,
                            key=idempotency_key, deadline=deadline)

    def remove_policy(self, principal: Caller, idempotency_key: str, identifier: int, *,
                      deadline: float | None = None) -> dict:
        _authorize(principal, "policy_remove")
        if type(identifier) is not int or identifier < 1:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)

        def action(_deadline: float, _accepted: AcceptedRequest) -> dict:
            self._reconcile_before_policy_mutation()
            if self._conn.execute("SELECT 1 FROM ops_policies WHERE id=?",
                                  (identifier,)).fetchone() is None:
                raise OpsError(ErrorCode.NOT_FOUND)
            cursor = self._conn.execute("DELETE FROM ops_policies WHERE id=?", (identifier,))
            if cursor.rowcount != 1:
                self._conn.rollback()
                raise OpsError(ErrorCode.NOT_FOUND)
            self._conn.commit()
            try:
                self._regenerate_policy()
            except Exception as exc:
                raise PolicyProjectionUnknown() from exc
            self._activity("SYSTEM", "policy_removed", f"id={identifier}")
            return {"id": identifier}

        return self._mutate(principal, ENDPOINTS["policy_remove"], {"id": identifier}, str(identifier), action,
                            key=idempotency_key, deadline=deadline)

    def reconcile_policy(self, principal: Caller, idempotency_key: str, *,
                         deadline: float | None = None) -> dict:
        _authorize(principal, "policy_reconcile")

        def action(_deadline: float, _accepted: AcceptedRequest) -> dict:
            try:
                changed = self._reconcile_policy()
            except MigrationRecordUnknown as exc:
                raise PolicyProjectionUnknown() from exc
            try:
                projection = (inspect_policy_file(self._conn, self._policy_path)
                              if self._policy_path is not None else None)
            except BaseException as exc:
                raise PolicyProjectionUnknown() from exc
            if changed:
                self._activity("SYSTEM", "policy_reconciled", "")
            return {"changed": changed,
                    "projection": {"status": projection.status if projection else "ok",
                                   "reasons": list(projection.reasons) if projection else []}}

        return self._mutate(principal, ENDPOINTS["policy_reconcile"], {}, "policy", action,
                            key=idempotency_key, deadline=deadline)

    def regenerate_policy(self) -> None:
        """起動時などに canonical record から policy file を作り直す。"""
        with self._ops_lane(self._deadline(None)):
            self._reconcile_policy()

    def _regenerate_policy(self) -> None:
        if self._policy_path is None:
            return
        regenerate_policy_file(self._conn, self._policy_path)

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
                   deadline: float | None = None, asserted_actor: str | None = None) -> dict:
        _authorize(principal, "job_cancel")
        actor = _actor(principal, asserted_actor, required=True)
        if not isinstance(job_id, str) or not job_id:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        # 申告主体の不一致は受理前に断る (監査行を作らない)。照合は registry の一か所。
        self.jobs.check_owner(job_id, principal, actor)

        def action(_deadline: float, _accepted: AcceptedRequest) -> dict:
            job = self.jobs.cancel(job_id, principal, actor)
            if job.state is JobState.CANCELLED and job.audit_id is not None:
                self._finish_job_audit(job, self.audit, self.events)
            if job.state is not JobState.RUNNING:
                # 走行中の繰り返し job は停止を要求しただけで、state は変わらない。
                self._job_event(job, self.events)
            response = {"id": job.id, "state": job.state.value}
            if job.repeat:
                response["stop_requested"] = bool((job.result or {}).get("stop_requested"))
            return response

        return self._mutate(principal, ENDPOINTS["job_cancel"], {"job_id": job_id},
                            f"job:{job_id}", action, deadline=deadline,
                            asserted_actor=actor)

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
                deadline: float | None = None, duration_sec=_UNSET, max_runs=_UNSET,
                comment=_UNSET, **unknown) -> dict:
        _authorize(principal, "improve")
        endpoint = ENDPOINTS["improve"]
        # 受理前の検査。違反は監査行を作らずに断る。渡されなかった key は本文に入れない。
        if unknown:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        given = {k: v for k, v in (("duration_sec", duration_sec), ("max_runs", max_runs),
                                   ("comment", comment)) if v is not _UNSET}
        if any(v is None for v in given.values()):
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        duration_sec, max_runs, comment = (given.get("duration_sec"), given.get("max_runs"),
                                           given.get("comment"))
        body: dict = {}
        if duration_sec is not None:
            try:
                body["duration_sec"] = ir.validate_duration_sec(duration_sec)
            except ValueError:
                raise OpsError(ErrorCode.INVALID_ARGUMENT) from None
        if max_runs is not None:
            try:
                body["max_runs"] = ir.validate_max_runs(max_runs)
            except ValueError:
                raise OpsError(ErrorCode.INVALID_ARGUMENT) from None
        if comment is not None:
            try:
                comment = ir.normalize_comment(comment)
            except ValueError:
                raise OpsError(ErrorCode.INVALID_ARGUMENT) from None
            # 本文 (とその digest) は監査・冪等の表に残さない。有無だけを写す。
            body["has_comment"] = True
        repeat = duration_sec is not None or max_runs is not None
        self._ensure_accepting()
        deadline = self._deadline(deadline)
        job_id = new_job_id()
        holder = ir.CommentHolder(comment) if comment is not None else None
        del comment
        with self._ops_lane(deadline):
            self._ensure_accepting()
            accepted = self._accept(principal, endpoint, body, "improve",
                                    key=idempotency_key, job_id=job_id)
            if isinstance(accepted, IdempotencyReplay):
                if holder is not None:
                    holder.clear()
                return self._replay(accepted)
            # 期限の基準は受理に成功した時点。以降の確認や待ちで動かさない。
            started_mono, started_wall = self._monotonic(), self._wall_clock()
            job: Job | None = None
            try:
                if self._improve_supervisor is None:
                    raise OpsError(ErrorCode.INVALID_STATE)
                if self._restricted(principal, endpoint, deadline):
                    raise OpsError(ErrorCode.AUTOPILOT_RESTRICTED)
                if repeat:
                    job = self._enqueue_repeat(principal, job_id, accepted.audit_id,
                                               duration_sec, max_runs, holder,
                                               started_mono, started_wall)
                else:
                    job = self.jobs.enqueue_single(
                        principal, None, "improve", self._improve_action(holder),
                        deadline_seconds=self.limits.improve_start_deadline_seconds,
                        busy_code=ErrorCode.IMPROVE_RUNNING, job_id=job_id,
                        audit_id=accepted.audit_id)
                self._start_thread(job)
                if holder is not None:
                    self._activity("IMPROVE", "improve_requested",
                                   f"repeat={'yes' if repeat else 'no'} comment=yes")
            except BaseException as exc:
                if holder is not None:
                    holder.clear()
                code = self._map_exception(exc) or ErrorCode.INTERNAL
                if job is None:
                    self._terminal(accepted, principal, endpoint, code.value,
                                   _error_response(code))
                else:
                    # job は作ったが走らせられなかった。誰も終わらせないので、ここで失敗にする。
                    self._finish_job_audit(job, self.audit, self.events,
                                           state=JobState.FAILED, error_code=code.value)
                    self.jobs.finish(job, state=JobState.FAILED, error_code=code.value,
                                     result=job.result)
                    self._job_event(job, self.events)
                raise
            return {"job_id": job.id, "state": job.state.value}

    def _enqueue_repeat(self, principal: Caller, job_id: str, audit_id: int,
                        duration_sec: int | None, max_runs: int | None,
                        holder: "ir.CommentHolder | None", started_mono: float,
                        started_wall: datetime) -> Job:
        # 期限は受理の時点 (呼び出し側が採った時刻) で一度だけ決める。単調時計が判定の正本、
        # 壁時計は表示用。
        span = ir.MAX_DURATION_SEC if duration_sec is None else duration_sec
        wall = started_wall
        if wall.tzinfo is None:
            wall = wall.replace(tzinfo=timezone.utc)
        deadline_mono = started_mono + span
        deadline_at = (wall + timedelta(seconds=span)).astimezone(timezone.utc).isoformat()
        initial = {"mission_id": None, "runs": 0,
                   "max_runs": ir.MAX_RUNS if max_runs is None else max_runs,
                   "deadline_at": deadline_at, "phase": "waiting_slot",
                   "stop_requested": False}
        return self.jobs.enqueue_single(
            principal, None, "improve",
            self._repeat_action(holder, duration_sec, max_runs, deadline_mono, deadline_at),
            deadline_seconds=self.limits.improve_start_deadline_seconds,
            busy_code=ErrorCode.IMPROVE_RUNNING, job_id=job_id, audit_id=audit_id,
            initial_result=initial, cancel_event=threading.Event(), repeat=True)

    def _improve_action(self, holder: "ir.CommentHolder | None" = None) -> Callable[[Job], dict]:
        """1 回だけの improve。一時指示は holder 越しにだけ持ち、終わったら必ず消す。"""
        def run(job: Job) -> dict:
            try:
                self._precheck_improve(job)

                def prepared(mission_id: int) -> None:
                    self.jobs.update_result(job, {"mission_id": mission_id})

                kwargs = {"on_prepared": prepared}
                text = holder.get() if holder is not None else None
                if text is not None:
                    kwargs["comment"] = text
                mission_id = self._improve_supervisor.submit_manual(**kwargs)
                return {"mission_id": mission_id}
            finally:
                if holder is not None:
                    holder.clear()
        return run

    def _repeat_action(self, holder: "ir.CommentHolder | None", duration_sec: int | None,
                       max_runs: int | None, deadline_mono: float,
                       deadline_at: str) -> Callable[[Job], dict]:
        def run(job: Job) -> dict:
            try:
                # 開始前の autopilot / daemon 停止の判定は controller が行い、通常の停止として終える。
                outcome = self._improve_supervisor.submit_repeat(
                    duration_sec=duration_sec, max_runs=max_runs, comment_holder=holder,
                    on_progress=lambda patch: self.jobs.update_result(job, patch),
                    cancel_event=job.cancel_event,
                    autopilot_check=lambda: self._autopilot_recheck(job),
                    monotonic=self._monotonic, deadline_mono=deadline_mono,
                    deadline_at=deadline_at)
                return {"stop_reason": outcome.stop_reason, "runs": outcome.runs,
                        "mission_id": outcome.last_mission_id}
            finally:
                if holder is not None:
                    holder.clear()
        return run

    def _precheck_improve(self, job: Job) -> None:
        # 開始直前に StateStore の flock 内で autopilot を読み直す (§5)。
        if job.authenticated_principal is not ShellCaller.SHELL \
                and self._autopilot_at_start(job):
            raise OpsError(ErrorCode.AUTOPILOT_RESTRICTED)
        if self.stop_event.is_set():
            raise OpsError(ErrorCode.UNAVAILABLE)

    def _start_thread(self, job: Job) -> None:
        thread = threading.Thread(target=self._run_single, args=(job,),
                                  name=f"afx-ops-{job.kind}", daemon=True)
        with self._threads_lock:
            self._operation_threads.add(thread)
        try:
            thread.start()
        except BaseException:
            with self._threads_lock:
                self._operation_threads.discard(thread)
            raise

    def _run_single(self, job: Job) -> None:
        try:
            try:
                result = job.action(job)
                state, code = JobState.DONE, None
            except BaseException as exc:
                mapped = self._map_exception(exc) or ErrorCode.INTERNAL
                if mapped is ErrorCode.INTERNAL:
                    _log.exception("ops job failed kind=%s", job.kind)
                state = (JobState.SHUTDOWN if mapped is ErrorCode.UNAVAILABLE
                         and self.stop_event.is_set() else JobState.FAILED)
                code, result = mapped.value, job.result
            state, code, result = self._seal_terminal_audit(
                job, state, code, result, self._close_job_with_ops_lane)
            self.jobs.finish(job, state=state, error_code=code, result=result)
            self._job_event_with_ops_lane(job)
        finally:
            with self._threads_lock:
                self._operation_threads.discard(threading.current_thread())

    def _seal_terminal_audit(self, job: Job, state: JobState, code: str | None,
                             result: dict | None, close) -> tuple[JobState, str | None,
                                                                    dict | None]:
        # 終端行は job を終端状態として見せる前に書く。polling で done が見えたら監査に
        # 終端行がある保証のため、書けなかった done の job は failed で終える (失敗・停止の
        # job は元の終端のまま)。accepted のまま残る行は次回起動の回復が outcome_unknown にする。
        try:
            close(job, state, code)
        except Exception:
            _log.warning("ops job terminal audit failed job=%s", job.id, exc_info=True)
            if state is JobState.DONE:
                return JobState.FAILED, AUDIT_WRITE_FAILED, job.result
        return state, code, result

    def _close_job_with_ops_lane(self, job: Job, state: JobState,
                                 error_code: str | None) -> None:
        with self._ops_lane(self._monotonic() + self.limits.request_deadline_seconds):
            self._finish_job_audit(job, self.audit, self.events, state=state,
                                   error_code=error_code)

    def _job_event_with_ops_lane(self, job: Job) -> None:
        # 共有接続を使うので event も ops lane の中で書く。
        try:
            with self._ops_lane(self._monotonic() + self.limits.request_deadline_seconds):
                self._job_event(job, self.events)
        except Exception:
            _log.warning("ops job event failed job=%s", job.id, exc_info=True)

    def _job_event_best_effort(self, job: Job, events: EventStore) -> None:
        try:
            self._job_event(job, events)
        except Exception:
            _log.warning("decision job event failed job=%s", job.id, exc_info=True)

    def _finish_job_audit(self, job: Job, audit: AuditStore, events: EventStore, *,
                          state: JobState | None = None,
                          error_code: str | None = None) -> None:
        if job.audit_id is None:
            return
        if state is None:
            state, error_code = job.state, job.error_code
        code = "ok" if state is JobState.DONE else (error_code or state.value)
        response = {"job_id": job.id, "state": state.value, "result_code": code}
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

    def _autopilot_recheck(self, job: Job) -> bool:
        """繰り返しの mission ごとの開始前確認。受理から時間が経つので、呼ぶたびに
        新しい期限で読む。読めなければ例外のまま伝え (mission を始めない)、
        対話シェルは制限しない。"""
        if self._state_store is None or job.authenticated_principal is ShellCaller.SHELL:
            return False
        deadline = self._monotonic() + self.limits.request_deadline_seconds
        return bool(self._state_store.autopilot_at_start(
            deadline=self._foreign(deadline), stop_event=self.stop_event))

    # ------------------------------------------------------------- decisions

    def approve(self, principal: Caller, approval_id: int, payload_sha256: str, *,
                deadline: float | None = None, asserted_actor: str | None = None) -> dict:
        return self._submit_decision(principal, "approve", approval_id, payload_sha256,
                                     None, deadline, asserted_actor)

    def reject(self, principal: Caller, approval_id: int, reason: str | None = None, *,
               deadline: float | None = None, asserted_actor: str | None = None) -> dict:
        return self._submit_decision(principal, "reject", approval_id, None, reason, deadline,
                                     asserted_actor)

    def retry(self, principal: Caller, approval_id: int, payload_sha256: str, *,
              deadline: float | None = None, asserted_actor: str | None = None) -> dict:
        return self._submit_decision(principal, "retry", approval_id, payload_sha256,
                                     None, deadline, asserted_actor)

    def _submit_decision(self, caller: Caller, kind: str, approval_id: int,
                         digest: str | None, reason: str | None,
                         deadline: float | None, asserted_actor: str | None = None) -> dict:
        if kind not in _DECISION_KINDS:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        _authorize(caller, kind)
        # 申告主体は監査と job 所有者にだけ記録する。decided_by は principal から作る。
        actor = _actor(caller, asserted_actor, required=True)
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
                                    key=None, job_id=job_id, asserted_actor=actor)
            try:
                self._preflight_decision(self._conn, approval_id, kind, digest)
                if self._restricted(caller, endpoint, deadline):
                    raise OpsError(ErrorCode.AUTOPILOT_RESTRICTED)
                action = self._decision_action(kind, approval_id, digest, reason,
                                               _decided_by(caller))
                job = self.jobs.enqueue_decision(
                    caller, actor, kind, action,
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
            result = self._execute_decision(job)
            state, code = JobState.DONE, None
        except BaseException as exc:
            mapped = self._map_exception(exc) or ErrorCode.INTERNAL
            if mapped is ErrorCode.INTERNAL:
                _log.exception("decision job failed kind=%s", job.kind)
            result = None
            if isinstance(exc, _DecisionOutcome):
                result, job.detail = exc.result, exc.outcome
            state = (JobState.SHUTDOWN if mapped is ErrorCode.UNAVAILABLE
                     and self.stop_event.is_set() else JobState.FAILED)
            code = mapped.value
        state, code, result = self._seal_terminal_audit(
            job, state, code, result, self._close_decision)
        self.jobs.finish(job, state=state, error_code=code, result=result,
                         detail=job.detail)
        self._job_event_best_effort(job, self._decide_events)
        return job

    def _execute_decision(self, job: Job) -> dict:
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
            return job.action(job)

    def _close_decision(self, job: Job, state: JobState, error_code: str | None) -> None:
        self._finish_job_audit(job, self._decide_audit, self._decide_events,
                               state=state, error_code=error_code)

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
