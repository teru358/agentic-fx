from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.store import fetch_attempts as fa
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 10, 2, 10, 38, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


def test_no_record_means_no_anchor(conn):
    assert fa.due_anchor(conn, fa.ECON_KEY, now=NOW, interval=HOUR) is None


def test_anchor_is_the_last_attempt_time(conn):
    fa.record_attempt(conn, fa.ECON_KEY, NOW)
    later = NOW + timedelta(minutes=20)
    assert fa.due_anchor(conn, fa.ECON_KEY, now=later, interval=HOUR) == NOW


def test_retry_after_longer_than_interval_pushes_anchor(conn):
    fa.record_attempt(conn, fa.ECON_KEY, NOW)
    fa.record_retry_after(conn, fa.ECON_KEY, NOW, timedelta(hours=3))
    anchor = fa.due_anchor(conn, fa.ECON_KEY, now=NOW, interval=HOUR)
    assert anchor + HOUR == NOW + timedelta(hours=3)


def test_retry_after_is_capped(conn):
    fa.record_attempt(conn, fa.ECON_KEY, NOW)
    fa.record_retry_after(conn, fa.ECON_KEY, NOW, timedelta(days=400))
    anchor = fa.due_anchor(conn, fa.ECON_KEY, now=NOW, interval=HOUR)
    assert anchor + HOUR == NOW + fa.MAX_RETRY_AFTER


def test_new_attempt_clears_previous_retry_after(conn):
    fa.record_attempt(conn, fa.ECON_KEY, NOW)
    fa.record_retry_after(conn, fa.ECON_KEY, NOW, timedelta(hours=3))
    fa.record_attempt(conn, fa.ECON_KEY, NOW + timedelta(hours=4))
    assert fa.blocked_until(conn, fa.ECON_KEY, NOW + timedelta(hours=4)) is None


def test_blocked_until_only_while_retry_after_is_pending(conn):
    key = fa.news_source_key("fxstreet")
    fa.record_attempt(conn, key, NOW)
    assert fa.blocked_until(conn, key, NOW) is None        # Retry-After 無し
    fa.record_retry_after(conn, key, NOW, timedelta(minutes=45))
    assert fa.blocked_until(conn, key, NOW + timedelta(minutes=44)) == \
        NOW + timedelta(minutes=45)
    assert fa.blocked_until(conn, key, NOW + timedelta(minutes=45)) is None


def test_future_attempt_is_rebased_to_now(conn):
    fa.record_attempt(conn, fa.ECON_KEY, NOW + timedelta(days=1))
    assert fa.due_anchor(conn, fa.ECON_KEY, now=NOW, interval=HOUR) == NOW
    assert fa.load(conn, fa.ECON_KEY)[0] == NOW


def test_naive_datetime_is_rejected(conn):
    with pytest.raises(ValueError):
        fa.record_attempt(conn, fa.ECON_KEY, NOW.replace(tzinfo=None))


@pytest.mark.parametrize("raw", [
    "Infinity", "-Infinity", "NaN", "1e308", "-5", '"abc"', "86401"])
def test_corrupt_retry_after_is_not_a_retry_after_nor_an_overflow(conn, raw):
    stored = ('{"attempted_at": "%s", "retry_after_sec": %s}'
              % (NOW.isoformat(), raw))
    with conn:
        conn.execute(
            "INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?)",
            (fa.ECON_KEY, stored, NOW.isoformat()))
    with pytest.raises(ValueError):
        fa.load(conn, fa.ECON_KEY)
    assert fa.blocked_until(conn, fa.ECON_KEY, NOW) is None
    assert fa.due_anchor(conn, fa.ECON_KEY, now=NOW,
                         interval=timedelta(hours=6)) is not None
