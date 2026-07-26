from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.timeutil import as_utc


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
