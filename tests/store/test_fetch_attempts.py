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


# --- 取得結果 (成功・失敗) と鮮度 -------------------------------------------

def _raw(conn):
    import json
    return json.loads(conn.execute(
        "SELECT value FROM alert_state WHERE key=?", (fa.ECON_KEY,)).fetchone()[0])


def test_freshness_without_a_row_is_unknown(conn):
    assert fa.freshness(conn, fa.ECON_KEY, NOW) == "unknown"


def test_outcome_does_not_change_the_attempt_or_the_retry_after(conn):
    fa.record_attempt(conn, fa.ECON_KEY, NOW)
    fa.record_retry_after(conn, fa.ECON_KEY, NOW, 2 * HOUR)
    fa.record_outcome(conn, fa.ECON_KEY, NOW + HOUR, ok=True)
    assert fa.load(conn, fa.ECON_KEY) == (NOW, 2 * HOUR)
    assert _raw(conn)["last_success_at"] == (NOW + HOUR).isoformat()


def test_a_new_attempt_or_retry_after_keeps_the_recorded_outcome(conn):
    fa.record_outcome(conn, fa.ECON_KEY, NOW, ok=True)
    fa.record_attempt(conn, fa.ECON_KEY, NOW + HOUR)
    fa.record_retry_after(conn, fa.ECON_KEY, NOW + HOUR, HOUR)
    raw = _raw(conn)
    assert raw["last_success_at"] == NOW.isoformat()
    assert raw["last_outcome"] == "success"
    assert fa.freshness(conn, fa.ECON_KEY, NOW + HOUR) == "fresh"


def test_failure_keeps_the_last_success_time_and_turns_stale(conn):
    fa.record_outcome(conn, fa.ECON_KEY, NOW, ok=True)
    fa.record_outcome(conn, fa.ECON_KEY, NOW + HOUR, ok=False)
    raw = _raw(conn)
    assert raw["last_success_at"] == NOW.isoformat()
    assert raw["last_outcome"] == "failure"
    assert raw["last_outcome_at"] == (NOW + HOUR).isoformat()
    assert fa.freshness(conn, fa.ECON_KEY, NOW + HOUR) == "stale"
    fa.record_outcome(conn, fa.ECON_KEY, NOW + 2 * HOUR, ok=True)
    assert fa.freshness(conn, fa.ECON_KEY, NOW + 2 * HOUR) == "fresh"


def test_failure_without_any_success_stays_unknown(conn):
    fa.record_outcome(conn, fa.ECON_KEY, NOW, ok=False)
    assert fa.freshness(conn, fa.ECON_KEY, NOW) == "unknown"


def test_freshness_threshold_is_twice_the_interval_and_inclusive(conn):
    fa.record_outcome(conn, fa.ECON_KEY, NOW, ok=True)
    interval = timedelta(hours=6)
    assert fa.freshness(conn, fa.ECON_KEY, NOW + 2 * interval,
                        refresh_interval=interval) == "fresh"
    assert fa.freshness(conn, fa.ECON_KEY, NOW + 2 * interval
                        + timedelta(microseconds=1),
                        refresh_interval=interval) == "stale"
    assert fa.freshness(conn, fa.ECON_KEY, NOW + timedelta(hours=12),
                        refresh_interval=timedelta(hours=1)) == "stale"
    assert fa.ECON_REFRESH_INTERVAL == interval


@pytest.mark.parametrize("value", [
    "{not json", "[]", '{"last_success_at": 5}', '{"last_success_at": "x"}',
    '{"last_success_at": "2026-10-02T10:38:00"}'])
def test_unreadable_values_are_unknown(conn, value):
    conn.execute("INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?)",
                 (fa.ECON_KEY, value, NOW.isoformat()))
    conn.commit()
    assert fa.freshness(conn, fa.ECON_KEY, NOW) == "unknown"


def test_outcome_over_a_broken_value_starts_again_and_naive_is_refused(conn):
    conn.execute("INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?)",
                 (fa.ECON_KEY, "{broken", NOW.isoformat()))
    conn.commit()
    fa.record_outcome(conn, fa.ECON_KEY, NOW, ok=True)
    assert fa.freshness(conn, fa.ECON_KEY, NOW) == "fresh"
    assert fa.load(conn, fa.ECON_KEY) == (NOW, timedelta(0))
    with pytest.raises(ValueError):
        fa.record_outcome(conn, fa.ECON_KEY, datetime(2026, 10, 2, 10, 38), ok=True)
    with pytest.raises(ValueError):
        fa.freshness(conn, fa.ECON_KEY, datetime(2026, 10, 2, 10, 38))


def test_news_keys_are_not_affected_by_the_econ_outcome(conn):
    key = fa.news_source_key("src")
    fa.record_attempt(conn, key, NOW)
    fa.record_outcome(conn, fa.ECON_KEY, NOW, ok=True)
    assert fa.load(conn, key) == (NOW, timedelta(0))
    assert "last_outcome" not in conn.execute(
        "SELECT value FROM alert_state WHERE key=?", (key,)).fetchone()[0]
