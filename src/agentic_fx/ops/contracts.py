"""ops 層の固定エラーと versioned な認可データ。"""
from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "invalid_argument"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    REQUEST_TIMEOUT = "request_timeout"
    AUTOPILOT_RESTRICTED = "autopilot_restricted"
    IDEMPOTENCY_MISMATCH = "idempotency_mismatch"
    OUTCOME_UNKNOWN = "outcome_unknown"
    JOB_RUNNING = "job_running"
    INVALID_STATE = "invalid_state"
    ATTEMPT_CHANGED = "attempt_changed"
    ALREADY_DECIDED = "already_decided"
    PAYLOAD_CHANGED = "payload_changed"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNSUPPORTED_KIND = "unsupported_kind"
    PLUGIN_BUSY = "plugin_busy"
    MISSION_BUSY = "mission_busy"
    IMPROVE_RUNNING = "improve_running"
    DECISION_IN_PROGRESS = "decision_in_progress"
    GENERATION_MISMATCH = "generation_mismatch"
    NOT_LATCHED = "not_latched"
    DATABASE_BUSY = "database_busy"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"


_MESSAGES = {code: code.value.replace("_", " ") for code in ErrorCode}


class OpsError(Exception):
    """固定 code の失敗。例外文は code だけで、内部の詳細を載せない。"""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code

    def __repr__(self) -> str:
        return f"OpsError({self.code.value})"

    @property
    def message(self) -> str:
        return _MESSAGES[self.code]


class Scope(StrEnum):
    STATUS_READ = "status.read"
    EVENTS_READ = "events.read"
    APPROVALS_LIST = "approvals.list"
    APPROVALS_DETAIL = "approvals.detail"
    LOGS_READ = "logs.read"
    JOBS_OWN = "jobs.own"
    OPERATE = "operate"
    DECIDE = "decide"
    LOCAL_GUARD = "local_guard"


class Principal(StrEnum):
    OPERATOR = "operator"
    APPROVER = "approver"


class ShellCaller(StrEnum):
    """対話シェル。API principal ではないので scope 表には載せない。

    シェルは従来から全操作を行える信頼境界内の利用者端末であり、API と同じ
    レーンを共有するために ops 関数を呼ぶ。監査では principal 欄に ``shell``
    と記録し、決定の ``decided_by`` も従来どおり ``shell`` にする。
    """
    SHELL = "shell"


Caller = Principal | ShellCaller


PRINCIPAL_SCOPES: dict[Principal, frozenset[Scope]] = {
    Principal.OPERATOR: frozenset({
        Scope.STATUS_READ, Scope.EVENTS_READ, Scope.APPROVALS_LIST,
        Scope.APPROVALS_DETAIL, Scope.LOGS_READ, Scope.JOBS_OWN, Scope.OPERATE}),
    Principal.APPROVER: frozenset(Scope),
}


def validate_principal_scopes(table: dict[Principal, frozenset[Scope]] = PRINCIPAL_SCOPES) -> None:
    if set(table) != set(Principal):
        raise ValueError("unknown or missing principal")
    for principal, scopes in table.items():
        if len(scopes) != len(set(scopes)):
            raise ValueError("duplicate scope")
        if Scope.LOCAL_GUARD in scopes and principal is not Principal.APPROVER:
            raise ValueError("local_guard is approver-only")


def assert_authorized(principal: Caller, scope: Scope) -> None:
    validate_principal_scopes()
    if principal is ShellCaller.SHELL:
        return
    if not isinstance(principal, Principal) or scope not in PRINCIPAL_SCOPES[principal]:
        raise OpsError(ErrorCode.FORBIDDEN)
