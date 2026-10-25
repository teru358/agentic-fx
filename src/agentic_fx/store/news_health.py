"""Validated persistent health state for individual news sources."""
from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from agentic_fx.store import fetch_attempts

N = 3
PREFIX = "news_source_health."
MAX_COUNT = 10**9
MAX_WAIT = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class Health:
    consecutive_failures: int = 0
    retry_after_streak: int = 0
    backoff_until: datetime | None = None
    backoff_cause: str | None = None
    reset_at: datetime | None = None
    reset_nonce: str | None = None
    last_outcome: str | None = None
    last_at: datetime | None = None
    last_count: int | None = None
    last_reason: str | None = None


@dataclass(frozen=True, slots=True)
class HealthRead:
    raw: str | None
    record: Health | None
    corrupt: bool


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("news health requires an aware datetime")
    return value.astimezone(timezone.utc)


def _key(name: str) -> str:
    if not isinstance(name, str) or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError("news health source name is invalid")
    return PREFIX + name


def stage(k: int) -> timedelta | None:
    """Return the bounded backoff stage; sub-threshold values have no wait."""
    if isinstance(k, bool) or not isinstance(k, int) or k < N:
        return None
    return min(timedelta(minutes=30) * (2 ** (min(k, N + 10) - N + 1)), MAX_WAIT)


def _integer(data: dict, key: str, nullable: bool = False) -> int | None:
    value = data.get(key)
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_COUNT:
        raise ValueError("invalid health counter")
    return value


def _instant(data: dict, key: str) -> datetime | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid health timestamp")
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _parse(raw: str) -> Health:
    data = json.loads(raw)
    if not isinstance(data, dict) or type(data.get("v")) is not int or data["v"] != 1:
        raise ValueError("invalid health record")
    cause, outcome = data.get("backoff_cause"), data.get("last_outcome")
    if cause not in (None, "failures", "refusals") or outcome not in (None, "ok", "failed", "rate_limited"):
        raise ValueError("invalid health enum")
    for key in ("reset_nonce", "last_reason"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ValueError("invalid health text")
    return Health(_integer(data, "consecutive_failures"), _integer(data, "retry_after_streak"),
                  _instant(data, "backoff_until"), cause, _instant(data, "reset_at"), data.get("reset_nonce"),
                  outcome, _instant(data, "last_at"), _integer(data, "last_count", True), data.get("last_reason"))  # type: ignore[arg-type]


def read(conn: sqlite3.Connection, name: str) -> HealthRead:
    """Read only; SQLite errors propagate and malformed records are marked corrupt."""
    row = conn.execute("SELECT value FROM alert_state WHERE key=?", (_key(name),)).fetchone()
    if row is None:
        return HealthRead(None, None, False)
    raw = row[0]
    try:
        return HealthRead(raw, _parse(raw), False)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, OverflowError):
        return HealthRead(raw, None, True)


def _encode(record: Health) -> str:
    data = asdict(record)
    data["v"] = 1
    for key, value in tuple(data.items()):
        if isinstance(value, datetime):
            data[key] = _utc(value).isoformat()
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def next_record(current: Health | None, outcome: str, now: datetime, *, count: int | None = None,
                reason: str | None = None, retry_after: timedelta | None = None) -> Health:
    """Build a record without I/O; bad outcome/time/count values raise ValueError."""
    now = _utc(now)
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
        raise ValueError("invalid health count")
    old = current or Health()
    if outcome == "ok":
        failures, refusals, duration, cause = 0, 0, None, None
    elif outcome == "failure":
        failures, refusals = min(old.consecutive_failures + 1, MAX_COUNT), 0
        duration, cause = stage(max(failures, refusals)), "failures"
    elif outcome == "refusal":
        failures, refusals = old.consecutive_failures, min(old.retry_after_streak + 1, MAX_COUNT)
        duration = stage(max(failures, refusals))
        cause = "failures" if failures >= refusals else "refusals"
    else:
        raise ValueError("invalid health outcome")
    if duration is None:
        cause = None
    return Health(failures, refusals, now + duration if duration else None, cause,
                  old.reset_at, old.reset_nonce,
                  {"ok": "ok", "failure": "failed", "refusal": "rate_limited"}[outcome],
                  now, count, reason)


def effective_wait(health: Health | None, attempt: tuple[datetime, timedelta] | None,
                   now: datetime) -> tuple[datetime, str] | None:
    """Return capped wait without writing; invalid input datetimes raise ValueError."""
    now = _utc(now)
    cap = now + MAX_WAIT
    choices: list[tuple[datetime, str]] = []
    if health and health.backoff_until and health.backoff_until > now:
        choices.append((min(health.backoff_until, cap), health.backoff_cause or "failures"))
    if attempt:
        attempted_at, retry_after = attempt
        until = min(_utc(attempted_at) + retry_after, cap)
        if retry_after > timedelta(0) and until > now:
            choices.append((until, "directive"))
    return max(choices, key=lambda value: value[0]) if choices else None


def load_attempt(conn: sqlite3.Connection, key: str) -> tuple[datetime, timedelta] | None:
    """Return the attempt record for a fetch_attempts key; a malformed value is None.

    sqlite3.Error propagates (the caller treats an unreadable store as "do not fetch").
    """
    try:
        return fetch_attempts.load(conn, key)
    except ValueError:
        return None


def read_effective_wait(conn: sqlite3.Connection, name: str, now: datetime) -> tuple[datetime, str] | None:
    """Read waits only; SQLite errors propagate and malformed attempts have no wait.

    A malformed health record is read as no health wait.  Never writes.
    """
    health = read(conn, name).record
    attempt = load_attempt(conn, fetch_attempts.news_source_key(name))
    return effective_wait(health, attempt, now)


def write_if_unchanged(conn: sqlite3.Connection, name: str, expected_raw: str | None, record: Health,
                       now: datetime, *, commit: bool = True) -> bool:
    """Compare and write in one SQL statement; SQLite errors and bad values propagate."""
    key, value, updated = _key(name), _encode(record), _utc(now).isoformat()
    if expected_raw is None:
        cursor = conn.execute("INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO NOTHING", (key, value, updated))
    else:
        cursor = conn.execute("UPDATE alert_state SET value=?,updated_at=? WHERE key=? AND value=?", (value, updated, key, expected_raw))
    if commit:
        conn.commit()
    return cursor.rowcount == 1


def cap_backoff_for_collector(conn: sqlite3.Connection, name: str, observed: HealthRead, now: datetime) -> HealthRead:
    """Persist a one-time 24-hour cap for a collector read; SQLite errors propagate."""
    if observed.record is None or observed.record.backoff_until is None:
        return observed
    cap = _utc(now) + MAX_WAIT
    if observed.record.backoff_until <= cap:
        return observed
    values = asdict(observed.record)
    values["backoff_until"] = cap
    corrected = Health(**values)
    if write_if_unchanged(conn, name, observed.raw, corrected, now):
        return HealthRead(_encode(corrected), corrected, False)
    return read(conn, name)


def initialize_for_enable(conn: sqlite3.Connection, name: str, now: datetime, *, nonce: str | None = None) -> None:
    """Initialize in the caller transaction; SQLite errors and invalid values propagate."""
    instant = _utc(now)
    record = Health(reset_at=instant, reset_nonce=nonce if nonce is not None else secrets.token_hex())
    conn.execute("INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (_key(name), _encode(record), instant.isoformat()))


def delete(conn: sqlite3.Connection, name: str, *, commit: bool = True) -> None:
    """Delete a record; SQLite errors and invalid values propagate."""
    conn.execute("DELETE FROM alert_state WHERE key=?", (_key(name),))
    if commit:
        conn.commit()
