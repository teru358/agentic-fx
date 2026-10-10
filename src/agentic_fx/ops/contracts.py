"""ops 層の固定エラーと versioned な認可データ。"""
from __future__ import annotations

import re
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
    OPS_BOT = "ops-bot"
    OPS_WEB = "ops-web"
    OPS_NOTIFY = "ops-notify"


# 初回 init が鍵を作る principal (旧形式の固定集合と同じ)。他は enroll で後から登録する。
INITIAL_PRINCIPALS: tuple[Principal, ...] = (Principal.OPERATOR, Principal.APPROVER)


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
    # bot は決定と、その job を追う jobs.own だけ。operate / local_guard / logs.read は持たない。
    Principal.OPS_BOT: frozenset({
        Scope.STATUS_READ, Scope.EVENTS_READ, Scope.APPROVALS_LIST,
        Scope.APPROVALS_DETAIL, Scope.JOBS_OWN, Scope.DECIDE}),
    Principal.OPS_WEB: frozenset({
        Scope.STATUS_READ, Scope.EVENTS_READ, Scope.APPROVALS_LIST,
        Scope.APPROVALS_DETAIL, Scope.LOGS_READ}),
    Principal.OPS_NOTIFY: frozenset({Scope.EVENTS_READ}),
}

# 外部主体を申告できる principal と、その値の形式 (Discord user ID)。
ACTOR_PRINCIPALS: frozenset[Principal] = frozenset({Principal.OPS_BOT})
_ACTOR_PATTERN = re.compile(r"discord:[0-9]{17,20}")
ACTOR_MAX_BYTES = 28


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


def validate_asserted_actor(principal: Caller, actor: str | None) -> str | None:
    """申告された外部主体を検査する。認可や decided_by の根拠にはしない。

    申告できるのは ``ACTOR_PRINCIPALS`` だけで、値は ASCII の ``discord:`` と
    17〜20 桁の十進数。それ以外 (空・空白・制御文字・非 ASCII を含む) は invalid_argument。
    """
    if actor is None:
        return None
    if principal not in ACTOR_PRINCIPALS or not isinstance(actor, str):
        raise OpsError(ErrorCode.INVALID_ARGUMENT)
    # \d は全角数字にも一致するので [0-9] の fullmatch で決める (末尾改行も許さない)。
    if (not actor.isascii() or len(actor.encode("ascii")) > ACTOR_MAX_BYTES
            or _ACTOR_PATTERN.fullmatch(actor) is None):
        raise OpsError(ErrorCode.INVALID_ARGUMENT)
    return actor
