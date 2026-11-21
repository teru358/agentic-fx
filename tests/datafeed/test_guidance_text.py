"""status の固定案内の文。表の全組・固定語・旧 API・不正な入力を確かめる。"""
import pytest

from agentic_fx.datafeed import guidance_text as gt

WAIT = "次の開場中の tick の判定待ち"


def _feed(phase, action, **extra):
    return {"state": "degraded", "phase": phase, "next_action": action, **extra}


def test_an_old_api_data_without_the_new_keys_gives_no_guidance():
    for feed in ({"state": "ready"}, {}, None, [], "ready", 3):
        assert gt.guidance_lines(feed) is None


@pytest.mark.parametrize(("phase", "action"), sorted(gt.PAIRS))
def test_every_published_pair_renders_two_lines(phase, action):
    reason, next_step = gt.guidance_lines(
        _feed(phase, action, unprocessed_positions=0, resume_last_result="rejected_unprocessed"))
    assert reason.startswith("停止理由: ") and next_step.startswith("次の操作: ")
    assert "\n" not in reason and "\n" not in next_step


def test_the_vocabulary_is_exactly_the_pairs():
    assert {p for p, _ in gt.PAIRS} == set(gt.PHASES)
    assert {a for _, a in gt.PAIRS} == set(gt.NEXT_ACTIONS)


@pytest.mark.parametrize("phase", ["resume_requested", "restricted"])
def test_wait_tick_always_says_it_waits_for_the_next_tick_during_market_hours(phase):
    _, step = gt.guidance_lines(_feed(phase, "wait_tick"))
    assert WAIT in step


def test_a_ready_state_says_no_operation_is_needed():
    reason, step = gt.guidance_lines(_feed("ready", "none"))
    assert step == "次の操作: 不要です"
    assert "停止" in reason


def test_an_acknowledge_is_requested_only_for_a_confirmed_check_of_the_positions():
    _, step = gt.guidance_lines(_feed("awaiting_resume", "resume_with_ack", unprocessed_positions=2))
    assert "建玉を確認し、再開を承認するときだけ" in step
    assert gt.RESUME_ACK in step
    _, plain = gt.guidance_lines(_feed("awaiting_resume", "resume", unprocessed_positions=0))
    assert gt.RESUME in plain and "--acknowledge`" not in plain


def test_the_awaiting_phase_always_adds_a_reminder_to_recheck_the_feed():
    for action in ("resume", "resume_with_ack"):
        _, step = gt.guidance_lines(_feed("awaiting_resume", action, unprocessed_positions=1))
        assert "取得状態を再確認してください" in step
    for phase, action in sorted(gt.PAIRS - {("awaiting_resume", "resume"),
                                            ("awaiting_resume", "resume_with_ack")}):
        _, step = gt.guidance_lines(_feed(phase, action, unprocessed_positions=1))
        assert "取得状態を再確認してください" not in step, (phase, action)


def test_restore_source_does_not_assert_the_cause_of_the_outage():
    _, step = gt.guidance_lines(_feed("stopped", "restore_source"))
    assert "取得状態を確認し、回復後に再要求" in step
    for word in ("接続", "ネットワーク", "bridge", "MT5", "障害"):
        assert word not in step


def test_a_watermark_rejection_is_explained_and_a_second_request_waits_for_recovery():
    reason, step = gt.guidance_lines(_feed("resume_rejected", "restore_source",
                                           resume_last_result="rejected_watermark",
                                           unprocessed_positions=3))
    assert "取得状態" in reason and "拒否" in reason
    assert "取得状態を確認し、回復後に再要求" in step


def test_an_unprocessed_rejection_with_no_positions_now_says_ack_is_not_needed():
    reason, step = gt.guidance_lines(_feed("resume_rejected", "resume",
                                           resume_last_result="rejected_unprocessed",
                                           unprocessed_positions=0))
    assert "未処理の足がある建玉" in reason and "拒否" in reason
    assert "--acknowledge なしで再要求できます" in step
    reason, step = gt.guidance_lines(_feed("resume_rejected", "resume_with_ack",
                                           resume_last_result="rejected_unprocessed",
                                           unprocessed_positions=2))
    assert gt.RESUME_ACK in step and "なしで" not in step


@pytest.mark.parametrize("count", [None, -1, True, 1.5, "3", "unknown", [], {}])
def test_a_count_that_is_not_a_non_negative_integer_is_shown_as_unknown(count):
    reason, _ = gt.guidance_lines(_feed("awaiting_resume", "resume_with_ack",
                                        unprocessed_positions=count))
    assert reason.endswith("(未処理の足がある建玉 不明 件)")


def test_a_count_is_shown_as_is():
    reason, _ = gt.guidance_lines(_feed("awaiting_resume", "resume", unprocessed_positions=0))
    assert reason.endswith("(未処理の足がある建玉 0 件)")


@pytest.mark.parametrize("pair", [("ready", "resume"), ("stopped", "none"), ("zzz", "none"),
                                  (None, None), (1, 2), ("ready", None), ("unknown", "none")])
def test_a_pair_outside_the_published_table_is_shown_as_unknown_never_as_ready(pair):
    reason, step = gt.guidance_lines({"phase": pair[0], "next_action": pair[1]})
    assert reason == "停止理由: 状態を確認できません"
    assert "再実行" in step and "不要" not in step


def test_a_feed_with_only_one_of_the_new_keys_is_treated_as_unknown():
    reason, _ = gt.guidance_lines({"phase": "ready"})
    assert "確認できません" in reason
    reason, _ = gt.guidance_lines({"next_action": "none"})
    assert "確認できません" in reason


def test_no_count_or_internal_names_leak_into_stopped_or_ready_text():
    for pair in (("stopped", "restore_source"), ("ready", "none"), ("restricted", "wait_tick")):
        text = "".join(gt.guidance_lines(_feed(*pair, unprocessed_positions=9)))
        assert "9" not in text
