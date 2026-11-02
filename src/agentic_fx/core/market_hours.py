"""FX market hours derived from the New York 17:00 daily boundary.

The market closes at New York 17:00: 21:00 UTC in summer and 22:00 UTC in
winter.  ``friday_swing_cutoff_ny`` remains earlier than 16:00 NY to leave a
conservative one-hour margin before that close.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from agentic_fx.core.fx_time import NY, ROLLOVER_NY, session_boundary_utc
from agentic_fx.core.timeutil import as_utc as _as_utc


_HOLIDAYS_MONTH_DAY = frozenset({(12, 25), (1, 1)})
DAY_CLOSE_BUFFER = timedelta(minutes=5)
_SESSION_SEARCH_MAX_DAYS = 14


def _ny(now: datetime) -> datetime:
    return _as_utc(now).astimezone(NY)


def trading_date(now: datetime) -> date:
    """Return the server-date trading label for an aware instant."""
    return (_ny(now) + timedelta(hours=7)).date()


def is_market_open(now: datetime) -> bool:
    """Whether the FX market is open at an aware instant."""
    local = _ny(now)
    label = trading_date(local)
    if (label.month, label.day) in _HOLIDAYS_MONTH_DAY:
        return False
    local_time = local.timetz().replace(tzinfo=None)
    if local.weekday() == 4 and local_time >= ROLLOVER_NY:
        return False
    if local.weekday() == 5:
        return False
    if local.weekday() == 6 and local_time < ROLLOVER_NY:
        return False
    return True


def trading_day_start(now: datetime) -> datetime:
    """Return the most recent NY 17:00 boundary as aware UTC."""
    local = _ny(now)
    day = local.date()
    if local.timetz().replace(tzinfo=None) < ROLLOVER_NY:
        day -= timedelta(days=1)
    return session_boundary_utc(day)


def next_rollover(now: datetime) -> datetime:
    """Return the next NY-calendar daily boundary as aware UTC."""
    local = _ny(now)
    day = local.date()
    if local.timetz().replace(tzinfo=None) >= ROLLOVER_NY:
        day += timedelta(days=1)
    return session_boundary_utc(day)


def session_start(now: datetime) -> datetime:
    """Return the most recent closed-to-open boundary, including holidays."""
    boundary = trading_day_start(now)
    for _ in range(_SESSION_SEARCH_MAX_DAYS):
        if is_market_open(boundary) and not is_market_open(boundary - timedelta(minutes=1)):
            return boundary
        previous_day = boundary.astimezone(NY).date() - timedelta(days=1)
        boundary = session_boundary_utc(previous_day)
    raise ValueError(
        f"no closed-to-open transition within {_SESSION_SEARCH_MAX_DAYS} "
        f"days before {now.isoformat()}")


def next_expected_trading_time(after: datetime, width: timedelta) -> datetime:
    """Return the next expected bar time while skipping known market closures."""
    candidate = _as_utc(after) + width
    while not is_market_open(candidate):
        candidate = next_rollover(candidate)
    return candidate


def next_bar_confirmation(bar_start: datetime, width: timedelta,
                          grace: timedelta) -> datetime:
    """Return when the following tradable bar is confirmed."""
    return next_expected_trading_time(bar_start, width) + width + grace


def is_friday_after(now: datetime, cutoff_hhmm: str) -> bool:
    """Whether an aware instant is at or after the configured NY Friday cutoff.

    The cutoff is intentionally before the NY 17:00 market close.
    """
    local = _ny(now)
    if local.weekday() != 4:
        return False
    hour, minute = map(int, cutoff_hhmm.split(":"))
    return local.timetz().replace(tzinfo=None) >= time(hour, minute)
