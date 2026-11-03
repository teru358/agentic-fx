"""UTC/NY time rules shared by FX market-boundary code.

MT5 encodes its server wall clock as an epoch.  The server wall clock is
New York wall time plus seven hours; it is not an instant until that rule is
applied.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


UTC = timezone.utc
NY = ZoneInfo("America/New_York")
SERVER_AHEAD_OF_NY = timedelta(hours=7)
ROLLOVER_NY = time(17, 0)
SWING_CUTOFF_MARGIN = timedelta(hours=1)
# Exclusive upper bound for the Friday swing cutoff, in NY wall time.
FRIDAY_SWING_CUTOFF_LIMIT_NY = (
    datetime.combine(date(2000, 1, 1), ROLLOVER_NY) - SWING_CUTOFF_MARGIN
).time()

_log = logging.getLogger(__name__)


def _new_york_wall_to_utc(wall: datetime) -> datetime:
    """Interpret a naive NY wall time, retaining anomalous MT5 bars.

    Both folds must round-trip to the input wall clock.  That distinguishes a
    repeated fall-back hour from a nonexistent spring-forward hour without
    trusting the ``fold`` flag alone.
    """
    first = wall.replace(tzinfo=NY, fold=0)
    second = wall.replace(tzinfo=NY, fold=1)
    first_utc = first.astimezone(UTC)
    second_utc = second.astimezone(UTC)
    first_round_trips = first_utc.astimezone(NY).replace(tzinfo=None) == wall
    second_round_trips = second_utc.astimezone(NY).replace(tzinfo=None) == wall

    if first_round_trips and second_round_trips and first_utc != second_utc:
        _log.warning("ambiguous New York server wall time: %s", wall.isoformat())
        return first_utc
    if not first_round_trips or not second_round_trips:
        _log.warning("nonexistent New York server wall time: %s", wall.isoformat())
        return first_utc
    return first_utc


def server_epoch_to_utc(server_epoch: int) -> datetime:
    """Convert an MT5 server-wall-clock epoch to an aware UTC datetime."""
    server_wall = datetime.fromtimestamp(server_epoch, UTC).replace(tzinfo=None)
    ny_wall = server_wall - SERVER_AHEAD_OF_NY
    return _new_york_wall_to_utc(ny_wall)


def utc_to_server_epoch(utc: datetime) -> int:
    """Encode an aware UTC instant as the MT5 server-wall-clock epoch."""
    if utc.tzinfo is None or utc.utcoffset() != timedelta(0):
        raise TypeError("utc must be an aware datetime with a zero UTC offset")
    server_wall = utc.astimezone(NY).replace(tzinfo=None) + SERVER_AHEAD_OF_NY
    return int(server_wall.replace(tzinfo=UTC).timestamp())


def session_boundary_utc(day: date) -> datetime:
    """Return the NY 17:00 boundary for ``day`` as an aware UTC instant."""
    return datetime.combine(day, ROLLOVER_NY, tzinfo=NY).astimezone(UTC)
