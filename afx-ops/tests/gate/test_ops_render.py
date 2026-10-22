from __future__ import annotations

import pytest

from afx_ops.gate.render import escape_discord, render_event


TS = "2026-10-22T00:00:00+00:00"


def _event(code: str, fields: dict, *, ref: str = "instance") -> dict:
    refs = {"approval_state_changed": "approval:25", "job_state_changed": "job:" + "a" * 32,
            "ops_request_terminal": "audit:7"}
    return {"id": 1, "ts": TS, "code": code, "ref": refs.get(code, ref), "fields": fields}


@pytest.mark.parametrize(("state", "name", "expected"), [
    ("approved", "sma_cross", r"承認 #25 (戦略 sma\_cross) が承認・配備されました。"),
    ("approved", None, "承認 #25 が承認・配備されました。"),
    ("rejected", "sma_cross", r"承認 #25 (戦略 sma\_cross) が却下されました。"),
    ("expired", "sma_cross", "承認 #25 の状態が変わりました (expired)。"),
])
def test_ac17_approval_event_has_a_fixed_human_sentence(state, name, expected):
    rendered = render_event(_event("approval_state_changed", {
        "state": state, "decision_code": "approve", "decided_by": "api:ops-bot"}),
                            plugin_name=name)
    assert rendered.content == expected
    assert "=" not in rendered.content
    assert "配備ジョブを開始" not in rendered.content
    assert "api:ops-bot" not in rendered.content


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
