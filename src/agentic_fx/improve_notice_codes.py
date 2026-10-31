"""改善 mission の終端通知に使う固定語の定義。

終端の各 site は分岐ごとの定数 (`NOTICE_*`) をそのまま渡す。語は
ここに列挙されたものに限り、例外の文面・数値・`last_result` から作らない。
終端 snapshot の書込みと event の検査は、このモジュールの検査関数を通す。
"""
from __future__ import annotations

from dataclasses import dataclass

RESULT_CODES = ("submitted", "observation", "report", "gate_failed", "failed")
STATES = ("started", "finished")
TRIGGERS = ("manual", "scheduled")
TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULED = "scheduled"

TOOL_CALLS_MAX = 1_000_000
OBSERVATION_TEXT_MAX = 2000


@dataclass(frozen=True)
class NoticeCode:
    result_code: str
    reason_code: str | None


class UnknownNoticeCode(ValueError):
    """閉じた辞書に無い語を引いた。"""


NOTICE_SUBMITTED = NoticeCode("submitted", None)

NOTICE_OBSERVATION = NoticeCode("observation", None)
NOTICE_OBSERVATION_NOT_SUBMITTED = NoticeCode("observation", "not_submitted")
NOTICE_DEMOTED_RECOVERED = NoticeCode("observation", "demoted_recovered")
NOTICE_RISK_GATE_UNSUPPORTED = NoticeCode("observation", "risk_gate_unsupported")
NOTICE_DUPLICATE_LOGIC = NoticeCode("observation", "duplicate_logic")
NOTICE_DUPLICATE_METRICS = NoticeCode("observation", "duplicate_metrics")
NOTICE_FAMILY_REDIRECT = NoticeCode("observation", "family_redirect")

NOTICE_REPORT_PUBLISHED = NoticeCode("report", None)
NOTICE_DUPLICATE_SKIPPED = NoticeCode("report", "duplicate_skipped")

NOTICE_UNPROFITABLE = NoticeCode("gate_failed", "unprofitable")
NOTICE_INSUFFICIENT_TRADES = NoticeCode("gate_failed", "insufficient_trades")
NOTICE_HISTORY_SOURCE_UNCONFIGURED = NoticeCode(
    "gate_failed", "history_source_unconfigured")
NOTICE_BACKTEST_DATA_UNAVAILABLE = NoticeCode(
    "gate_failed", "backtest_data_unavailable")
NOTICE_COMPARISON_BASE_UNAVAILABLE = NoticeCode(
    "gate_failed", "comparison_base_unavailable")
NOTICE_NOT_IMPROVED = NoticeCode("gate_failed", "not_improved")
NOTICE_SAME_NAME_CONTRACT_CHANGED = NoticeCode(
    "gate_failed", "same_name_contract_changed")
NOTICE_SNAPSHOT_REJECTED = NoticeCode("gate_failed", "snapshot_rejected")
NOTICE_SOURCE_REJECTED = NoticeCode("gate_failed", "source_rejected")
NOTICE_LOADER_REJECTED = NoticeCode("gate_failed", "loader_rejected")
NOTICE_SYNTAX_ERROR = NoticeCode("gate_failed", "syntax_error")
NOTICE_NOOP_COPY = NoticeCode("gate_failed", "noop_copy")
NOTICE_SELF_TEST_MISSING = NoticeCode("gate_failed", "self_test_missing")
NOTICE_PYTEST_FAILED = NoticeCode("gate_failed", "pytest_failed")
NOTICE_SELF_TEST_TOO_THIN = NoticeCode("gate_failed", "self_test_too_thin")
NOTICE_SELF_TEST_MUTATED = NoticeCode("gate_failed", "self_test_mutated")
NOTICE_UNVERIFIED = NoticeCode("gate_failed", "unverified")
NOTICE_MAX_BARS_LIMIT = NoticeCode("gate_failed", "max_bars_limit")
NOTICE_KIND_UNSUPPORTED = NoticeCode("gate_failed", "kind_unsupported")
NOTICE_OUTPUTS_REQUIRED = NoticeCode("gate_failed", "outputs_required")
NOTICE_INDICATOR_UNRESOLVED = NoticeCode("gate_failed", "indicator_unresolved")
# plugin gate の Landlock 不可。backlog を消費する点が strategy gate の
# failed/sandbox_unavailable (backlog を open に戻す) と異なるため別の語にする。
NOTICE_GATE_SANDBOX_UNAVAILABLE = NoticeCode(
    "gate_failed", "gate_sandbox_unavailable")

NOTICE_REPORT_WRITE_FAILED = NoticeCode("failed", "report_write_failed")
NOTICE_SANDBOX_UNAVAILABLE = NoticeCode("failed", "sandbox_unavailable")
NOTICE_TIMEOUT = NoticeCode("failed", "timeout")
NOTICE_MAX_TURNS = NoticeCode("failed", "max_turns")
NOTICE_RUNNER_FAILED = NoticeCode("failed", "runner_failed")
NOTICE_TOOL_BUDGET_ABORT = NoticeCode("failed", "tool_budget_abort")
NOTICE_OUTPUT_INVALID = NoticeCode("failed", "output_invalid")
NOTICE_APPROVAL_TX_FAILED = NoticeCode("failed", "approval_tx_failed")
NOTICE_COMMIT_CRASHED = NoticeCode("failed", "commit_crashed")
NOTICE_LAUNCH_CRASHED = NoticeCode("failed", "launch_crashed")
NOTICE_PREPARE_FAILED = NoticeCode("failed", "prepare_failed")
NOTICE_INTERRUPTED = NoticeCode("failed", "interrupted")

_ALL_NOTICES = (
    NOTICE_SUBMITTED,
    NOTICE_OBSERVATION, NOTICE_OBSERVATION_NOT_SUBMITTED,
    NOTICE_DEMOTED_RECOVERED, NOTICE_RISK_GATE_UNSUPPORTED,
    NOTICE_DUPLICATE_LOGIC, NOTICE_DUPLICATE_METRICS, NOTICE_FAMILY_REDIRECT,
    NOTICE_REPORT_PUBLISHED, NOTICE_DUPLICATE_SKIPPED,
    NOTICE_UNPROFITABLE, NOTICE_INSUFFICIENT_TRADES,
    NOTICE_HISTORY_SOURCE_UNCONFIGURED, NOTICE_BACKTEST_DATA_UNAVAILABLE,
    NOTICE_COMPARISON_BASE_UNAVAILABLE, NOTICE_NOT_IMPROVED,
    NOTICE_SAME_NAME_CONTRACT_CHANGED, NOTICE_SNAPSHOT_REJECTED,
    NOTICE_SOURCE_REJECTED, NOTICE_LOADER_REJECTED, NOTICE_SYNTAX_ERROR,
    NOTICE_NOOP_COPY, NOTICE_SELF_TEST_MISSING, NOTICE_PYTEST_FAILED,
    NOTICE_SELF_TEST_TOO_THIN, NOTICE_SELF_TEST_MUTATED, NOTICE_UNVERIFIED,
    NOTICE_MAX_BARS_LIMIT, NOTICE_KIND_UNSUPPORTED, NOTICE_OUTPUTS_REQUIRED,
    NOTICE_INDICATOR_UNRESOLVED, NOTICE_GATE_SANDBOX_UNAVAILABLE,
    NOTICE_REPORT_WRITE_FAILED, NOTICE_SANDBOX_UNAVAILABLE, NOTICE_TIMEOUT,
    NOTICE_MAX_TURNS, NOTICE_RUNNER_FAILED, NOTICE_TOOL_BUDGET_ABORT,
    NOTICE_OUTPUT_INVALID, NOTICE_APPROVAL_TX_FAILED, NOTICE_COMMIT_CRASHED,
    NOTICE_LAUNCH_CRASHED, NOTICE_PREPARE_FAILED, NOTICE_INTERRUPTED,
)

ALLOWED_PAIRS = frozenset((n.result_code, n.reason_code) for n in _ALL_NOTICES)

# 専用の見出しを持つ対 (通知本文の見出しの表に専用の行があるもの)。
# これ以外の許可対は汎用の見出しに reason を添えて出す。
PAIRS_WITH_DEDICATED_HEADING = frozenset((n.result_code, n.reason_code) for n in (
    NOTICE_SUBMITTED,
    NOTICE_OBSERVATION, NOTICE_OBSERVATION_NOT_SUBMITTED,
    NOTICE_DEMOTED_RECOVERED, NOTICE_RISK_GATE_UNSUPPORTED,
    NOTICE_DUPLICATE_LOGIC, NOTICE_DUPLICATE_METRICS, NOTICE_FAMILY_REDIRECT,
    NOTICE_UNPROFITABLE, NOTICE_NOT_IMPROVED, NOTICE_SAME_NAME_CONTRACT_CHANGED,
    NOTICE_REPORT_PUBLISHED, NOTICE_DUPLICATE_SKIPPED,
))

_PLUGIN_GATE_NOTICES = {
    "snapshot_rejected": NOTICE_SNAPSHOT_REJECTED,
    "source_rejected": NOTICE_SOURCE_REJECTED,
    "loader_rejected": NOTICE_LOADER_REJECTED,
    "syntax_error": NOTICE_SYNTAX_ERROR,
    "noop_copy": NOTICE_NOOP_COPY,
    "self_test_missing": NOTICE_SELF_TEST_MISSING,
    "pytest_failed": NOTICE_PYTEST_FAILED,
    "self_test_too_thin": NOTICE_SELF_TEST_TOO_THIN,
    "self_test_mutated": NOTICE_SELF_TEST_MUTATED,
    "sandbox_unavailable": NOTICE_GATE_SANDBOX_UNAVAILABLE,
}
PLUGIN_GATE_CODES = frozenset(_PLUGIN_GATE_NOTICES)

_QUALITY_NOTICES = {
    "duplicate_logic": NOTICE_DUPLICATE_LOGIC,
    "duplicate_metrics": NOTICE_DUPLICATE_METRICS,
    "family_redirect": NOTICE_FAMILY_REDIRECT,
    "comparison_base_unavailable": NOTICE_COMPARISON_BASE_UNAVAILABLE,
    "candidate_identity_unavailable": NOTICE_UNVERIFIED,
}
QUALITY_CODES = frozenset(_QUALITY_NOTICES)

_UNEVALUABLE_NOTICES = {
    "insufficient_trades": NOTICE_INSUFFICIENT_TRADES,
    "history_source_unconfigured": NOTICE_HISTORY_SOURCE_UNCONFIGURED,
    "reference_history_missing": NOTICE_BACKTEST_DATA_UNAVAILABLE,
}


def _closed_lookup(table: dict[str, NoticeCode], code: object,
                   what: str) -> NoticeCode:
    if not isinstance(code, str) or code not in table:
        raise UnknownNoticeCode(f"unknown {what} code")
    return table[code]


def notice_for_plugin_gate(code: str) -> NoticeCode:
    return _closed_lookup(_PLUGIN_GATE_NOTICES, code, "plugin gate")


def notice_for_quality(code: str) -> NoticeCode:
    return _closed_lookup(_QUALITY_NOTICES, code, "quality")


def notice_for_unevaluable_kind(kind: str) -> NoticeCode:
    return _closed_lookup(_UNEVALUABLE_NOTICES, kind, "unevaluable kind")


def validate_pair(result_code: object, reason_code: object) -> None:
    """(result_code, reason_code) が許可対であることを確かめる。"""
    if not isinstance(result_code, str) or result_code not in RESULT_CODES:
        raise ValueError("result_code is not an allowed value")
    if reason_code is not None and not isinstance(reason_code, str):
        raise ValueError("reason_code must be a string or null")
    if (result_code, reason_code) not in ALLOWED_PAIRS:
        raise ValueError("(result_code, reason_code) is not an allowed pair")


def validate_tool_calls(tool_calls: object) -> None:
    if tool_calls is None:
        return
    if (isinstance(tool_calls, bool) or not isinstance(tool_calls, int)
            or not 0 <= tool_calls <= TOOL_CALLS_MAX):
        raise ValueError(
            f"tool_calls must be an integer in 0..{TOOL_CALLS_MAX} or null")


def validate_notice_fields(result_code: object, reason_code: object,
                           tool_calls: object) -> None:
    validate_pair(result_code, reason_code)
    validate_tool_calls(tool_calls)


def validate_state(state: object) -> None:
    if not isinstance(state, str) or state not in STATES:
        raise ValueError("state is not an allowed value")


def validate_trigger(trigger: object) -> None:
    if not isinstance(trigger, str) or trigger not in TRIGGERS:
        raise ValueError("trigger is not an allowed value")


def truncate_observation_text(text: str) -> str:
    """保存する観察文をコードポイント単位で上限へ切り詰める (拒否はしない)。"""
    return text[:OBSERVATION_TEXT_MAX]
