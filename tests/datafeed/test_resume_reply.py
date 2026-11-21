import pytest

from agentic_fx.datafeed.resume_reply import resume_reply


@pytest.mark.parametrize(("acknowledge", "kept"), [
    (True, "--acknowledge 指定ありを維持しています"),
    (False, "--acknowledge 指定なしを維持しています"),
])
def test_an_inquiry_says_whether_the_acknowledge_is_kept(acknowledge, kept):
    text = resume_reply("existing", acknowledge)
    assert text.startswith("resume 要求は既に記録済みです。")
    assert kept in text
    other = "指定なしを維持" if acknowledge else "指定ありを維持"
    assert other not in text
    assert "次の開場中の tick の判定待ちです。" in text


def test_a_new_acknowledged_request_is_followed_by_a_separator_before_the_next_sentence():
    text = resume_reply("new", True)
    assert text.endswith("(--acknowledge 指定あり) ")
    assert resume_reply("new", False).endswith("判定待ちです。")
