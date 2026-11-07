"""承認カード (Discord) の期限・判定時刻は、応答の display_timezone で換算した offset 付きの分で出す。"""
from __future__ import annotations

import pytest

from afx_ops import display_time, display_zone
from afx_ops.api_client import Reply
from afx_ops.bot.adapter import _post_effect
from afx_ops.gate.render import render_approval, render_terminal
from afx_ops.gate.state import ApprovalRecord

RECORD = ApprovalRecord(approval_id=7, message_id=101, guild_id=12345678901234567,
                        channel_id=12345678901234568, payload_sha256="a" * 64,
                        display_generation=1, state="fresh")
CHANGE = {"name": "sma_cross", "kind": "strategy", "classification": "updated",
          "candidate_hash": "b" * 64, "baseline_hash": "c" * 64,
          "computed_at": "2026-10-19T00:00:00+00:00", "reason": None,
          "params": {"items": [], "omitted": 0, "invalid_keys": 0, "flatten_omitted": 0,
                     "reason": None},
          "performance": {"in_sample": [], "holdout": []}, "summary": "要約"}


@pytest.fixture(autouse=True)
def _fresh_zone_state(monkeypatch):
    monkeypatch.setattr(display_zone, "_warned", False)
    display_time.clear_zone_cache()
    yield
    display_time.clear_zone_cache()


def _detail(**fields):
    data = {"status": "pending", "change": dict(CHANGE),
            "expires_at": "2026-10-26T00:00:00+00:00"}
    data.update(fields)
    return data


def _deadline(content):
    return next(line for line in content.split("\n") if line.startswith("期限"))


def test_the_deadline_is_converted_to_the_zone_of_the_response():
    assert _deadline(render_approval(RECORD, _detail(display_timezone="Asia/Tokyo"))) \
        == "期限: 2026-10-26 09:00 +09:00"


def test_the_offset_follows_daylight_saving_time_at_the_deadline():
    summer = _detail(display_timezone="America/New_York", expires_at="2026-07-01T00:00:00Z")
    winter = _detail(display_timezone="America/New_York", expires_at="2026-12-01T00:00:00Z")
    assert _deadline(render_approval(RECORD, summer)) == "期限: 2026-06-30 20:00 -04:00"
    assert _deadline(render_approval(RECORD, winter)) == "期限: 2026-11-30 19:00 -05:00"


def test_the_card_never_names_the_zone():
    content = render_approval(RECORD, _detail(display_timezone="Asia/Tokyo"))
    assert "Asia/Tokyo" not in content and "表示 tz" not in content


def test_a_response_without_the_field_shows_the_deadline_in_utc_without_logging(capsys):
    assert _deadline(render_approval(RECORD, _detail())) == "期限: 2026-10-26 00:00 +00:00"
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("bad", ["Not/AZone", "A" * 300, "", None, 5, ["UTC"], "../x"])
def test_an_unusable_zone_shows_utc_and_the_wire_value_is_not_logged(bad, capsys):
    content = render_approval(RECORD, _detail(display_timezone=bad))
    assert _deadline(content) == "期限: 2026-10-26 00:00 +00:00"
    err = capsys.readouterr().err
    assert err == "afx-ops: display_timezone_unusable (UTC で表示)\n"


def test_a_naive_deadline_stays_a_minute_without_an_offset_in_any_zone():
    detail = _detail(display_timezone="Asia/Tokyo", expires_at="2026-10-26T00:00:00")
    assert _deadline(render_approval(RECORD, detail)) == "期限: 2026-10-26 00:00"


@pytest.mark.parametrize("raw", ["not a time", "", 5, None, "9999-12-31T23:59:59-23:59",
                                 "2026-10-26T00:00:00." + "1" * 60 + "+00:00"])
def test_an_unreadable_or_unconvertible_deadline_is_not_shown(raw):
    detail = _detail(display_timezone="Asia/Tokyo", expires_at=raw)
    assert _deadline(render_approval(RECORD, detail)) == "期限: なし"


def test_a_time_the_zone_cannot_convert_is_shown_in_utc_instead():
    # 19 世紀の日付は地方平均時の offset が分で割り切れず、その zone では換算できない
    detail = _detail(display_timezone="Europe/Amsterdam", expires_at="1850-01-01T00:00:00Z")
    assert _deadline(render_approval(RECORD, detail)) == "期限: 1850-01-01 00:00 +00:00"


@pytest.mark.parametrize("status,label", [("approved", "承認済み"), ("rejected", "却下"),
                                           ("expired", "失効")])
def test_the_decided_time_of_a_terminal_post_is_converted(status, label):
    detail = _detail(status=status, decided_at="2026-10-19T14:16:00+00:00",
                     display_timezone="Asia/Tokyo")
    content = render_terminal(RECORD, detail)
    assert f"状態: {label} (2026-10-19 23:16 +09:00)" in content


def test_a_terminal_post_of_a_legacy_payload_converts_the_time_too():
    content = render_terminal(RECORD, {"status": "rejected", "display_timezone": "Asia/Tokyo",
                                       "decided_at": "2026-10-19T14:16:00+00:00"})
    assert content == "承認 #7\n\n状態: 却下 (2026-10-19 23:16 +09:00)\n\n旧形式のため要約なし"


@pytest.mark.parametrize("bad", ["Not/AZone", "A" * 300, None, 5, {"x": 1}])
def test_posting_does_not_fail_on_a_hostile_zone(bad):
    reply = Reply(200, _detail(display_timezone=bad), None)
    effect = _post_effect(RECORD, reply)
    assert "期限: 2026-10-26 00:00 +00:00" in effect.content


def test_posting_does_not_fail_when_the_environment_cannot_read_any_zone(monkeypatch):
    def unreadable(name):
        raise OSError("no data")

    monkeypatch.setattr(display_time, "ZoneInfo", unreadable)
    display_time.clear_zone_cache()
    reply = Reply(200, _detail(display_timezone="Asia/Tokyo"), None)
    # zone が読めなくても UTC の換算で出る (UTC は zone 名の読込みを要さない)
    assert "期限: 2026-10-26 00:00 +00:00" in _post_effect(RECORD, reply).content


def test_a_post_without_a_detail_shows_no_time():
    assert _post_effect(RECORD, None).content == "承認 #7\n詳細を取得できません"
