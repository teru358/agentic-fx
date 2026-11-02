from datetime import datetime, timedelta, timezone

import pytest


UTC = timezone.utc


def _server_epoch(year: int, month: int, day: int, hour: int, minute: int) -> int:
    """MT5 server wall time encoded as an epoch, as supplied by the bridge."""
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


@pytest.mark.parametrize(("server_epoch", "expected"), [
    (_server_epoch(2026, 7, 24, 23, 59), datetime(2026, 7, 24, 20, 59, tzinfo=UTC)),
    (_server_epoch(2026, 11, 6, 23, 59), datetime(2026, 11, 6, 21, 59, tzinfo=UTC)),
    (_server_epoch(2026, 11, 2, 0, 0), datetime(2026, 11, 1, 22, 0, tzinfo=UTC)),
    (_server_epoch(2026, 3, 7, 0, 0), datetime(2026, 3, 6, 22, 0, tzinfo=UTC)),
    (_server_epoch(2026, 3, 9, 0, 0), datetime(2026, 3, 8, 21, 0, tzinfo=UTC)),
])
def test_server_epoch_to_utc_uses_new_york_dst_rule(server_epoch, expected):
    from agentic_fx.core.fx_time import server_epoch_to_utc

    actual = server_epoch_to_utc(server_epoch)

    assert actual == expected
    assert actual.tzinfo is UTC


def test_utc_to_server_epoch_rejects_non_utc_and_round_trips_regular_instants():
    from agentic_fx.core.fx_time import server_epoch_to_utc, utc_to_server_epoch

    with pytest.raises(TypeError):
        utc_to_server_epoch(datetime(2026, 7, 24, 20, 59))
    with pytest.raises(TypeError):
        utc_to_server_epoch(datetime(2026, 7, 24, 20, 59,
                                     tzinfo=timezone(timedelta(hours=9))))

    for instant in (
        datetime(2026, 3, 8, 6, 59, tzinfo=UTC),
        datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
        datetime(2026, 11, 1, 5, 59, tzinfo=UTC),
        datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        datetime(2027, 3, 14, 7, 0, tzinfo=UTC),
    ):
        assert server_epoch_to_utc(utc_to_server_epoch(instant)) == instant


@pytest.mark.parametrize(("server_epoch", "expected", "warning"), [
    (_server_epoch(2026, 11, 1, 8, 30), datetime(2026, 11, 1, 5, 30, tzinfo=UTC), "ambiguous"),
    (_server_epoch(2026, 3, 8, 9, 30), datetime(2026, 3, 8, 7, 30, tzinfo=UTC), "nonexistent"),
    (_server_epoch(2027, 3, 14, 9, 30), datetime(2027, 3, 14, 7, 30, tzinfo=UTC), "nonexistent"),
])
def test_server_epoch_to_utc_retains_dst_anomalies_with_a_warning(
        caplog, server_epoch, expected, warning):
    from agentic_fx.core.fx_time import server_epoch_to_utc

    assert server_epoch_to_utc(server_epoch) == expected
    assert warning in caplog.text
