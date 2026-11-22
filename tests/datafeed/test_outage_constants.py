"""停止の判定が他の部品と共有する定数の突き合わせ。値の写しが元から離れたら落ちる。"""
from datetime import timedelta

from agentic_fx.core import protection_contact, scheduler
from agentic_fx.datafeed import guidance_text, outage, outage_inspect, outage_transition


def test_the_interruption_threshold_is_two_scheduler_ticks():
    assert outage._INTERRUPTION_SEC == 2 * scheduler.TICK_INTERVAL_SEC


def test_the_latest_bar_freshness_matches_the_normal_protection():
    assert outage_inspect.LATEST_BAR_FRESHNESS == scheduler._BAR_FRESHNESS


def test_the_bar_width_is_one_minute_like_the_contact_evaluation():
    assert outage_inspect.BAR_WIDTH == timedelta(minutes=1) == protection_contact._BAR_WIDTH


def test_the_agreed_grace_values():
    assert outage_transition.EXPOSURE_STALL_SEC == 600
    assert outage_transition.RESTRICTED_MAX_SEC == 1800


def test_every_contact_kind_has_a_display_word():
    assert set(guidance_text._CONTACT_KIND_TEXT) == set(protection_contact.KINDS)


def test_the_contact_notice_key_is_one_word_shared_with_the_sweep():
    from agentic_fx.ops import datafeed_notices
    assert outage_transition.CONTACT_NOTICE_KEY == datafeed_notices.CONTACT_NOTICE_KEY == "contact"
