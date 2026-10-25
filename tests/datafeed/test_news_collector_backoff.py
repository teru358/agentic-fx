"""Collector backoff: counting rules, stages, persistence, unreadable state."""
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from agentic_fx.datafeed.fetchers import FeedFetchError
from agentic_fx.store import fetch_attempts, news_health, news_sources
from tests.datafeed.news_support import (
    HALF_HOUR, T0, MutableClock, Scripted, add_source, article, activity_lines,
    activity_details, hours, make_env, raised_status, run_grid,
)

H = timedelta(hours=1)
# attempt times of an always-failing source on a 30-minute grid
FAIL_FOREVER = hours(0, 0.5, 1, 2, 4, 8, 16, 32, 56)


def _health(conn, name="src"):
    return news_health.read(conn, name)


def _put_health(conn, record, name="src"):
    raw = _health(conn, name).raw
    assert news_health.write_if_unchanged(conn, name, raw, record, T0)


# ---- stage table observed through the written deadline --------------------

@pytest.mark.parametrize("failures,expected", [
    (1, None), (2, None), (3, H), (4, 2 * H), (5, 4 * H), (6, 8 * H),
    (7, 16 * H), (8, 24 * H), (9, 24 * H),
])
def test_deadline_after_nth_consecutive_failure(tmp_path, monkeypatch, failures, expected):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    if failures > 1:
        _put_health(conn, news_health.Health(consecutive_failures=failures - 1))
    fetch = Scripted([raised_status(404)], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        col.collect()
    record = _health(conn).record
    assert record.consecutive_failures == failures
    assert record.backoff_until == (None if expected is None else T0 + expected)


def test_huge_stored_counter_still_waits_a_day(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    _put_health(conn, news_health.Health(consecutive_failures=10**9 - 1))
    fetch = Scripted([raised_status(500)], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        col.collect()
    assert _health(conn).record.backoff_until == T0 + 24 * H


def test_always_failing_source_follows_the_attempt_table(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    fetch = Scripted([raised_status(404)], clock)
    assert run_grid(monkeypatch, col, clock, fetch, hours=60) == FAIL_FOREVER


# ---- waiting behaviour -----------------------------------------------------

def test_waiting_cycles_do_not_fetch_and_report_skip_once(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    _put_health(conn, news_health.Health(consecutive_failures=2))
    fetch = Scripted([raised_status(500)], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        col.collect()                      # third failure: waits one hour
        until = _health(conn).record.backoff_until
        assert until == T0 + H
        clock.advance(HALF_HOUR)
        col.collect()
        col.collect()
    assert len(fetch.calls) == 1
    assert activity_details(tmp_path, "news_source_skipped") == ["src: 連続 3 回失敗のため 13:00 UTC まで待つ"]


def test_first_cycle_one_second_after_the_deadline_fetches(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    _put_health(conn, news_health.Health(consecutive_failures=2))
    fetch = Scripted([raised_status(500), raised_status(500)], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        col.collect()
        clock.t = T0 + H - timedelta(seconds=1)
        col.collect()
        assert len(fetch.calls) == 1
        clock.t = T0 + H + timedelta(seconds=1)
        col.collect()
    assert len(fetch.calls) == 2
    assert _health(conn).record.consecutive_failures == 4


def test_success_clears_state_and_next_failure_starts_over(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    _put_health(conn, news_health.Health(consecutive_failures=5, retry_after_streak=2,
                                         backoff_until=T0 - H, backoff_cause="failures"))
    fetch = Scripted([[article()], raised_status(404)], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        col.collect()
        ok = _health(conn).record
        assert (ok.consecutive_failures, ok.retry_after_streak, ok.backoff_until) == (0, 0, None)
        assert (ok.last_outcome, ok.last_count) == ("ok", 1)
        clock.advance(HALF_HOUR)
        col.collect()
    assert _health(conn).record.consecutive_failures == 1
    assert _health(conn).record.backoff_until is None


def test_skip_message_names_each_cause(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn, "a", url="https://example.invalid/a")
    add_source(conn, "b", url="https://example.invalid/b")
    add_source(conn, "c", url="https://example.invalid/c")
    until = T0 + 2 * H
    _put_health(conn, news_health.Health(consecutive_failures=3, backoff_until=until,
                                         backoff_cause="failures"), "a")
    _put_health(conn, news_health.Health(retry_after_streak=4, backoff_until=until,
                                         backoff_cause="refusals"), "b")
    fetch_attempts.record_attempt(conn, fetch_attempts.news_source_key("c"), T0)
    fetch_attempts.record_retry_after(conn, fetch_attempts.news_source_key("c"), T0, 2 * H)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", Scripted([[]], clock))
        col.collect()
    assert sorted(activity_details(tmp_path, "news_source_skipped")) == [
        "a: 連続 3 回失敗のため 14:00 UTC まで待つ",
        "b: 先方の拒否が 4 回続いたため 14:00 UTC まで待つ",
        "c: 先方の指示により 14:00 UTC まで待つ"]


def test_new_collector_instance_keeps_waiting(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    _put_health(conn, news_health.Health(consecutive_failures=3, backoff_until=T0 + H,
                                         backoff_cause="failures"))
    from agentic_fx.activity import ActivityLog
    from agentic_fx.datafeed.news_collector import NewsCollector
    restarted = NewsCollector(conn, rag, ActivityLog(tmp_path / "b.log"), clock, timeout_sec=10)
    fetch = Scripted([[article()]], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        restarted.collect()
    assert fetch.calls == []


# ---- counting rules --------------------------------------------------------

DATE_PAST = "Wed, 21 Oct 2026 07:28:00 GMT"


def _future_date(now):
    return (now + HALF_HOUR).strftime("%a, %d %b %Y %H:%M:%S GMT")


COUNTING = [
    # label, outcome, failures, refusals, last_outcome, reason, cause
    ("500", raised_status(500), 1, 0, "failed", "HTTP 500", "failures"),
    ("500+RA", raised_status(500, {"Retry-After": "120"}), 1, 0, "failed", "HTTP 500", "failures"),
    ("502+RA", raised_status(502, {"Retry-After": "120"}), 1, 0, "failed", "HTTP 502", "failures"),
    ("429", raised_status(429), 1, 0, "failed", "HTTP 429", "failures"),
    ("429 soon", raised_status(429, {"Retry-After": "soon"}), 1, 0, "failed", "HTTP 429", "failures"),
    ("429 zero", raised_status(429, {"Retry-After": "0"}), 1, 0, "failed", "HTTP 429", "failures"),
    ("429 negative", raised_status(429, {"Retry-After": "-5"}), 1, 0, "failed", "HTTP 429", "failures"),
    ("429 past date", raised_status(429, {"Retry-After": DATE_PAST}), 1, 0, "failed", "HTTP 429", "failures"),
    ("503", raised_status(503), 1, 0, "failed", "HTTP 503", "failures"),
    ("429+RA", raised_status(429, {"Retry-After": "120"}), 0, 1, "rate_limited", "HTTP 429", "refusals"),
    ("503+RA", raised_status(503, {"Retry-After": "1"}), 0, 1, "rate_limited", "HTTP 503", "refusals"),
    ("503+date", lambda now: raised_status(503, {"Retry-After": _future_date(now)}), 0, 1,
     "rate_limited", "HTTP 503", "refusals"),
    ("404", raised_status(404), 1, 0, "failed", "HTTP 404", "failures"),
    ("timeout", httpx.ReadTimeout("slow"), 1, 0, "failed", "タイムアウト", "failures"),
    ("connect", httpx.ConnectError("refused"), 1, 0, "failed", "接続できません", "failures"),
    ("name resolution", httpx.ConnectError("[Errno -2] Name or service not known"), 1, 0,
     "failed", "接続できません", "failures"),
    ("feed error", FeedFetchError("x: feed fetch/parse failed (SAXParseException)"), 1, 0,
     "failed", "取得できません", "failures"),
]


@pytest.mark.parametrize("label,outcome,f,s,last,reason,cause",
                         COUNTING, ids=[row[0] for row in COUNTING])
def test_single_outcome_counting(tmp_path, monkeypatch, label, outcome, f, s, last, reason, cause):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    fetch = Scripted([outcome], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        col.collect()
    record = _health(conn).record
    assert (record.consecutive_failures, record.retry_after_streak) == (f, s)
    assert (record.last_outcome, record.last_reason) == (last, reason)
    assert len(fetch.calls) == 1
    assert activity_details(tmp_path, "news_source_failed") == [f"src: {reason}"]


def test_empty_success_counts_as_success(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    _put_health(conn, news_health.Health(consecutive_failures=2))
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", Scripted([[]], clock))
        col.collect()
    record = _health(conn).record
    assert (record.consecutive_failures, record.last_outcome, record.last_count) == (0, "ok", 0)
    assert activity_details(tmp_path, "news_source_failed") == []


def test_unknown_fetcher_leaves_no_health_row_and_uses_fixed_word(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn, "weird", fetcher="bogus")
    fetch = Scripted([[]], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_web", fetch)
        col.collect()
    assert fetch.calls == []
    assert _health(conn, "weird").raw is None
    assert activity_details(tmp_path, "news_source_failed") == ["weird: 未知の取得方式"]


def test_refusal_streak_resets_on_other_failure_or_success(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    ra = raised_status(429, {"Retry-After": "1"})
    fetch = Scripted([ra, ra, raised_status(404), ra, ra, [article()]], clock)
    seen = []
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        for _ in range(6):
            # a long step so no wait is ever in force between observations
            clock.advance(timedelta(hours=30))
            col.collect()
            r = _health(conn).record
            seen.append((r.consecutive_failures, r.retry_after_streak))
    assert seen == [(0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (0, 0)]


def test_cause_follows_the_larger_counter(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    _put_health(conn, news_health.Health(consecutive_failures=4, retry_after_streak=3))
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed",
                      Scripted([raised_status(429, {"Retry-After": "5"})], clock))
        col.collect()
    r = _health(conn).record
    assert (r.consecutive_failures, r.retry_after_streak, r.backoff_cause) == (4, 4, "failures")
    assert r.backoff_until == T0 + 2 * H
    clock.t = T0 + 5 * H
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed",
                      Scripted([raised_status(429, {"Retry-After": "5"})], clock))
        col.collect()
    r = _health(conn).record
    assert (r.retry_after_streak, r.backoff_cause) == (5, "refusals")
    assert r.backoff_until == T0 + 5 * H + 4 * H


# ---- Retry-After sources converge to the same table ------------------------

@pytest.mark.parametrize("seconds,expected", [
    ("1", hours(0, 0.5, 1, 2, 4, 8, 16, 32, 56)),
    ("1799", hours(0, 0.5, 1, 2, 4, 8, 16, 32, 56)),
    ("1800", hours(0, 0.5, 1, 2, 4, 8, 16, 32, 56)),
    # 30 minutes and 1 second: the half-hour cycle after each early attempt is still held
    ("1801", hours(0, 1, 2, 3, 5, 9, 17, 33, 57)),
])
def test_short_retry_after_every_cycle_converges(tmp_path, monkeypatch, seconds, expected):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    fetch = Scripted([raised_status(429, {"Retry-After": seconds})], clock)
    assert run_grid(monkeypatch, col, clock, fetch, hours=60) == expected


def test_http_date_retry_after_every_cycle_converges(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)

    def ra_date(now):
        return raised_status(429, {"Retry-After": _future_date(now)})

    fetch = Scripted([ra_date], clock)
    assert run_grid(monkeypatch, col, clock, fetch, hours=60) == hours(0, 0.5, 1, 2, 4, 8, 16, 32, 56)


@pytest.mark.parametrize("pattern", [
    ["500", "ra", "ra"],
    ["ra", "ra", "404"],
])
def test_mixed_series_after_the_cap_is_a_day_apart(tmp_path, monkeypatch, pattern):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    _put_health(conn, news_health.Health(consecutive_failures=8))
    pick = {"500": raised_status(500), "404": raised_status(404),
            "ra": raised_status(429, {"Retry-After": "120"})}
    fetch = Scripted([pick[p] for p in pattern * 12], clock)
    times = run_grid(monkeypatch, col, clock, fetch, hours=24 * 9)
    assert len(times) >= 8
    for earlier, later in zip(times, times[1:]):
        assert timedelta(hours=24) <= later - earlier <= timedelta(hours=24, minutes=30)


# ---- unreadable or odd stored state ---------------------------------------

def _raw_row(conn, name, value):
    conn.execute("INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (news_health.PREFIX + name, value, T0.isoformat()))
    conn.commit()


def _good(**over):
    base = {"v": 1, "consecutive_failures": 3, "retry_after_streak": 0,
            "backoff_until": (T0 + H).isoformat(), "backoff_cause": "failures",
            "reset_at": None, "reset_nonce": None, "last_outcome": "failed",
            "last_at": T0.isoformat(), "last_count": None, "last_reason": "HTTP 500"}
    base.update(over)
    return json.dumps(base)


CORRUPT = [
    "not json",
    _good(consecutive_failures=True),
    _good(consecutive_failures=-1),
    _good(consecutive_failures="3"),
    _good(retry_after_streak=10**12),
    _good(backoff_until=(T0 + H).replace(tzinfo=None).isoformat()),
    _good(backoff_until=12345),
    _good(backoff_cause="other"),
    _good(v=2),
    "[]",
]


@pytest.mark.parametrize("raw", CORRUPT)
def test_corrupt_health_means_no_wait_and_is_rewritten(tmp_path, monkeypatch, raw):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    _raw_row(conn, "src", raw)
    fetch = Scripted([raised_status(500)], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        col.collect()
    assert len(fetch.calls) == 1
    read = _health(conn)
    assert not read.corrupt
    assert read.record.consecutive_failures == 1


def test_rewound_clock_deadline_is_capped_and_saved_once(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    _put_health(conn, news_health.Health(consecutive_failures=5, backoff_until=T0 + timedelta(days=10),
                                         backoff_cause="failures"))
    fetch = Scripted([[article()]], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        col.collect()
        assert _health(conn).record.backoff_until == T0 + 24 * H
        clock.advance(H)
        col.collect()
        assert _health(conn).record.backoff_until == T0 + 24 * H
    assert fetch.calls == []


def test_backoff_cap_is_not_written_by_read_paths(tmp_path):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    _put_health(conn, news_health.Health(consecutive_failures=5, backoff_until=T0 + timedelta(days=10),
                                         backoff_cause="failures"))
    before = _health(conn).raw
    assert news_health.read_effective_wait(conn, "src", T0) == (T0 + 24 * H, "failures")
    assert _health(conn).raw == before


def test_rewound_clock_retry_after_wait_is_capped_at_a_day_from_now(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    key = fetch_attempts.news_source_key("src")
    future = T0 + timedelta(days=10)
    fetch_attempts.record_attempt(conn, key, future)
    fetch_attempts.record_retry_after(conn, key, future, 24 * H)
    assert news_health.read_effective_wait(conn, "src", T0) == (T0 + 24 * H, "directive")
    fetch = Scripted([[article()]], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        col.collect()
    assert fetch.calls == []
    assert activity_details(tmp_path, "news_source_skipped") == ["src: 先方の指示により 12:00 UTC まで待つ"]


def test_corrupt_fetch_attempt_value_means_no_wait(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    conn.execute("INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?)",
                 (fetch_attempts.news_source_key("src"), "garbage", T0.isoformat()))
    conn.commit()
    fetch = Scripted([[article()]], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        col.collect()
    assert len(fetch.calls) == 1


@pytest.mark.parametrize("target", ["health", "attempt"])
def test_unreadable_state_skips_fetch_without_noise(tmp_path, monkeypatch, caplog, target):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    add_source(conn, "other", url="https://example.invalid/other")
    fetch = Scripted([[article()]], clock)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        if target == "health":
            real = news_health.read
            patch.setattr(news_health, "read",
                          lambda c, n: boom() if n == "src" else real(c, n))
        else:
            real = fetch_attempts.load
            patch.setattr(fetch_attempts, "load",
                          lambda c, k: boom() if k.endswith(".src") else real(c, k))
        with caplog.at_level(logging.WARNING, logger="agentic_fx.news"):
            for _ in range(4):
                col.collect()
                clock.advance(HALF_HOUR)
    # only the healthy source is fetched; the unreadable one is skipped every cycle
    assert len(fetch.calls) == 4
    assert activity_details(tmp_path, "news_source_skipped") == []
    warned = [r for r in caplog.records if "unreadable" in r.getMessage()]
    assert len(warned) == 1
    assert "OperationalError" in warned[0].getMessage()
    assert "disk I/O" not in warned[0].getMessage()


def test_recovered_state_warns_again_on_the_next_outage(tmp_path, monkeypatch, caplog):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    fetch = Scripted([[article()]], clock)
    real = news_health.read
    broken = {"on": True}

    def flaky(c, n):
        if broken["on"]:
            raise sqlite3.OperationalError("locked")
        return real(c, n)

    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        patch.setattr(news_health, "read", flaky)
        with caplog.at_level(logging.WARNING, logger="agentic_fx.news"):
            col.collect()
            col.collect()
            broken["on"] = False
            col.collect()
            broken["on"] = True
            col.collect()
    assert len(fetch.calls) == 1
    assert len([r for r in caplog.records if "unreadable" in r.getMessage()]) == 2


# ---- write failures: in-memory record ---------------------------------------

def _failing_writes(monkeypatch_ctx):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch_ctx.setattr(news_health, "write_if_unchanged", fail)


def test_write_failure_does_not_stop_rag_or_failure_record(tmp_path, monkeypatch, caplog):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn, "good", url="https://example.invalid/good")
    add_source(conn, "bad", url="https://example.invalid/bad")

    def fetch(url, name, *, timeout_sec):
        if name == "bad":
            raise raised_status(404)
        return [article()]

    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        _failing_writes(patch)
        with caplog.at_level(logging.WARNING, logger="agentic_fx.news"):
            assert col.collect() == 1
    assert len(rag.added) == 1
    assert activity_details(tmp_path, "news_source_failed") == ["bad: HTTP 404"]
    assert all("database is locked" not in r.getMessage() for r in caplog.records)
    assert any("health write failed (OperationalError)" in r.getMessage() for r in caplog.records)


def test_persistent_write_failure_still_converges(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    fetch = Scripted([raised_status(500)], clock)
    with monkeypatch.context() as patch:
        _failing_writes(patch)
        times = run_grid(patch, col, clock, fetch, hours=60)
    assert times == FAIL_FOREVER


def test_persistent_write_failure_with_future_last_at_and_rewound_clock(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    _put_health(conn, news_health.Health(
        consecutive_failures=2, last_outcome="failed", last_at=T0 + timedelta(days=30),
        last_reason="HTTP 500"))
    fetch = Scripted([raised_status(500)], clock)
    with monkeypatch.context() as patch:
        _failing_writes(patch)
        times = run_grid(patch, col, clock, fetch, hours=60)
    assert times == hours(0, 1, 3, 7, 15, 31, 55)


def test_memory_record_is_dropped_when_the_db_row_changes(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    fetch = Scripted([raised_status(500)], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        real = news_health.write_if_unchanged
        _failing_writes(patch)
        for _ in range(3):
            col.collect()
            clock.advance(HALF_HOUR)
        assert len(fetch.calls) == 3          # third failure now waits in memory only
        clock.advance(timedelta(seconds=1))
        col.collect()
        assert len(fetch.calls) == 3
        # an enable lands: the row appears, so the pair's original text no longer matches
        news_health.initialize_for_enable(conn, "src", clock.now(), nonce="n1")
        conn.commit()
        col.collect()
        assert len(fetch.calls) == 4
        assert "src" in col._health_overlay            # new failure kept in memory again
        patch.setattr(news_health, "write_if_unchanged", real)
        clock.advance(timedelta(days=2))
        col.collect()
    assert "src" not in col._health_overlay
    assert _health(conn).record.consecutive_failures >= 1


def test_memory_record_is_dropped_by_the_next_successful_write(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    real = news_health.write_if_unchanged
    fetch = Scripted([raised_status(500)], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        _failing_writes(patch)
        col.collect()
        assert "src" in col._health_overlay
        patch.setattr(news_health, "write_if_unchanged", real)
        clock.advance(HALF_HOUR)
        col.collect()
    assert "src" not in col._health_overlay
    assert _health(conn).record.consecutive_failures == 2


def test_memory_record_is_dropped_when_a_conditional_write_loses(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn)
    real = news_health.write_if_unchanged
    fetch = Scripted([raised_status(500)], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        _failing_writes(patch)
        col.collect()
        assert "src" in col._health_overlay

        def lose(c, name, expected_raw, record, now, **kw):
            c.execute("INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (news_health.PREFIX + name, _good(), now.isoformat()))
            c.commit()
            return real(c, name, expected_raw, record, now, **kw)

        patch.setattr(news_health, "write_if_unchanged", lose)
        clock.advance(HALF_HOUR)
        col.collect()
    assert "src" not in col._health_overlay


def test_control_character_name_is_skipped_not_fatal(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    add_source(conn, "bad\x07name", url="https://example.invalid/x")
    add_source(conn, "fine", url="https://example.invalid/y")
    fetch = Scripted([[article()]], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        col.collect()
    assert len(fetch.calls) == 1


def test_uppercase_and_long_legacy_names_are_tracked(tmp_path, monkeypatch):
    conn, rag, clock, col = make_env(tmp_path)
    name = "Legacy-" + "X" * 60
    add_source(conn, name)
    fetch = Scripted([raised_status(500)], clock)
    with monkeypatch.context() as patch:
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        col.collect()
    assert _health(conn, name).record.consecutive_failures == 1


# ---- storage failure is not a source failure -------------------------------

class _BrokenRag:
    def add_news(self, articles, now):
        raise RuntimeError(f"chroma down {CANARY_STORE}")

    def cleanup_news(self, now, hours=48):
        return 0


CANARY_STORE = "https://example.invalid/store-canary?k=SECRETSTORE"


def test_rag_write_failure_is_not_counted_against_the_source(tmp_path, monkeypatch, caplog):
    conn, rag, clock, col = make_env(tmp_path, rag=_BrokenRag())
    add_source(conn)
    before = _health(conn)
    fetch = Scripted([[article(1)]], clock)
    with monkeypatch.context() as patch, caplog.at_level(logging.DEBUG):
        patch.setattr("agentic_fx.datafeed.news_collector.fetch_feed", fetch)
        col.collect()
    lines = activity_lines(tmp_path)
    assert not any("news_source_failed" in line for line in lines)
    assert sum("news_store_failed" in line for line in lines) == 1
    assert _health(conn).raw == before.raw and _health(conn).record is None
    assert not any("SECRETSTORE" in line or "example.invalid" in line for line in lines)
    assert "SECRETSTORE" not in caplog.text and "chroma down" not in caplog.text
