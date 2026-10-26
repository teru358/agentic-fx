from __future__ import annotations

import pytest

from afx_ops.improve_events import CODE as IMPROVE_CODE
from afx_ops.gate.render import OPS_EVENT_SPECS, escape_discord, render_event
from afx_ops.notify.payload import ALLOWED_FIELDS
from afx_ops.trade_events import TRADE_CODES


TS = "2026-10-22T00:00:00+00:00"


def _event(code: str, fields: dict, *, ref: str = "instance") -> dict:
    refs = {"approval_state_changed": "approval:25", "job_state_changed": "job:" + "a" * 32,
            "ops_request_terminal": "audit:7"}
    return {"id": 1, "ts": TS, "code": code, "ref": refs.get(code, ref), "fields": fields}


@pytest.mark.parametrize(("state", "name", "expected"), [
    ("approved", "sma_cross", r"承認 #25 (戦略 sma\_cross) が承認・配備されました。"),
    ("approved", None, "承認 #25 が承認・配備されました。"),
    ("rejected", "sma_cross", r"承認 #25 (戦略 sma\_cross) が却下されました。"),
    ("expired", "sma_cross", r"承認 #25 (戦略 sma\_cross) は期限切れになりました。"),
    ("expired", None, "承認 #25 は期限切れになりました。"),
])
def test_ac17_approval_event_has_a_fixed_human_sentence(state, name, expected):
    rendered = render_event(_event("approval_state_changed", {
        "state": state, "decision_code": "approve", "decided_by": "api:ops-bot"}),
                            plugin_name=name)
    assert rendered.content == expected
    assert "=" not in rendered.content
    assert "配備ジョブを開始" not in rendered.content
    assert "api:ops-bot" not in rendered.content


def test_event_generation_omits_values_wider_than_the_approval_reference_limit():
    event = lambda generation: _event("guard_state_changed", {
        "guard_kind": "kill_switch", "state": "latched", "generation": generation})
    assert render_event(event(int("9" * 18))).content == "kill switch が作動しました (世代 " + "9" * 18 + ")。"
    assert render_event(event(int("9" * 19))).content == "kill switch が作動しました。"


def test_event_body_uses_the_limited_generation_sentence_without_a_second_fallback():
    rendered = render_event(_event("guard_state_changed", {
        "guard_kind": "kill_switch", "state": "latched", "generation": int("9" * 2100)}))
    assert rendered.content == "kill switch が作動しました。"
    assert len(rendered.content) <= 2000


@pytest.mark.parametrize(("code", "fields", "expected"), [
    ("job_state_changed", {"job_kind": "approve", "state": "failed", "result_code": "unavailable"},
     "ジョブ approve が失敗しました (理由 code: unavailable)。"),
    ("job_state_changed", {"job_kind": "ask", "state": "done", "result_code": "ok"},
     "ジョブ ask が完了しました。"),
    ("guard_state_changed", {"guard_kind": "kill_switch", "state": "latched", "generation": 3},
     "kill switch が作動しました (世代 3)。"),
    ("guard_state_changed", {"guard_kind": "data_feed", "state": "resume_requested", "generation": None},
     "データ取得の再開を要求しました。"),
    ("service_health_changed", {"component_code": "trade-sink", "health_code": "degraded"},
     "trade-sink の健全性が degraded になりました。"),
    ("ops_request_terminal", {"endpoint_code": "approve", "result_code": "ok",
                              "authenticated_principal": "api:ops-bot"},
     "操作 approve が終了しました (結果 code: ok)。"),
    ("event_stream_health", {"failure_count": 3, "first_failed_at": "x", "last_failed_at": "y"},
     r"イベント event\_stream\_health が記録されました。"),
])
def test_ac20_event_templates_only_show_the_allowed_values(code, fields, expected):
    rendered = render_event(_event(code, fields))
    assert rendered.content == expected
    assert "api:ops-bot" not in rendered.content


def test_ac20_discord_syntax_is_escaped_in_permitted_template_values():
    assert escape_discord("<@&1> **x** @here") == r"\<@ &1\> \*\*x\*\* @ here"


def test_event_templates_omit_invalid_fields_and_use_the_generic_sentence_without_a_subject():
    cases = [
        ("job_state_changed", {"job_kind": None, "state": "done", "result_code": "ok"}),
        ("guard_state_changed", {"guard_kind": None, "state": "latched", "generation": 1}),
        ("service_health_changed", {"component_code": None, "health_code": "ok"}),
        ("ops_request_terminal", {"endpoint_code": "approve", "result_code": None,
                                  "authenticated_principal": "api:ops-bot"}),
    ]
    for code, fields in cases:
        content = render_event(_event(code, fields)).content
        assert "  " not in content and "(結果 code: )" not in content
        if code != "ops_request_terminal":
            assert content == f"イベント {escape_discord(code)} が記録されました。"
        else:
            assert content == "操作 approve が終了しました。"


def test_ops_event_table_matches_the_daemon_non_trade_event_schema():
    assert set(OPS_EVENT_SPECS) == set(ALLOWED_FIELDS) - set(TRADE_CODES) - {IMPROVE_CODE}




@pytest.mark.parametrize(("code", "fields", "expected"), [
    ("job_state_changed", {"job_kind": "a_b", "state": "failed", "result_code": "r_c"},
     r"ジョブ a\_b が失敗しました (理由 code: r\_c)。"),
    ("job_state_changed", {"job_kind": "a_b", "state": "cancelled", "result_code": "r_c"},
     r"ジョブ a\_b が取り消されました (理由 code: r\_c)。"),
    ("job_state_changed", {"job_kind": "a_b", "state": "shutdown", "result_code": "r_c"},
     r"ジョブ a\_b が停止で中断しました (理由 code: r\_c)。"),
    ("job_state_changed", {"job_kind": "approve", "state": "cancelled", "result_code": None},
     "ジョブ approve が取り消されました。"),
    ("job_state_changed", {"job_kind": "approve", "state": "queued", "result_code": "ok"},
     "ジョブ approve が受け付けられました。"),
    ("job_state_changed", {"job_kind": "approve", "state": "running", "result_code": "ok"},
     "ジョブ approve が実行中です。"),
    ("job_state_changed", {"job_kind": "approve", "state": "paused", "result_code": "ok"},
     "ジョブ approve が状態が変わりました。"),
    ("guard_state_changed", {"guard_kind": "g_k", "state": "s_t", "generation": 2},
     r"ガード g\_k の状態が s\_t になりました。"),
    ("guard_state_changed", {"guard_kind": "kill_switch", "state": "released", "generation": 4},
     "kill switch が解除されました (世代 4)。"),
    ("guard_state_changed", {"guard_kind": "kill_switch", "state": "released", "generation": None},
     "kill switch が解除されました。"),
    ("guard_state_changed", {"guard_kind": "kill_switch", "state": "latched", "generation": None},
     "kill switch が作動しました。"),
    ("guard_state_changed", {"guard_kind": "kill_switch", "state": "resume_requested", "generation": 1},
     "ガード kill\\_switch の状態が resume\\_requested になりました。"),
    ("guard_state_changed", {"guard_kind": "data_feed", "state": "latched", "generation": 1},
     "ガード data\\_feed の状態が latched になりました。"),
    ("service_health_changed", {"component_code": "a_b", "health_code": "d_e"},
     r"a\_b の健全性が d\_e になりました。"),
    ("ops_request_terminal", {"endpoint_code": "e_p", "result_code": "r_c",
                              "authenticated_principal": "api:ops-bot"},
     r"操作 e\_p が終了しました (結果 code: r\_c)。"),
])
def test_event_templates_escape_every_value_and_pick_the_right_branch(code, fields, expected):
    assert render_event(_event(code, fields)).content == expected


def test_approval_state_outside_the_known_pair_is_escaped_in_the_fallback_sentence():
    rendered = render_event(_event("approval_state_changed", {
        "state": "e_x", "decision_code": "approve", "decided_by": "api:ops-bot"}),
                            plugin_name="sma_cross")
    assert rendered.content == r"承認 #25 の状態が変わりました (e\_x)。"


@pytest.mark.parametrize("name", ["../x", "@everyone", "with space", "", "A", 3])
def test_a_plugin_name_outside_the_name_grammar_is_never_written(name):
    rendered = render_event(_event("approval_state_changed", {
        "state": "approved", "decision_code": "approve"}), plugin_name=name)
    assert rendered.content == "承認 #25 が承認・配備されました。"


@pytest.mark.parametrize(("state", "result_code", "expected"), [
    ("failed", "unavailable", "承認 #7 の配備ジョブが失敗しました (理由 code: unavailable)。"),
    ("failed", None, "承認 #7 の配備ジョブが失敗しました。"),
    ("cancelled", "unavailable", "ジョブ approve が取り消されました (理由 code: unavailable)。"),
    ("shutdown", "unavailable", "ジョブ approve が停止で中断しました (理由 code: unavailable)。"),
    ("done", "ok", "ジョブ approve が完了しました。"),
])
def test_a_deploy_job_failure_names_its_approval_only_when_one_is_given(state, result_code, expected):
    rendered = render_event(_event("job_state_changed", {
        "job_kind": "approve", "state": state, "result_code": result_code}), approval_id=7)
    assert rendered.content == expected
