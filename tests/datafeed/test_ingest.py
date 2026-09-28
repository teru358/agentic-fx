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
    count, _ = ingest.prepare(NOW, conn)
    assert count == 2
    assert {call[1] for call in calls} == {"1m", "1h"}
    assert load_cache_bars(conn, "USDJPY", "1m", source="yfinance") == []
    assert ingest.commit(conn) == 2
    assert len(load_cache_bars(conn, "USDJPY", "1m", source="yfinance")) == 1
    count, _ = ingest.prepare(NOW, conn)
    assert count == 0
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
    count, _ = ingest.prepare(NOW, conn)
    assert count == 2
    count, _ = ingest.prepare(NOW + timedelta(seconds=1), conn)
    assert count == 1
    count, _ = ingest.prepare(NOW + timedelta(seconds=2), conn)
    assert count == 1
    count, _ = ingest.prepare(NOW + timedelta(seconds=4), conn)
    assert count == 1
    assert all(timeout > 0 for _, timeout in calls)
    conn.close()


def test_closed_market_and_request_cap_make_no_excess_requests(tmp_path):
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    calls = []
    ingest = Ingest(_settings(), fetch=lambda *args, **kwargs: calls.append(args) or [])
    saturday = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    count, _ = ingest.prepare(saturday, conn)
    assert count == 0
    assert calls == []
    count, _ = ingest.prepare(NOW, conn)
    assert count == 2
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


def test_decision_interval_is_a_health_requirement_and_ingest_priority(tmp_path):
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    settings = _settings()
    settings.datafeed.intervals = ["1m", "15m", "1h"]
    settings.datafeed.primary_intervals = ["15m"]
    ingest = Ingest(settings, fetch=lambda *args, **kwargs: [])

    assert "15m" in ingest.registry.required_intervals
    assert ingest._priority(("USDJPY", "15m")) == 1
    assert ingest._priority(("USDJPY", "1h")) == 2
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


def test_next_probe_is_the_next_bar_close_not_fetch_time_plus_interval(tmp_path):
    """10:22 に 09:00 の 1h 足を取ったら、次に取りに行くのは 10:00 の足が確定する
    11:00:30 であって 11:22 ではない (取得時刻 + 足幅にすると判断足が最大 1 足幅遅れる。
    2026-09-22 の実機で 22 分遅れを観測)。期待時刻を過ぎても足が無ければ backoff で再試行。"""
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    now = datetime(2026, 9, 16, 10, 22, tzinfo=timezone.utc)

    def fetch(pair, interval, start, end, *, timeout):
        if interval == "1h":
            return [Bar(pair, "1h", datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc), 1, 1, 1, 1, 1)]
        return [Bar(pair, "1m", now - timedelta(minutes=2), 1, 1, 1, 1, 1)]

    ingest = Ingest(_settings(), fetch=fetch)
    ingest.prepare(now, conn)
    ingest.commit(conn)
    grace = timedelta(seconds=_settings().datafeed.closed_bar_grace_sec)
    assert ingest.next_probe_at[("USDJPY", "1h")] == datetime(2026, 9, 16, 11, 0, tzinfo=timezone.utc) + grace
    # 11:01 の tick で取りに行き、まだ 10:00 の足が無ければ backoff (足幅が上限) で再試行
    calls = []

    def fetch_empty(pair, interval, start, end, *, timeout):
        calls.append(interval)
        return []

    ingest.fetch = fetch_empty
    later = datetime(2026, 9, 16, 11, 1, tzinfo=timezone.utc)
    ingest.prepare(later, conn)
    assert "1h" in calls
    assert later < ingest.next_probe_at[("USDJPY", "1h")] <= later + timedelta(hours=1)
    conn.close()


def test_next_probe_skips_weekend_to_next_bar_confirmation(tmp_path):
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    settings = _settings()
    settings.datafeed.intervals = ["1m", "15m"]
    settings.datafeed.primary_intervals = ["15m"]
    settings.datafeed.closed_bar_grace_sec = 30
    friday = datetime(2026, 9, 25, 20, 45, tzinfo=timezone.utc)
    calls = []

    def fetch(pair, interval, start, end, *, timeout):
        calls.append((pair, interval))
        return [Bar(pair, interval, friday if interval == "15m" else friday + timedelta(minutes=14), 1, 1, 1, 1, 1)]

    ingest = Ingest(settings, fetch=fetch)
    ingest.prepare(datetime(2026, 9, 27, 21, 0, 4, tzinfo=timezone.utc), conn)
    assert ingest.next_probe_at[("USDJPY", "15m")] == datetime(2026, 9, 27, 21, 15, 30, tzinfo=timezone.utc)
    assert ingest._backoff[("USDJPY", "15m")] == 0
    assert calls.count(("USDJPY", "15m")) == 1
    # 期限ちょうどに新しい足がまだ無ければ予約でなく短い再試行 (backoff 1 回目 = 2 秒)
    at_deadline = datetime(2026, 9, 27, 21, 15, 30, tzinfo=timezone.utc)
    ingest.prepare(at_deadline, conn)
    assert ingest._backoff[("USDJPY", "15m")] == 1
    assert ingest.next_probe_at[("USDJPY", "15m")] == at_deadline + timedelta(seconds=2)
    # 再試行の間隔は足幅 (15 分 = 900 秒) を上限に指数で伸びる
    now = at_deadline
    for attempt in range(2, 12):
        now = ingest.next_probe_at[("USDJPY", "15m")]
        ingest.prepare(now, conn)
        assert ingest._backoff[("USDJPY", "15m")] == attempt
        assert ingest.next_probe_at[("USDJPY", "15m")] - now == timedelta(seconds=min(900, 2 ** attempt))
    conn.close()


def test_report_marks_budget_deferred_decision_key_as_not_attempted(tmp_path):
    """budget 切れで持ち越した判断足 key は report.deferred に載り、
    report.attempted には含まれない (まだ試みていないため)。"""
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    settings = _settings()
    settings.pairs = ["USDJPY", "EURUSD"]
    settings.datafeed.primary_intervals = ["1h"]
    settings.datafeed.ingest_budget_sec = 1
    elapsed = [0.0]

    def fetch(pair, interval, start, end, *, timeout):
        elapsed[0] += 2.0  # 1 回で budget を使い切る
        return []

    ingest = Ingest(settings, fetch=fetch, monotonic=lambda: elapsed[0])
    count, report = ingest.prepare(NOW, conn)
    assert ("EURUSD", "1h") in report.deferred
    assert ("EURUSD", "1h") not in report.attempted
    assert ("EURUSD", "1h") not in report.succeeded
    assert ("EURUSD", "1h") not in report.failed
    conn.close()


def test_report_excludes_backoff_waiting_key_from_every_set(tmp_path):
    """backoff 待ち (next_probe_at がまだ先) の key はどの集合にも現れない
    (「not-attempted」の契約)。"""
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    settings = _settings()

    def fetch(pair, interval, start, end, *, timeout):
        return [Bar(pair, interval, NOW.replace(minute=0, hour=11) if interval == "1h"
                    else NOW.replace(minute=59, hour=11), 1, 1, 1, 1, 1)]

    ingest = Ingest(settings, fetch=fetch)
    ingest.prepare(NOW, conn)
    ingest.commit(conn)
    # 直後 (next_probe_at がまだ先) にもう一度 prepare すると、両方の key が
    # backoff 待ちで probe されない
    count, report = ingest.prepare(NOW + timedelta(seconds=1), conn)
    assert count == 0
    for key in (("USDJPY", "1m"), ("USDJPY", "1h")):
        assert key not in report.attempted
        assert key not in report.succeeded
        assert key not in report.failed
        assert key not in report.deferred
        assert key not in report.empty
    conn.close()
