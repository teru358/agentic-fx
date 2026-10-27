"""再起動をまたがない、所有者付きの小さな job 表と FIFO 決定キュー。"""
from __future__ import annotations

import collections
import threading
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from .contracts import Caller, ErrorCode, OpsError

DECISION_QUEUE_CAPACITY = 8
TERMINAL_RETENTION = 64
TERMINAL_TTL_SECONDS = 3600.0
#: 走行中の job が公開する進捗のキー。終端 result と同名でも現在の値が勝つ。
PROGRESS_KEYS = ("phase", "stop_reason", "runs", "mission_id", "stop_requested")


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SHUTDOWN = "shutdown"


TERMINAL_STATES = frozenset({JobState.DONE, JobState.FAILED, JobState.CANCELLED,
                             JobState.SHUTDOWN})


def new_job_id() -> str:
    return uuid.uuid4().hex


@dataclass(slots=True)
class Job:
    id: str
    kind: str
    authenticated_principal: Caller
    asserted_actor: str | None
    action: Callable[["Job"], dict]
    target: str | None = None
    state: JobState = JobState.QUEUED
    result: dict | None = None
    error_code: str | None = None
    created_mono: float = 0.0
    deadline_mono: float | None = None
    audit_id: int | None = None
    terminal_mono: float | None = None
    # 同一プロセス内の呼び出し元 (対話シェル) にだけ渡す結果。job GET には出さない。
    detail: object = None
    done: threading.Event | None = None
    # 繰り返し job の印。走行中の cancel はこの Event を立てるだけで state は変えない。
    cancel_event: threading.Event | None = None


class JobRegistry:
    """job の可視性と容量を lock 一つに閉じ、実行は lock 外で行う。"""

    def __init__(self, *, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._decisions: collections.deque[str] = collections.deque()
        # 停止で一度閉じたら開き直さない。閉じた後の受理は job を作らずに断る。
        self._closed = False

    def _new(self, job_id: str | None, principal: Caller, actor: str | None, kind: str,
             action: Callable[[Job], dict], *, deadline_seconds: float,
             target: str | None = None, audit_id: int | None = None) -> Job:
        self._evict()
        created = self._monotonic()
        job = Job(job_id or new_job_id(), kind, principal, actor, action, target=target,
                  created_mono=created, deadline_mono=created + deadline_seconds,
                  audit_id=audit_id, done=threading.Event())
        self._jobs[job.id] = job
        return job

    def _active(self, predicate: Callable[[Job], bool]) -> bool:
        return any(predicate(j) and j.state not in TERMINAL_STATES
                   for j in self._jobs.values())

    def enqueue_decision(self, principal: Caller, actor: str | None, kind: str,
                         action: Callable[[Job], dict], *, deadline_seconds: float,
                         target: str, job_id: str | None = None,
                         audit_id: int | None = None) -> Job:
        with self._lock:
            if self._closed:
                raise OpsError(ErrorCode.UNAVAILABLE)
            # 同じ approval の決定は 1 件だけ。容量より先に見て、重複は 409 にする。
            if self._active(lambda j: j.target == target and j.kind in
                            {"approve", "reject", "retry"}):
                raise OpsError(ErrorCode.DECISION_IN_PROGRESS)
            if len(self._decisions) >= DECISION_QUEUE_CAPACITY:
                raise OpsError(ErrorCode.UNAVAILABLE)
            job = self._new(job_id, principal, actor, kind, action,
                            deadline_seconds=deadline_seconds, target=target,
                            audit_id=audit_id)
            self._decisions.append(job.id)
            return job

    def enqueue_single(self, principal: Caller, actor: str | None, kind: str,
                       action: Callable[[Job], dict], *, deadline_seconds: float,
                       busy_code: ErrorCode, job_id: str | None = None,
                       audit_id: int | None = None, initial_result: dict | None = None,
                       cancel_event: threading.Event | None = None) -> Job:
        """ask / 手動 improve の 1 本枠。走行中の同種 job があれば作らない。

        `initial_result` と `cancel_event` は job が見えるようになる lock 内で設定する。"""
        with self._lock:
            if self._closed:
                raise OpsError(ErrorCode.UNAVAILABLE)
            if self._active(lambda j: j.kind == kind):
                raise OpsError(busy_code)
            job = self._new(job_id, principal, actor, kind, action,
                            deadline_seconds=deadline_seconds, audit_id=audit_id)
            job.state = JobState.RUNNING
            if initial_result is not None:
                job.result = dict(initial_result)
            job.cancel_event = cancel_event
            return job

    def update_result(self, job: Job, patch: dict) -> None:
        """進捗を重ねた新しい dict に置き換える。終端の job には何もしない。
        停止要求は registry だけが書き、patch では下げない。"""
        with self._lock:
            if job.state in TERMINAL_STATES:
                return
            merged = dict(job.result or {})
            merged.update({k: v for k, v in patch.items() if k != "stop_requested"})
            job.result = merged

    def has_active(self, kind: str) -> bool:
        with self._lock:
            return self._active(lambda j: j.kind == kind)

    def take_next_decision(self) -> Job | None:
        with self._lock:
            while self._decisions:
                job = self._jobs.get(self._decisions.popleft())
                if job is not None and job.state is JobState.QUEUED:
                    job.state = JobState.RUNNING
                    return job
            return None

    def finish(self, job: Job, *, state: JobState, result: dict | None = None,
               error_code: str | None = None, detail: object = None) -> Job:
        with self._lock:
            if job.state in TERMINAL_STATES:
                return job
            if result is not None or job.result is not None:
                current = job.result or {}
                merged = {**current, **(result or {})}
                merged.update({k: current[k] for k in PROGRESS_KEYS if k in current})
                result = merged
            job.result, job.error_code, job.detail = result, error_code, detail
            job.state = state
            job.terminal_mono = self._monotonic()
            self._evict()
        if job.done is not None:
            job.done.set()
        return job

    def lookup(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    @staticmethod
    def _owner_error(job: Job | None, principal: Caller, actor: str | None) -> ErrorCode | None:
        """所有者の判定はここ一か所。別 principal の job は無いものとして扱い (NOT_FOUND)、
        同じ principal で申告主体だけが違う場合は FORBIDDEN と区別する。"""
        if job is None or job.authenticated_principal != principal:
            return ErrorCode.NOT_FOUND
        return None if job.asserted_actor == actor else ErrorCode.FORBIDDEN

    @classmethod
    def _owned(cls, job: Job | None, principal: Caller, actor: str | None) -> Job:
        # 取得・取消は (principal, actor) の組が違えば job を見せない。
        if cls._owner_error(job, principal, actor) is not None:
            raise OpsError(ErrorCode.NOT_FOUND)
        return job

    def check_owner(self, job_id: str, principal: Caller, actor: str | None) -> None:
        """受理前の拒否用。同じ principal で申告主体だけが違う job は FORBIDDEN。
        存在しない・別 principal の job はここでは断らず、取得・取消の NOT_FOUND に任せる。"""
        with self._lock:
            error = self._owner_error(self._jobs.get(job_id), principal, actor)
        if error is ErrorCode.FORBIDDEN:
            raise OpsError(error)

    def get(self, job_id: str, principal: Caller, actor: str | None) -> Job:
        with self._lock:
            return self._owned(self._jobs.get(job_id), principal, actor)

    def cancel(self, job_id: str, principal: Caller, actor: str | None) -> Job:
        with self._lock:
            job = self._owned(self._jobs.get(job_id), principal, actor)
            if job.state in TERMINAL_STATES:
                return job
            if job.state is JobState.RUNNING:
                if job.cancel_event is None:
                    raise OpsError(ErrorCode.JOB_RUNNING)
                # 繰り返し job は停止を要求するだけ。走行中の mission は完走する。
                job.cancel_event.set()
                job.result = {**(job.result or {}), "stop_requested": True}
                return job
            job.state, job.terminal_mono = JobState.CANCELLED, self._monotonic()
            job.error_code = "cancelled"
        if job.done is not None:
            job.done.set()
        return job

    def shutdown_queued(self) -> list[Job]:
        """待ち行列を閉じ、未着手の job を shutdown 終端にして返す。"""
        with self._lock:
            self._closed = True
            closed = []
            for job in self._jobs.values():
                if job.state is JobState.QUEUED:
                    job.state, job.terminal_mono = JobState.SHUTDOWN, self._monotonic()
                    job.error_code = "shutdown"
                    closed.append(job)
            self._decisions.clear()
        for job in closed:
            if job.done is not None:
                job.done.set()
        return closed

    def _evict(self) -> None:
        now = self._monotonic()
        terminal = sorted((j for j in self._jobs.values() if j.state in TERMINAL_STATES),
                          key=lambda j: j.terminal_mono if j.terminal_mono is not None else now)
        keep = []
        for job in terminal:
            if job.terminal_mono is not None and now - job.terminal_mono > TERMINAL_TTL_SECONDS:
                self._jobs.pop(job.id, None)
            else:
                keep.append(job)
        for job in keep[:-TERMINAL_RETENTION] if len(keep) > TERMINAL_RETENTION else []:
            self._jobs.pop(job.id, None)
