import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.store import fetch_attempts, news_health
from agentic_fx.store.db import connect, connect_observer_readonly, init_db
from agentic_fx.store.news_health import (
    Health, delete, effective_wait, initialize_for_enable, next_record, read,
    read_effective_wait, stage, write_if_unchanged,
)

NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
H = timedelta(hours=1)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "h.db")
    init_db(c)
    yield c
    c.close()


# ---- stage -----------------------------------------------------------------

@pytest.mark.parametrize("k,expected", [
    (0, None), (1, None), (2, None), (3, H), (4, 2 * H), (5, 4 * H), (6, 8 * H),
    (7, 16 * H), (8, 24 * H), (9, 24 * H), (13, 24 * H), (14, 24 * H), (10**9, 24 * H),
    (10**30, 24 * H),
])
def test_stage_table(k, expected):
    assert stage(k) == expected


def test_stage_threshold_constant_and_rejects_non_integers():
    assert news_health.N == 3
    assert stage(True) is None and stage(-5) is None


# ---- record building -------------------------------------------------------

def test_refusal_preserves_failures_and_success_resets_both():
    failed = next_record(Health(consecutive_failures=2), "failure", NOW, reason="HTTP 500")
    refused = next_record(failed, "refusal", NOW, reason="HTTP 429")
    assert (refused.consecutive_failures, refused.retry_after_streak) == (3, 1)
    assert refused.backoff_until == NOW + H and refused.backoff_cause == "failures"
    ok = next_record(refused, "ok", NOW, count=0)
    assert (ok.consecutive_failures, ok.retry_after_streak, ok.backoff_until, ok.backoff_cause) == (0, 0, None, None)
    assert (ok.last_outcome, ok.last_count, ok.last_reason) == ("ok", 0, None)


def test_failure_resets_refusal_streak_and_stage_comes_from_the_larger_counter():
    result = next_record(Health(consecutive_failures=2, retry_after_streak=8), "failure", NOW, reason="HTTP 404")
    assert (result.consecutive_failures, result.retry_after_streak) == (3, 0)
    assert result.backoff_until == NOW + H and result.backoff_cause == "failures"


def test_refusal_cause_and_deadline_use_max_of_both_counters():
    r = next_record(Health(consecutive_failures=2, retry_after_streak=4), "refusal", NOW, reason="HTTP 503")
    assert (r.consecutive_failures, r.retry_after_streak, r.backoff_cause) == (2, 5, "refusals")
    assert r.backoff_until == NOW + 4 * H
    tie = next_record(Health(consecutive_failures=4, retry_after_streak=3), "refusal", NOW, reason="HTTP 503")
    assert tie.backoff_cause == "failures" and tie.backoff_until == NOW + 2 * H
    below = next_record(Health(retry_after_streak=1), "refusal", NOW, reason="HTTP 503")
    assert (below.backoff_until, below.backoff_cause) == (None, None)


def test_next_record_keeps_enable_marker_and_saturates():
    base = Health(consecutive_failures=10**9, reset_at=NOW, reset_nonce="n")
    r = next_record(base, "failure", NOW, reason="x")
    assert r.consecutive_failures == 10**9
    assert (r.reset_at, r.reset_nonce) == (NOW, "n")
    assert r.backoff_until == NOW + 24 * H


def test_next_record_rejects_bad_input():
    with pytest.raises(ValueError):
        next_record(None, "failure", datetime(2026, 1, 1), reason="x")
    with pytest.raises(ValueError):
        next_record(None, "bogus", NOW)
    with pytest.raises(ValueError):
        next_record(None, "ok", NOW, count=-1)


# ---- effective wait --------------------------------------------------------

def test_effective_wait_picks_the_later_component_and_names_it():
    h = Health(backoff_until=NOW + 2 * H, backoff_cause="refusals")
    assert effective_wait(h, None, NOW) == (NOW + 2 * H, "refusals")
    assert effective_wait(h, (NOW, timedelta(hours=3)), NOW) == (NOW + 3 * H, "directive")
    assert effective_wait(h, (NOW, timedelta(hours=1)), NOW) == (NOW + 2 * H, "refusals")
    assert effective_wait(None, (NOW, timedelta(hours=1)), NOW) == (NOW + H, "directive")
    assert effective_wait(None, None, NOW) is None
    assert effective_wait(Health(), (NOW - 2 * H, H), NOW) is None
    assert effective_wait(Health(backoff_until=NOW), None, NOW) is None


def test_effective_wait_caps_each_component_at_one_day():
    h = Health(backoff_until=NOW + timedelta(days=10), backoff_cause="failures")
    assert effective_wait(h, None, NOW) == (NOW + 24 * H, "failures")
    future = NOW + timedelta(days=10)
    assert effective_wait(None, (future, 24 * H), NOW) == (NOW + 24 * H, "directive")


# ---- reading ---------------------------------------------------------------

def _row(conn, name, value):
    conn.execute("INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (news_health.PREFIX + name, value, NOW.isoformat()))
    conn.commit()


def test_read_missing_row(conn):
    assert read(conn, "src") == news_health.HealthRead(None, None, False)


def test_roundtrip_drops_unknown_fields(conn):
    record = Health(consecutive_failures=3, retry_after_streak=1, backoff_until=NOW + H,
                    backoff_cause="failures", reset_at=NOW, reset_nonce="n",
                    last_outcome="failed", last_at=NOW, last_count=None, last_reason="HTTP 500")
    assert write_if_unchanged(conn, "src", None, record, NOW)
    got = read(conn, "src")
    assert got.record == record and not got.corrupt
    data = json.loads(got.raw)
    assert data["v"] == 1
    data["future_field"] = "x"
    _row(conn, "src", json.dumps(data))
    assert read(conn, "src").record == record
    assert write_if_unchanged(conn, "src", json.dumps(data), record, NOW)
    assert "future_field" not in json.loads(read(conn, "src").raw)


def _valid(**over):
    base = {"v": 1, "consecutive_failures": 0, "retry_after_streak": 0, "backoff_until": None,
            "backoff_cause": None, "reset_at": None, "reset_nonce": None, "last_outcome": None,
            "last_at": None, "last_count": None, "last_reason": None}
    base.update(over)
    return json.dumps(base)


@pytest.mark.parametrize("raw", [
    "not json", "[]", "null", "5", '"x"', "{}", _valid(v=2), _valid(v=True), _valid(v=1.0),
    _valid(consecutive_failures=True), _valid(consecutive_failures=-1),
    _valid(consecutive_failures="3"), _valid(consecutive_failures=2.5),
    _valid(consecutive_failures=None), _valid(retry_after_streak=10**9 + 1),
    _valid(backoff_until="2026-10-25T00:00:00"), _valid(backoff_until=5),
    _valid(backoff_until="garbage"), _valid(backoff_cause="x"), _valid(last_outcome="x"),
    _valid(last_at="2026-10-25T00:00:00"), _valid(last_count=-1), _valid(last_count=True),
    _valid(reset_nonce=5), _valid(last_reason=5), "9" * 5000,
])
def test_corrupt_values_are_marked_and_raw_is_kept(conn, raw):
    _row(conn, "src", raw)
    got = read(conn, "src")
    assert got.corrupt and got.record is None and got.raw == raw


def test_valid_extremes_are_accepted(conn):
    _row(conn, "src", _valid(consecutive_failures=10**9, retry_after_streak=0,
                             backoff_until="2026-10-25T09:00:00+09:00"))
    got = read(conn, "src")
    assert not got.corrupt
    assert got.record.consecutive_failures == 10**9
    assert got.record.backoff_until == datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)


def test_key_accepts_legacy_names_and_rejects_control_characters(conn):
    for name in ("Upper", "x" * 80, "a b", "日本語"):
        assert read(conn, name).raw is None
    for name in ("a\x00b", "a\nb", "a\x7fb", ""[:0] + "\t"):
        with pytest.raises(ValueError):
            read(conn, name)
        with pytest.raises(ValueError):
            write_if_unchanged(conn, name, None, Health(), NOW)
    assert not any(r[0].startswith("news_source_health.") for r in
                   conn.execute("SELECT key FROM alert_state"))


def test_read_works_on_a_read_only_connection_and_never_writes(tmp_path):
    path = tmp_path / "ro.db"
    c = connect(path)
    init_db(c)
    write_if_unchanged(c, "src", None, Health(consecutive_failures=4), NOW)
    c.close()
    ro = connect_observer_readonly(path)
    try:
        assert read(ro, "src").record.consecutive_failures == 4
        assert read(ro, "missing").raw is None
        assert read_effective_wait(ro, "src", NOW) is None
    finally:
        ro.close()


def test_sqlite_errors_propagate(tmp_path):
    c = connect(tmp_path / "e.db")
    init_db(c)
    c.close()
    with pytest.raises(sqlite3.ProgrammingError):
        read(c, "src")
    with pytest.raises(sqlite3.ProgrammingError):
        read_effective_wait(c, "src", NOW)


def test_read_effective_wait_combines_health_and_attempt(conn):
    write_if_unchanged(conn, "src", None, Health(consecutive_failures=3, backoff_until=NOW + H,
                                                 backoff_cause="failures"), NOW)
    assert read_effective_wait(conn, "src", NOW) == (NOW + H, "failures")
    key = fetch_attempts.news_source_key("src")
    fetch_attempts.record_attempt(conn, key, NOW)
    fetch_attempts.record_retry_after(conn, key, NOW, 3 * H)
    assert read_effective_wait(conn, "src", NOW) == (NOW + 3 * H, "directive")
    conn.execute("UPDATE alert_state SET value='junk' WHERE key=?", (key,))
    assert read_effective_wait(conn, "src", NOW) == (NOW + H, "failures")


# ---- conditional write -----------------------------------------------------

def test_conditional_write_semantics(conn):
    first = Health(consecutive_failures=1)
    assert write_if_unchanged(conn, "src", None, first, NOW)
    assert not write_if_unchanged(conn, "src", None, Health(consecutive_failures=9), NOW)
    raw = read(conn, "src").raw
    assert not write_if_unchanged(conn, "src", raw + " ", Health(consecutive_failures=9), NOW)
    assert read(conn, "src").record.consecutive_failures == 1
    assert write_if_unchanged(conn, "src", raw, Health(consecutive_failures=2), NOW)
    assert read(conn, "src").record.consecutive_failures == 2


def test_conditional_write_commit_flag(tmp_path):
    path = tmp_path / "c.db"
    a = connect(path)
    init_db(a)
    b = connect(path)
    try:
        assert write_if_unchanged(a, "src", None, Health(), NOW, commit=False)
        assert read(b, "src").raw is None          # not visible before commit
        a.commit()
        assert read(b, "src").raw is not None
        assert write_if_unchanged(a, "other", None, Health(), NOW)   # commits by default
        assert read(b, "other").raw is not None
    finally:
        a.close()
        b.close()


def test_collector_cap_rewrites_only_when_needed_and_loses_gracefully(conn):
    far = Health(consecutive_failures=5, backoff_until=NOW + timedelta(days=9), backoff_cause="failures")
    write_if_unchanged(conn, "src", None, far, NOW)
    observed = read(conn, "src")
    capped = news_health.cap_backoff_for_collector(conn, "src", observed, NOW)
    assert capped.record.backoff_until == NOW + 24 * H
    assert read(conn, "src").record.backoff_until == NOW + 24 * H
    again = news_health.cap_backoff_for_collector(conn, "src", read(conn, "src"), NOW + H)
    assert again.record.backoff_until == NOW + 24 * H
    # an enable lands between the read and the cap: the cap yields to the fresh row
    stale = read(conn, "src")
    initialize_for_enable(conn, "src", NOW, nonce="n")
    got = news_health.cap_backoff_for_collector(
        conn, "src", news_health.HealthRead(stale.raw, far, False), NOW)
    assert got.record.reset_nonce == "n"
    missing = news_health.HealthRead(None, None, False)
    assert news_health.cap_backoff_for_collector(conn, "src", missing, NOW) is missing


# ---- enable / delete -------------------------------------------------------

def test_initialize_for_enable_joins_the_callers_transaction(tmp_path):
    path = tmp_path / "i.db"
    a = connect(path)
    init_db(a)
    b = connect(path)
    try:
        write_if_unchanged(a, "src", None, Health(consecutive_failures=7, backoff_until=NOW + H,
                                                  backoff_cause="failures", last_outcome="failed"), NOW)
        initialize_for_enable(a, "src", NOW, nonce="fixed")
        assert read(b, "src").record.consecutive_failures == 7      # not committed yet
        a.rollback()
        assert read(b, "src").record.consecutive_failures == 7
        initialize_for_enable(a, "src", NOW + H, nonce="fixed")
        a.commit()
        rec = read(b, "src").record
        assert rec == Health(reset_at=NOW + H, reset_nonce="fixed")
    finally:
        a.close()
        b.close()


def test_initialize_for_enable_creates_row_and_nonce_changes_each_time(conn):
    initialize_for_enable(conn, "new", NOW)
    initialize_for_enable(conn, "new2", NOW)
    one, two = read(conn, "new").record, read(conn, "new2").record
    assert one.reset_nonce and two.reset_nonce and one.reset_nonce != two.reset_nonce
    raw1 = read(conn, "new").raw
    initialize_for_enable(conn, "new", NOW)
    assert read(conn, "new").raw != raw1              # same instant, new nonce


def test_enable_does_not_touch_fetch_attempts(conn):
    key = fetch_attempts.news_source_key("src")
    fetch_attempts.record_attempt(conn, key, NOW)
    before = conn.execute("SELECT value FROM alert_state WHERE key=?", (key,)).fetchone()[0]
    initialize_for_enable(conn, "src", NOW)
    assert conn.execute("SELECT value FROM alert_state WHERE key=?", (key,)).fetchone()[0] == before


def test_delete_commit_flag(tmp_path):
    path = tmp_path / "d.db"
    a = connect(path)
    init_db(a)
    b = connect(path)
    try:
        write_if_unchanged(a, "src", None, Health(), NOW)
        delete(a, "src", commit=False)
        assert read(b, "src").raw is not None
        a.commit()
        assert read(b, "src").raw is None
        write_if_unchanged(a, "src", None, Health(), NOW)
        delete(a, "src")
        assert read(b, "src").raw is None
        delete(a, "never-existed")
    finally:
        a.close()
        b.close()


def test_alert_state_known_key_allow_list_is_not_widened():
    from agentic_fx.store import alert_state
    assert not any(k.startswith("news_source_health") for k in alert_state._KNOWN_KEYS)
