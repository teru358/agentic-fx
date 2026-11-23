"""`afx ctl status` の data: 停止理由と次の操作の 2 行。"""
from __future__ import annotations

import pytest

from agentic_fx.datafeed import guidance_text
from agentic_fx.ops import client


def _feed(**extra):
    base = {"state": "degraded", "phase": "stopped", "next_action": "restore_source",
            "unprocessed_positions": 0, "resume_last_result": None,
            "resume_last_result_at": None}
    return {**base, **extra}


def _lines(data):
    return client.format_status({"data": data}).splitlines()


def test_ctl_shows_the_state_line_then_the_reason_and_the_next_step():
    lines = _lines(_feed())
    i = lines.index("data: degraded")
    assert lines[i + 1].startswith("停止理由: ") and lines[i + 2].startswith("次の操作: ")
    assert lines[i + 1:i + 3] == list(guidance_text.guidance_lines(_feed()))


def test_ctl_wait_tick_always_names_the_next_open_tick():
    text = "\n".join(_lines(_feed(phase="resume_requested", next_action="wait_tick")))
    assert "次の開場中の tick の判定待ち" in text


@pytest.mark.parametrize("old", [{"state": "ready"}, {"state": "degraded"}, {"state": "mystery"}])
def test_an_old_api_response_keeps_the_existing_state_line_only(old):
    lines = _lines(old)
    assert lines == [line for line in lines if not line.startswith(("停止理由", "次の操作"))]
    assert f"data: {old['state']}" in lines
    assert not any("不要" in line for line in lines)


@pytest.mark.parametrize("junk", ["<script>", "\x1b[31m", "ready", 5, None, ["ready"]])
def test_a_hostile_or_foreign_phase_never_becomes_ready_or_raw_text(junk):
    text = client.format_status({"data": _feed(phase=junk, next_action=junk)})
    assert "停止理由: 状態を確認できません" in text
    assert "\x1b" not in text and "<script>" not in text
    assert "不要です" not in text


def test_the_second_line_for_every_pair_is_present_in_ctl():
    for phase, action in sorted(guidance_text.PAIRS):
        lines = _lines(_feed(phase=phase, next_action=action))
        assert any(line.startswith("停止理由: ") for line in lines), (phase, action)
        assert any(line.startswith("次の操作: ") for line in lines), (phase, action)


@pytest.mark.parametrize("count", [None, "unknown", -1, True, "0"])
def test_ctl_never_says_no_acknowledge_is_needed_for_a_count_that_is_not_the_integer_zero(count):
    feed = _feed(phase="resume_rejected", next_action="resume",
                 resume_last_result="rejected_unprocessed")
    if count is None:
        del feed["unprocessed_positions"]
    else:
        feed["unprocessed_positions"] = count
    text = "\n".join(_lines(feed))
    assert "なしで再要求" not in text and "停止理由: 状態を確認できません" in text
    zero = "\n".join(_lines(_feed(phase="resume_rejected", next_action="resume",
                                  resume_last_result="rejected_unprocessed",
                                  unprocessed_positions=0)))
    assert "なしで再要求" in zero
    positive = "\n".join(_lines(_feed(phase="resume_rejected", next_action="resume",
                                      resume_last_result="rejected_unprocessed",
                                      unprocessed_positions=4)))
    assert "なしで再要求" not in positive and "--acknowledge" in positive


def test_ctl_asks_to_recheck_the_feed_after_a_rejection():
    text = "\n".join(_lines(_feed(phase="resume_rejected", next_action="restore_source",
                                  resume_last_result="rejected_watermark")))
    assert "取得状態を再確認してください" in text
