"""Status の data カード直下の固定案内 (停止理由と次の操作)。"""
from __future__ import annotations

import pytest

from afx_ops import guidance_text
from afx_ops.api_client import Reply
from afx_ops.web import marks
from afx_ops.web.request import Request, handle_request


def _feed(**extra):
    base = {"state": "degraded", "phase": "stopped", "next_action": "restore_source",
            "unprocessed_positions": 0, "resume_last_result": None,
            "resume_last_result_at": None}
    return {**base, **extra}


class _Client:
    def __init__(self, feed):
        self.feed = feed

    def status(self):
        return Reply(200, {"mode": "paper", "equity": 1, "balance": 1, "data": self.feed}, None)


def _page(feed) -> str:
    response = handle_request(Request("GET", "/", {}), _Client(feed))
    assert response.status == 200
    return response.body.decode()


@pytest.mark.parametrize(("phase", "action"), sorted(guidance_text.PAIRS))
def test_every_pair_shows_both_lines_just_below_the_data_card(phase, action):
    feed = _feed(phase=phase, next_action=action)
    page = _page(feed)
    reason, step = guidance_text.guidance_lines(feed)
    for line in (reason, step):
        assert line.replace("&", "&amp;").replace('"', "&quot;") in page or line in page
    assert page.index("data</strong>") < page.index("停止理由:")
    assert page.index("停止理由:") < page.index("次の操作:")


def test_the_guidance_is_always_shown_even_when_nothing_is_wrong():
    page = _page(_feed(state="ready", phase="ready", next_action="none"))
    assert "次の操作: 不要です" in page
    assert "操作は SSH の端末から行ってください" in page


def test_the_page_has_no_button_or_form_for_the_operation():
    page = _page(_feed(phase="awaiting_resume", next_action="resume_with_ack",
                       unprocessed_positions=2))
    assert "<form" not in page and "<button" not in page and "<input" not in page
    assert "afx ctl --as approver data resume --acknowledge" in page


def test_an_old_api_response_keeps_the_existing_state_card_without_guidance():
    page = _page({"state": "ready"})
    assert "停止理由" not in page and "次の操作" not in page and "データの案内" not in page
    assert "ready" in page
    page = _page({"state": "degraded"})
    assert "停止理由" not in page and "不要です" not in page


@pytest.mark.parametrize("junk", ["<script>", "ready", 5, None, ["ready"], {"a": 1}])
def test_a_hostile_phase_is_unknown_and_never_ready_or_raw(junk):
    page = _page(_feed(phase=junk, next_action=junk))
    assert "停止理由: 状態を確認できません" in page
    assert "<script>" not in page
    assert "不要です" not in page
    assert marks.DATA_PHASE_TEXT["unknown"] in page


@pytest.mark.parametrize("junk", [None, "x", 5, [], "<b>"])
def test_a_non_dict_data_does_not_break_the_page(junk):
    response = handle_request(Request("GET", "/", {}), type("C", (), {
        "status": lambda self: Reply(200, {"mode": "paper", "data": junk}, None)})())
    assert response.status == 200
    assert "次の操作" not in response.body.decode()


def test_the_phase_mark_comes_from_the_shared_assignment_table():
    for phase in guidance_text.PHASES:
        level, word = marks.classify_data_phase({"phase": phase})
        assert level == marks.DATA_PHASES[phase] and word == marks.DATA_PHASE_TEXT[phase]
    assert marks.classify_data_phase({"phase": "zzz"}) == ("unknown", "不明")
    assert marks.classify_data_phase(None) == ("unknown", "不明")
    assert marks.DATA_PHASES["ready"] == "ok"
    for phase in guidance_text.PHASES:
        if phase not in ("ready", "unknown"):
            assert marks.DATA_PHASES[phase] == "warn"


def test_the_phase_assignment_table_covers_the_vocabulary_exactly():
    assert set(marks.DATA_PHASES) == set(guidance_text.PHASES)
    assert set(marks.DATA_PHASE_TEXT) == set(guidance_text.PHASES)


def test_the_count_is_shown_in_the_reason_and_an_unreadable_one_is_unknown_guidance():
    assert "未処理の足がある建玉 2 件" in _page(_feed(
        phase="awaiting_resume", next_action="resume_with_ack", unprocessed_positions=2))
    page = _page(_feed(phase="awaiting_resume", next_action="resume_with_ack",
                       unprocessed_positions="unknown"))
    assert "停止理由: 状態を確認できません" in page and "不要です" not in page


@pytest.mark.parametrize("count", [None, "unknown", -1, True, "0"])
def test_the_no_acknowledge_text_is_never_shown_for_a_count_that_is_not_the_integer_zero(count):
    feed = _feed(phase="resume_rejected", next_action="resume",
                 resume_last_result="rejected_unprocessed")
    if count is None:
        del feed["unprocessed_positions"]
    else:
        feed["unprocessed_positions"] = count
    page = _page(feed)
    assert "なしで再要求" not in page and "停止理由: 状態を確認できません" in page
    ok = _page(_feed(phase="resume_rejected", next_action="resume",
                     resume_last_result="rejected_unprocessed", unprocessed_positions=0))
    assert "なしで再要求" in ok
    positive = _page(_feed(phase="resume_rejected", next_action="resume",
                           resume_last_result="rejected_unprocessed", unprocessed_positions=4))
    assert "なしで再要求" not in positive and "--acknowledge" in positive


def test_a_rejection_after_the_awaiting_mark_still_asks_to_recheck_the_feed():
    page = _page(_feed(phase="resume_rejected", next_action="resume_with_ack",
                       resume_last_result="rejected_unprocessed", unprocessed_positions=2))
    assert "取得状態を再確認してください" in page
