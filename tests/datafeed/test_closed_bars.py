from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.closed_bars import normalize_closed_range


UTC = timezone.utc


def _bar(ts: datetime, close: float = 1.0) -> Bar:
    return Bar("USDJPY", "1m", ts, close, close, close, close, 0.0)


def test_normalize_closed_range_excludes_right_edge_forming_and_deduplicates():
    start = datetime(2026, 9, 21, 8, 58, tzinfo=UTC)
    cutoff = datetime(2026, 9, 21, 9, 0, 30, tzinfo=UTC)
    bars = [_bar(start), _bar(start), _bar(start + timedelta(minutes=1)),
            _bar(start + timedelta(minutes=2))]

    result = normalize_closed_range(
        bars, interval="1m", start=start, end=start + timedelta(minutes=2),
        cutoff=cutoff, grace=timedelta(seconds=30))

    assert [bar.ts for bar in result] == [start, start + timedelta(minutes=1)]


def test_normalize_closed_range_requires_aware_datetimes():
    with pytest.raises(ValueError, match="timezone-aware"):
        normalize_closed_range(
            [_bar(datetime(2026, 9, 21, 9, 0, tzinfo=UTC))], interval="1m",
            start=datetime(2026, 9, 21, 8, 0),
            end=datetime(2026, 9, 21, 10, 0, tzinfo=UTC),
            cutoff=datetime(2026, 9, 21, 10, 0, tzinfo=UTC),
            grace=timedelta(seconds=30))
