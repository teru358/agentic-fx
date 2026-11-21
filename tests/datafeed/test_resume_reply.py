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
