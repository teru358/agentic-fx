"""終端通知の固定語: 許可対・分岐ごとの定数・閉じた辞書・検査関数。"""
from __future__ import annotations

import itertools

import pytest

from agentic_fx import improve_notice_codes as codes

# 設計書の変換表を、実装とは別に書き下した期待値 (定数名, result, reason)。
EXPECTED = [
    ("NOTICE_SUBMITTED", "submitted", None),
    ("NOTICE_OBSERVATION", "observation", None),
    ("NOTICE_OBSERVATION_NOT_SUBMITTED", "observation", "not_submitted"),
    ("NOTICE_DEMOTED_RECOVERED", "observation", "demoted_recovered"),
    ("NOTICE_RISK_GATE_UNSUPPORTED", "observation", "risk_gate_unsupported"),
    ("NOTICE_DUPLICATE_LOGIC", "observation", "duplicate_logic"),
    ("NOTICE_DUPLICATE_METRICS", "observation", "duplicate_metrics"),
    ("NOTICE_FAMILY_REDIRECT", "observation", "family_redirect"),
    ("NOTICE_COMPARISON_BASE_UNAVAILABLE", "gate_failed",
     "comparison_base_unavailable"),
    ("NOTICE_REPORT_PUBLISHED", "report", None),
    ("NOTICE_DUPLICATE_SKIPPED", "report", "duplicate_skipped"),
    ("NOTICE_REPORT_WRITE_FAILED", "failed", "report_write_failed"),
    ("NOTICE_UNPROFITABLE", "gate_failed", "unprofitable"),
    ("NOTICE_INSUFFICIENT_TRADES", "gate_failed", "insufficient_trades"),
    ("NOTICE_HISTORY_SOURCE_UNCONFIGURED", "gate_failed",
     "history_source_unconfigured"),
    ("NOTICE_BACKTEST_DATA_UNAVAILABLE", "gate_failed",
     "backtest_data_unavailable"),
    ("NOTICE_NOT_IMPROVED", "gate_failed", "not_improved"),
    ("NOTICE_SAME_NAME_CONTRACT_CHANGED", "gate_failed",
     "same_name_contract_changed"),
    ("NOTICE_SNAPSHOT_REJECTED", "gate_failed", "snapshot_rejected"),
    ("NOTICE_SOURCE_REJECTED", "gate_failed", "source_rejected"),
    ("NOTICE_LOADER_REJECTED", "gate_failed", "loader_rejected"),
    ("NOTICE_SYNTAX_ERROR", "gate_failed", "syntax_error"),
    ("NOTICE_NOOP_COPY", "gate_failed", "noop_copy"),
    ("NOTICE_SELF_TEST_MISSING", "gate_failed", "self_test_missing"),
    ("NOTICE_PYTEST_FAILED", "gate_failed", "pytest_failed"),
    ("NOTICE_SELF_TEST_TOO_THIN", "gate_failed", "self_test_too_thin"),
    ("NOTICE_SELF_TEST_MUTATED", "gate_failed", "self_test_mutated"),
    ("NOTICE_SANDBOX_UNAVAILABLE", "failed", "sandbox_unavailable"),
    ("NOTICE_GATE_SANDBOX_UNAVAILABLE", "gate_failed",
     "gate_sandbox_unavailable"),
    ("NOTICE_UNVERIFIED", "gate_failed", "unverified"),
    ("NOTICE_MAX_BARS_LIMIT", "gate_failed", "max_bars_limit"),
    ("NOTICE_KIND_UNSUPPORTED", "gate_failed", "kind_unsupported"),
    ("NOTICE_OUTPUTS_REQUIRED", "gate_failed", "outputs_required"),
    ("NOTICE_INDICATOR_UNRESOLVED", "gate_failed", "indicator_unresolved"),
    ("NOTICE_TIMEOUT", "failed", "timeout"),
    ("NOTICE_MAX_TURNS", "failed", "max_turns"),
    ("NOTICE_RUNNER_FAILED", "failed", "runner_failed"),
    ("NOTICE_TOOL_BUDGET_ABORT", "failed", "tool_budget_abort"),
    ("NOTICE_OUTPUT_INVALID", "failed", "output_invalid"),
    ("NOTICE_APPROVAL_TX_FAILED", "failed", "approval_tx_failed"),
    ("NOTICE_COMMIT_CRASHED", "failed", "commit_crashed"),
    ("NOTICE_LAUNCH_CRASHED", "failed", "launch_crashed"),
    ("NOTICE_PREPARE_FAILED", "failed", "prepare_failed"),
    ("NOTICE_INTERRUPTED", "failed", "interrupted"),
]

DEDICATED = {
    ("submitted", None), ("observation", None), ("observation", "not_submitted"),
    ("observation", "demoted_recovered"),
    ("observation", "risk_gate_unsupported"),
    ("observation", "duplicate_logic"), ("observation", "duplicate_metrics"),
    ("observation", "family_redirect"), ("gate_failed", "unprofitable"),
    ("gate_failed", "not_improved"), ("gate_failed", "same_name_contract_changed"),
    ("report", None), ("report", "duplicate_skipped"),
}


@pytest.mark.parametrize("name,result,reason", EXPECTED)
def test_each_constant_is_the_table_pair(name, result, reason):
    assert getattr(codes, name) == codes.NoticeCode(result, reason)


def test_constants_match_the_table_one_to_one():
    defined = {n for n in dir(codes) if n.startswith("NOTICE_")}
    assert defined == {name for name, _, _ in EXPECTED}
    pairs = [(r, w) for _, r, w in EXPECTED]
    assert len(pairs) == len(set(pairs))


def test_allowed_pairs_are_exactly_the_table():
    assert codes.ALLOWED_PAIRS == {(r, w) for _, r, w in EXPECTED}


def test_result_codes_are_the_five_values():
    assert codes.RESULT_CODES == (
        "submitted", "observation", "report", "gate_failed", "failed")
    assert {r for r, _ in codes.ALLOWED_PAIRS} == set(codes.RESULT_CODES)


def test_catch_all_words_do_not_exist():
    reasons = {w for _, w in codes.ALLOWED_PAIRS}
    assert "gate_other" not in reasons and "plugin_gate" not in reasons


def test_dedicated_heading_pairs_are_those_with_a_dedicated_row():
    assert codes.PAIRS_WITH_DEDICATED_HEADING == DEDICATED
    assert DEDICATED <= codes.ALLOWED_PAIRS


def test_constants_are_frozen():
    with pytest.raises(Exception):
        codes.NOTICE_TIMEOUT.reason_code = "x"  # type: ignore[misc]


# --- 閉じた辞書 -------------------------------------------------------------

PLUGIN_GATE = {
    "snapshot_rejected": "NOTICE_SNAPSHOT_REJECTED",
    "source_rejected": "NOTICE_SOURCE_REJECTED",
    "loader_rejected": "NOTICE_LOADER_REJECTED",
    "syntax_error": "NOTICE_SYNTAX_ERROR",
    "noop_copy": "NOTICE_NOOP_COPY",
    "self_test_missing": "NOTICE_SELF_TEST_MISSING",
    "pytest_failed": "NOTICE_PYTEST_FAILED",
    "self_test_too_thin": "NOTICE_SELF_TEST_TOO_THIN",
    "self_test_mutated": "NOTICE_SELF_TEST_MUTATED",
    "sandbox_unavailable": "NOTICE_GATE_SANDBOX_UNAVAILABLE",
}
QUALITY = {
    "duplicate_logic": "NOTICE_DUPLICATE_LOGIC",
    "duplicate_metrics": "NOTICE_DUPLICATE_METRICS",
    "family_redirect": "NOTICE_FAMILY_REDIRECT",
    "comparison_base_unavailable": "NOTICE_COMPARISON_BASE_UNAVAILABLE",
    "candidate_identity_unavailable": "NOTICE_UNVERIFIED",
}


@pytest.mark.parametrize("code,name", PLUGIN_GATE.items())
def test_plugin_gate_dictionary(code, name):
    assert codes.notice_for_plugin_gate(code) == getattr(codes, name)


@pytest.mark.parametrize("code,name", QUALITY.items())
def test_quality_dictionary(code, name):
    assert codes.notice_for_quality(code) == getattr(codes, name)


def test_dictionaries_are_closed_sets():
    assert codes.PLUGIN_GATE_CODES == set(PLUGIN_GATE)
    assert codes.QUALITY_CODES == set(QUALITY)
    assert len(codes.QUALITY_CODES) == 5


@pytest.mark.parametrize("lookup", [
    codes.notice_for_plugin_gate, codes.notice_for_quality,
    codes.notice_for_unevaluable_kind])
@pytest.mark.parametrize("bad", ["", "unknown", "gate_other", None, 3,
                                 "pf:7.7777", "duplicate_logic ", "x"])
def test_unknown_word_is_an_explicit_error_not_a_default(lookup, bad):
    with pytest.raises(codes.UnknownNoticeCode) as info:
        lookup(bad)
    assert not isinstance(info.value, KeyError)


def test_unevaluable_kind_dictionary():
    assert codes.notice_for_unevaluable_kind("insufficient_trades") \
        == codes.NOTICE_INSUFFICIENT_TRADES
    assert codes.notice_for_unevaluable_kind("history_source_unconfigured") \
        == codes.NOTICE_HISTORY_SOURCE_UNCONFIGURED


# --- 検査関数 ---------------------------------------------------------------

def test_validate_pair_accepts_exactly_the_allowed_pairs():
    reasons = {w for _, w in codes.ALLOWED_PAIRS} | {"bogus"}
    for result, reason in itertools.product(codes.RESULT_CODES, reasons):
        if (result, reason) in codes.ALLOWED_PAIRS:
            codes.validate_pair(result, reason)
        else:
            with pytest.raises(ValueError):
                codes.validate_pair(result, reason)


@pytest.mark.parametrize("reason", [
    "pf:7.7777", ":", "7", "16", "insufficient_trades:16",
    "commit_crashed:OSError:disk", "Disk quota exceeded", "gate_other",
    "plugin_gate", "zzz_new_word", "", " timeout", "timeout\n"])
def test_validate_pair_rejects_free_text_and_unknown_words(reason):
    for result in codes.RESULT_CODES:
        with pytest.raises(ValueError):
            codes.validate_pair(result, reason)


@pytest.mark.parametrize("result", ["", "done", "FAILED", None, 3, "gate_other"])
def test_validate_pair_rejects_unknown_result_code(result):
    with pytest.raises(ValueError):
        codes.validate_pair(result, None)


@pytest.mark.parametrize("pair", [("submitted", "timeout"),
                                  ("failed", None), ("observation", "timeout"),
                                  ("report", "unprofitable")])
def test_validate_pair_rejects_pairs_not_in_the_table(pair):
    with pytest.raises(ValueError):
        codes.validate_pair(*pair)


def test_validate_pair_rejects_non_string_reason():
    with pytest.raises(ValueError):
        codes.validate_pair("failed", 7)


@pytest.mark.parametrize("ok", [0, 1, 999, 1_000_000, None])
def test_tool_calls_accepts(ok):
    codes.validate_notice_fields("failed", "timeout", ok)


@pytest.mark.parametrize("bad", [-1, True, False, 1.0, 1.5, "3", b"3",
                                 1_000_001, [1], float("nan")])
def test_tool_calls_rejects(bad):
    with pytest.raises(ValueError):
        codes.validate_notice_fields("failed", "timeout", bad)


@pytest.mark.parametrize("state,ok", [("started", True), ("finished", True),
                                      ("done", False), ("", False), (None, False)])
def test_state(state, ok):
    if ok:
        codes.validate_state(state)
    else:
        with pytest.raises(ValueError):
            codes.validate_state(state)


@pytest.mark.parametrize("trigger,ok", [("manual", True), ("scheduled", True),
                                        ("cron", False), ("", False),
                                        (None, False)])
def test_trigger(trigger, ok):
    if ok:
        codes.validate_trigger(trigger)
    else:
        with pytest.raises(ValueError):
            codes.validate_trigger(trigger)


def test_validation_messages_do_not_echo_the_value():
    with pytest.raises(ValueError) as info:
        codes.validate_pair("failed", "https://example.com/?apikey=SECRET9")
    assert "SECRET9" not in str(info.value)


@pytest.mark.parametrize("size,expected", [(0, 0), (1999, 1999), (2000, 2000),
                                           (2001, 2000), (10_000, 2000)])
def test_truncate_observation_text_counts_code_points(size, expected):
    assert codes.OBSERVATION_TEXT_MAX == 2000
    assert codes.truncate_observation_text("あ" * size) == "あ" * expected
    emoji = "😀" * size
    assert len(codes.truncate_observation_text(emoji)) == expected
