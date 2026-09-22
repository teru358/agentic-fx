from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.ingest import Ingest
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import load_cache_bars


NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)  # Wednesday


def _settings():
    return SimpleNamespace(
        pairs=["USDJPY"],
        datafeed=SimpleNamespace(primary="yfinance", intervals=["1m", "1h", "4h"],
                                 primary_intervals=["1h"], ingest_budget_sec=10,
                                 closed_bar_grace_sec=0),
    )


def test_cold_fill_is_once_per_native_key_and_commit_is_separate(tmp_path):
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    calls = []

    def fetch(pair, interval, start, end, *, timeout):
        calls.append((pair, interval, start, end, timeout))
        return [Bar(pair, interval, NOW.replace(minute=0, hour=11) if interval == "1h"
                    else NOW.replace(minute=59, hour=11), 1, 1, 1, 1, 1)]

    ingest = Ingest(_settings(), fetch=fetch)
    assert ingest.prepare(NOW, conn) == 2
    assert {call[1] for call in calls} == {"1m", "1h"}
    assert load_cache_bars(conn, "USDJPY", "1m", source="yfinance") == []
    assert ingest.commit(conn) == 2
    assert len(load_cache_bars(conn, "USDJPY", "1m", source="yfinance")) == 1
    assert ingest.prepare(NOW, conn) == 0
    conn.close()


def test_failure_retries_once_next_tick_and_empty_response_backs_off(tmp_path):
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    calls = []

    def fetch(pair, interval, start, end, *, timeout):
        calls.append((interval, timeout))
        if len(calls) == 1:
            raise OSError("offline")
        return []

    ingest = Ingest(_settings(), fetch=fetch)
    assert ingest.prepare(NOW, conn) == 2
    assert ingest.prepare(NOW + timedelta(seconds=1), conn) == 1
    assert ingest.prepare(NOW + timedelta(seconds=2), conn) == 1
    assert ingest.prepare(NOW + timedelta(seconds=4), conn) == 1
    assert all(timeout > 0 for _, timeout in calls)
    conn.close()


def test_closed_market_and_request_cap_make_no_excess_requests(tmp_path):
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    calls = []
    ingest = Ingest(_settings(), fetch=lambda *args, **kwargs: calls.append(args) or [])
    saturday = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    assert ingest.prepare(saturday, conn) == 0
    assert calls == []
    assert ingest.prepare(NOW, conn) == 2
    assert ingest.last_request_count <= len(ingest.keys)
    conn.close()


def test_prepare_timeout_is_remaining_budget_and_commit_is_separate(tmp_path):
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    elapsed = iter((0.0, 7.0, 7.0, 10.0))
    seen = []
    settings = _settings()
    settings.datafeed.ingest_budget_sec = 10

    def fetch(pair, interval, start, end, *, timeout):
        seen.append((interval, timeout))
        return []

    ingest = Ingest(settings, fetch=fetch, monotonic=lambda: next(elapsed))
    ingest.prepare(NOW, conn)
    assert seen[0] == ("1m", 3.0)
    assert ingest.budget_exhausted
    assert ingest.commit(conn) == 0
    conn.close()


def test_budget_defers_only_decision_keys_to_the_front_of_next_tick(tmp_path):
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    settings = _settings()
    settings.pairs = ["USDJPY", "EURUSD"]
    settings.datafeed.primary_intervals = ["1h"]
    settings.datafeed.ingest_budget_sec = 1
    calls = []

    def fetch(pair, interval, start, end, *, timeout):
        calls.append((pair, interval))
        return []

    ingest = Ingest(settings, fetch=fetch, monotonic=lambda: 0.0)
    ingest._deferred = [("USDJPY", "1m"), ("USDJPY", "1h"),
                         ("EURUSD", "1h")]
    assert ingest._ordered_keys() == (("USDJPY", "1h"), ("EURUSD", "1h"),
                                      ("EURUSD", "1m"), ("USDJPY", "1m"))
    conn.close()


def test_budget_fetches_deferred_decision_within_two_ticks(tmp_path):
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    settings = _settings()
    settings.pairs = ["EURUSD"]
    settings.datafeed.ingest_budget_sec = 1
    elapsed = [0.0]
    calls = []

    def fetch(pair, interval, start, end, *, timeout):
        calls.append((pair, interval))
        elapsed[0] += 1
        raise OSError("retry next tick")

    ingest = Ingest(settings, fetch=fetch, monotonic=lambda: elapsed[0])
    ingest.prepare(NOW, conn)
    ingest.prepare(NOW + timedelta(seconds=1), conn)
    assert calls == [("EURUSD", "1m"), ("EURUSD", "1h")]
    conn.close()
