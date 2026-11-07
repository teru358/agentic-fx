"""Cutover from the fixed 21:00 UTC boundary to the New York 17:00 rule.

A daemon that starts on the new rule while positions and accounting rows from
the old rule are still alive must not loosen anything for the span between the
cutover instant ``T_c`` and the first correct boundary ``T_b``.  This module
owns the one-time marker and capture written at ``T_c`` and the four
transitional rules that apply only while ``T_c <= now < T_b``.

``legacy_*`` are frozen copies of the previous boundary functions.  The 21:00
UTC they use is the old rule itself, kept on purpose, and they are never
called outside the transitional window.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from agentic_fx.activity import Category
from agentic_fx.core import display_time, market_hours
from agentic_fx.core.contracts import OrderStatus as S
from agentic_fx.core.timeutil import as_utc

_log = logging.getLogger("agentic_fx.time_cutover")

MARKER_NAME = "market_hours_ny_rule_v1"
CAPTURE_KEY = "time_rule_cutover.capture"
ACTIVITY_EVENT = "time_rule_cutover"
REFUSED_EVENT = "time_rule_cutover_refused"
MARKER_MISSING = "time_rule_marker_missing"
# systemd の RestartPreventExitStatus で再起動を止めるための専用値 (75 / 76 の隣)。
EXIT_MARKER_MISSING = 78

_LEGACY_ROLLOVER_UTC = time(21, 0)
_LEGACY_SERVER_AHEAD = timedelta(hours=3)


# ---- frozen copies of the previous rule -----------------------------------

def legacy_trading_day_start(now: datetime) -> datetime:
    now = as_utc(now)
    boundary = now.replace(hour=21, minute=0, second=0, microsecond=0)
    if now.timetz().replace(tzinfo=None) < _LEGACY_ROLLOVER_UTC:
        boundary -= timedelta(days=1)
    return boundary


def legacy_next_rollover(now: datetime) -> datetime:
    now = as_utc(now)
    boundary = now.replace(hour=21, minute=0, second=0, microsecond=0)
    if now.timetz().replace(tzinfo=None) >= _LEGACY_ROLLOVER_UTC:
        boundary += timedelta(days=1)
    return boundary


def legacy_trading_date(now: datetime) -> date:
    return (as_utc(now) + _LEGACY_SERVER_AHEAD).date()


# ---- marker and window ----------------------------------------------------

def cutover_instant(conn: sqlite3.Connection) -> datetime | None:
    """``T_c``: the marker's ``applied_at``, or None before the cutover."""
    try:
        row = conn.execute(
            "SELECT applied_at FROM data_migrations WHERE name=?",
            (MARKER_NAME,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    if row is None:
        return None
    return as_utc(datetime.fromisoformat(row[0]))


def transition_end(t_c: datetime) -> datetime:
    """``T_b``: the first boundary of the new rule after ``T_c``."""
    return market_hours.next_rollover(t_c)


def active_window(conn: sqlite3.Connection,
                  now: datetime) -> tuple[datetime, datetime] | None:
    """``(T_c, T_b)`` while ``now`` is inside ``[T_c, T_b)``, else None."""
    t_c = cutover_instant(conn)
    if t_c is None:
        return None
    t_b = transition_end(t_c)
    if t_c <= as_utc(now) < t_b:
        return t_c, t_b
    return None


# ---- transitional rules ---------------------------------------------------

def day_close_rollover(window: tuple[datetime, datetime] | None,
                       anchor: datetime) -> datetime:
    """Boundary that closes a day position opened at ``anchor``.

    ``window`` is ``active_window(conn, now)``, read once by the caller.  A
    position that started before ``T_c`` takes the earlier of the two
    boundaries so the cutover never extends its life.
    """
    new = market_hours.next_rollover(anchor)
    if window is None or as_utc(anchor) >= window[0]:
        return new
    return min(legacy_next_rollover(anchor), new)


def earliest_day_close_rollover(anchor: datetime) -> datetime:
    """Boundary used when the window cannot be read: the earlier of the rules."""
    return min(legacy_next_rollover(anchor), market_hours.next_rollover(anchor))


def daily_base(conn: sqlite3.Connection, now: datetime,
               compute: Callable[[datetime], float | None]) -> float | None:
    """Daily-loss reference equity; ``compute`` maps a day start to equity.

    Inside the window the larger of the two references is used (a larger
    reference means a larger measured loss) and a missing one fails closed.
    """
    new = compute(market_hours.trading_day_start(now))
    if active_window(conn, now) is None:
        return new
    old = compute(legacy_trading_day_start(now))
    if new is None or old is None:
        return None
    return max(new, old)


def signal_day_start(conn: sqlite3.Connection, now: datetime) -> datetime:
    """Start of the day the signal daily cap counts from (earlier is stricter)."""
    new = market_hours.trading_day_start(now)
    if active_window(conn, now) is None:
        return new
    return min(legacy_trading_day_start(now), new)


def daily_label(conn: sqlite3.Connection, now: datetime) -> date:
    """Trading-date label for daily dedupe (the later label avoids re-sending)."""
    new = market_hours.trading_date(now)
    if active_window(conn, now) is None:
        return new
    return max(legacy_trading_date(now), new)


# ---- capture --------------------------------------------------------------

def _read_capture(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM alert_state WHERE key=?",
                       (CAPTURE_KEY,)).fetchone()
    return row[0] if row else None


def _base_entry(found: tuple[float, str] | None) -> dict | None:
    if found is None:
        return None
    return {"equity": found[0], "snapshot_ts": found[1]}


def build_capture(conn: sqlite3.Connection, t_c: datetime,
                  now: datetime) -> dict:
    """Snapshot of what the old and new rules would do at ``t_c``."""
    from agentic_fx.core import accounting

    t_c = as_utc(t_c)
    now = as_utc(now)
    buffer = market_hours.DAY_CLOSE_BUFFER
    rows = []
    for (order_id, pair, horizon, status, filled_at,
         created_at) in conn.execute(
            "SELECT id, pair, horizon, status, filled_at, created_at FROM orders "
            "WHERE status IN (?, ?) ORDER BY id",
            (str(S.OPEN), str(S.PENDING_FILL))).fetchall():
        anchor = filled_at or created_at
        entry = {"id": order_id, "pair": pair, "horizon": horizon,
                 "status": status, "anchor": anchor, "day_deadline": None}
        if horizon == "day":
            try:
                started = as_utc(datetime.fromisoformat(anchor))
                legacy = legacy_next_rollover(started) - buffer
                new = market_hours.next_rollover(started) - buffer
                applied = min(legacy, new) if started < t_c else new
                entry["day_deadline"] = {"legacy": legacy.isoformat(),
                                         "new": new.isoformat(),
                                         "applied": applied.isoformat()}
            except (TypeError, ValueError):
                entry["anchor_invalid"] = True
        rows.append(entry)
    legacy_base = accounting.daily_start_snapshot(
        conn, now, legacy_trading_day_start(now))
    new_base = accounting.daily_start_snapshot(
        conn, now, market_hours.trading_day_start(now))
    if legacy_base is None or new_base is None:
        applied_base = None
    else:
        applied_base = max(legacy_base[0], new_base[0])
    return {"t_c": t_c.isoformat(),
            "t_b": transition_end(t_c).isoformat(),
            "captured_at": now.isoformat(),
            "orders": rows,
            "daily_base": {"legacy": _base_entry(legacy_base),
                           "new": _base_entry(new_base),
                           "applied": applied_base},
            "activity_logged": False}


def _dump(capture: dict) -> str:
    return json.dumps(capture, sort_keys=True, ensure_ascii=False)


def _begin_write(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise RuntimeError("time rule cutover needs a connection without a pending transaction")
    conn.execute("BEGIN IMMEDIATE")


def _write_marker_and_capture(conn: sqlite3.Connection, t_c: datetime) -> None:
    """Insert the marker and the capture in one transaction."""
    _begin_write(conn)
    try:
        conn.execute(
            "INSERT INTO data_migrations(name, applied_at) VALUES (?, ?)",
            (MARKER_NAME, t_c.isoformat()))
        capture = build_capture(conn, t_c, t_c)
        conn.execute(
            "INSERT INTO alert_state(key, value, updated_at) VALUES (?,?,?)",
            (CAPTURE_KEY, _dump(capture), t_c.isoformat()))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _supplement_capture(conn: sqlite3.Connection, t_c: datetime,
                        now: datetime) -> None:
    """Add the missing capture for an existing marker; ``t_c`` does not move."""
    _begin_write(conn)
    try:
        if _read_capture(conn) is None:
            capture = build_capture(conn, t_c, now)
            capture["supplemented_at"] = now.isoformat()
            conn.execute(
                "INSERT INTO alert_state(key, value, updated_at) VALUES (?,?,?)",
                (CAPTURE_KEY, _dump(capture), now.isoformat()))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


# ---- activity, once -------------------------------------------------------

def activity_ref(t_c: datetime) -> str:
    return f"{ACTIVITY_EVENT}:{as_utc(t_c).isoformat()}"


def _mark_activity_logged(conn: sqlite3.Connection, capture: dict,
                          now: datetime) -> None:
    capture["activity_logged"] = True
    conn.execute(
        "UPDATE alert_state SET value=?, updated_at=? WHERE key=?",
        (_dump(capture), now.isoformat(), CAPTURE_KEY))
    conn.commit()


INVALID_CAPTURE_EVENT = "time_rule_cutover_capture_invalid"
INVALID_CAPTURE_NOTE = "time rule capture is unreadable; start continues, audit record skipped"


def _note_capture_invalid(activity, t_c: datetime) -> None:
    """Tell the reader of the activity log, once, that the capture is unusable."""
    ref = f"{INVALID_CAPTURE_EVENT}:{as_utc(t_c).isoformat()}"
    try:
        if activity.contains(INVALID_CAPTURE_EVENT, ref):
            return
        activity.end_partial_line()
        activity.write(Category.SYSTEM, INVALID_CAPTURE_EVENT,
                       INVALID_CAPTURE_NOTE, ref_id=ref)
    except OSError as exc:
        _log.warning("time rule capture notice failed: %s", exc)


def _log_activity_once(conn: sqlite3.Connection, activity, t_c: datetime,
                       now: datetime) -> None:
    """Record the capture in the activity log exactly once.

    ``ActivityLog.write`` reports nothing on failure, so success is decided by
    reading the log back for the stable ``ref_id``; ``activity_logged`` turns
    true only after that, and a missing confirmation leaves it false for the
    next start.
    """
    raw = _read_capture(conn)
    if raw is None:
        return
    try:
        capture = json.loads(raw)
    except ValueError:
        capture = None
    if not isinstance(capture, dict):
        _log.warning("time rule capture is unusable; activity skipped")
        _note_capture_invalid(activity, t_c)
        return
    if capture.get("activity_logged") is True:
        return
    ref = activity_ref(t_c)
    try:
        present = activity.contains(ACTIVITY_EVENT, ref)
        if not present:
            activity.end_partial_line()
            activity.write(*display_time.time_rule_cutover(capture), ref_id=ref)
            present = activity.contains(ACTIVITY_EVENT, ref)
    except OSError as exc:
        _log.warning("time rule activity check failed: %s", exc)
        return
    if present:
        _mark_activity_logged(conn, capture, now)


# ---- startup --------------------------------------------------------------

def ensure_cutover(conn: sqlite3.Connection, *, now: datetime, activity) -> None:
    """Decide the startup case from the marker and the capture.

    Call with the instance lock held and before the first tick.  Raises
    ``PreflightFailure("time_rule_marker_missing")`` when a capture exists
    without its marker.  No code path removes the marker.
    """
    now = as_utc(now)
    marker = cutover_instant(conn)
    capture = _read_capture(conn)
    if marker is None and capture is not None:
        message = ("time rule marker is missing while its capture exists; "
                   "the marker must not be removed")
        activity.write(Category.SYSTEM, REFUSED_EVENT, message)
        from agentic_fx.setup.preflight import PreflightFailure
        raise PreflightFailure(MARKER_MISSING, message)
    if marker is None:
        _write_marker_and_capture(conn, now)
        marker = now
    elif capture is None:
        _supplement_capture(conn, marker, now)
    _log_activity_once(conn, activity, marker, now)
