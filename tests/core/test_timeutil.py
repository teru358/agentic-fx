from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from agentic_fx.core.timeutil import as_utc, to_display


def test_as_utc_converts_non_utc_tz_aware():
    jst = timezone(timedelta(hours=9))
    jst_time = datetime(2026, 7, 25, 5, 0, tzinfo=jst)
    assert as_utc(jst_time) == datetime(2026, 7, 24, 20, 0, tzinfo=timezone.utc)


def test_as_utc_passes_through_utc():
    utc_time = datetime(2026, 7, 24, 20, 0, tzinfo=timezone.utc)
    assert as_utc(utc_time) == utc_time


def test_as_utc_rejects_naive():
    naive = datetime(2026, 7, 24, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        as_utc(naive)


def test_to_display_utc_to_tokyo():
    utc_time = datetime(2026, 7, 24, 5, 0, tzinfo=timezone.utc)
    result = to_display(utc_time, "Asia/Tokyo")
    assert result == datetime(2026, 7, 24, 14, 0, tzinfo=ZoneInfo("Asia/Tokyo"))


def test_to_display_dst_timezone_summer_and_winter():
    # NY 夏時間 (EDT, UTC-4)
    summer = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
    result_summer = to_display(summer, "America/New_York")
    assert result_summer.hour == 8
    assert result_summer.utcoffset() == timedelta(hours=-4)

    # NY 冬時間 (EST, UTC-5)
    winter = datetime(2026, 1, 24, 12, 0, tzinfo=timezone.utc)
    result_winter = to_display(winter, "America/New_York")
    assert result_winter.hour == 7
    assert result_winter.utcoffset() == timedelta(hours=-5)


def test_to_display_rejects_naive():
    naive = datetime(2026, 7, 24, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        to_display(naive, "Asia/Tokyo")
