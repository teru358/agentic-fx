from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.ingest import Ingest
from agentic_fx.datafeed.outage import OutageStateMachine
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import latest_closed_cache_bar_time, upsert_cache_bars


PAIR = "USDJPY"
_UTC = timezone.utc


def test_weekend_open_stays_ready_until_first_15m_bar_is_ingested(tmp_path):
    conn = connect(tmp_path / "bars.db")
    init_db(conn)
    friday_15m = datetime(2026, 9, 25, 20, 45, tzinfo=_UTC)
    friday_1m = datetime(2026, 9, 25, 20, 59, tzinfo=_UTC)
    upsert_cache_bars(conn, [
        Bar(PAIR, "15m", friday_15m, 1, 1, 1, 1, 1),
        Bar(PAIR, "1m", friday_1m, 1, 1, 1, 1, 1),
    ], source="yfinance")
    settings = SimpleNamespace(
        pairs=[PAIR],
        datafeed=SimpleNamespace(primary="yfinance", intervals=["1m", "15m"],
                                 primary_intervals=["15m"], ingest_budget_sec=10,
                                 closed_bar_grace_sec=30),
    )
    requests = []

    def fetch(pair, interval, start, end, *, timeout):
        requests.append((interval, end))
        if interval == "15m":
            if end >= datetime(2026, 9, 27, 21, 15, 30, tzinfo=_UTC):
                return [Bar(pair, interval, datetime(2026, 9, 27, 21, 0, tzinfo=_UTC), 1, 1, 1, 1, 1)]
            return [Bar(pair, interval, friday_15m, 1, 1, 1, 1, 1)]
        first_close = datetime(2026, 9, 27, 21, 1, 30, tzinfo=_UTC)
        if end < first_close:
            return [Bar(pair, interval, friday_1m, 1, 1, 1, 1, 1)]
        latest = (end - timedelta(minutes=1, seconds=30)).replace(second=0,
                                                                      microsecond=0)
        return [Bar(pair, interval, latest, 1, 1, 1, 1, 1)]

    ingest = Ingest(settings, fetch=fetch)
    activity = ActivityLog(tmp_path / "activity.log")
    outage = OutageStateMachine(
        conn, hard_keys=frozenset({(PAIR, "1m"), (PAIR, "15m")} ),
        interval_widths={"1m": timedelta(minutes=1), "15m": timedelta(minutes=15)},
        grace=timedelta(seconds=30), storage_source="yfinance", activity=activity)
    first = datetime(2026, 9, 27, 21, 0, 4, tzinfo=_UTC)
    for offset in range(18):
        now = first + timedelta(minutes=offset)
        _, report = ingest.prepare(now, conn)
        ingest.commit(conn)
        assert outage.observe(now, report) == "ready"

    assert [when for interval, when in requests if interval == "15m"] == [
        datetime(2026, 9, 27, 21, 0, 4, tzinfo=_UTC),
        datetime(2026, 9, 27, 21, 16, 4, tzinfo=_UTC),
    ]
    assert latest_closed_cache_bar_time(
        conn, PAIR, "15m", now=datetime(2026, 9, 27, 21, 17, 4, tzinfo=_UTC),
        width=timedelta(minutes=15), grace=timedelta(seconds=30), source="yfinance",
    ) == datetime(2026, 9, 27, 21, 0, tzinfo=_UTC)
    assert latest_closed_cache_bar_time(
        conn, PAIR, "1m", now=datetime(2026, 9, 27, 21, 2, 4, tzinfo=_UTC),
        width=timedelta(minutes=1), grace=timedelta(seconds=30), source="yfinance",
    ) == datetime(2026, 9, 27, 21, 0, tzinfo=_UTC)
    assert not any("datafeed_degraded" in line or "data_outage_degraded" in line
                   for line in activity.tail(50))
    conn.close()
