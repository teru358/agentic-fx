import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.outage import IngestTickReport, OutageStateMachine
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import upsert_cache_bars

PAIR = "USDJPY"
KEY = (PAIR, "1m")
UTC = timezone.utc
FIXTURES = Path(__file__).parents[1] / "fixtures" / "outage"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["bars"]


def _machine(tmp_path, *, activity=None):
    conn = connect(tmp_path / "outage.db")
    init_db(conn)
    return conn, OutageStateMachine(
        conn, hard_keys=frozenset({KEY}), interval_widths={"1m": timedelta(minutes=1)},
        grace=timedelta(seconds=30), storage_source="mt5-live", activity=activity)


def _report():
    return IngestTickReport(attempted=frozenset({KEY}), succeeded=frozenset({KEY}),
                            failed=frozenset(), deferred=frozenset(), empty=frozenset())


def _replay(conn, machine, bars, start, end):
    parsed = [(datetime.fromisoformat(bar["time"]), bar) for bar in bars]
    states = {}
    now = start
    while now <= end:
        eligible = [bar for time, bar in parsed if time <= now - timedelta(minutes=2)]
        if eligible:
            upsert_cache_bars(conn, [Bar(PAIR, "1m", datetime.fromisoformat(bar["time"]),
                                         bar["open"], bar["high"], bar["low"],
                                         bar["close"], bar["volume"])
                                    for bar in eligible], source="mt5-live")
        states[now] = machine.observe(now, _report())
        now += timedelta(minutes=1)
    return states


def test_rollover_fixture_creates_three_restricted_episodes(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    conn, machine = _machine(tmp_path, activity=log)
    states = _replay(conn, machine, _load("usdjpy-1m-20260929-rollover.json"),
                     datetime(2026, 9, 29, 20, 52, tzinfo=UTC),
                     datetime(2026, 9, 29, 21, 31, tzinfo=UTC))

    assert states[datetime(2026, 9, 29, 21, 17, tzinfo=UTC)] == "restricted"
    assert states[datetime(2026, 9, 29, 21, 20, tzinfo=UTC)] == "ready"
    assert states[datetime(2026, 9, 29, 21, 21, tzinfo=UTC)] == "restricted"
    assert states[datetime(2026, 9, 29, 21, 24, tzinfo=UTC)] == "ready"
    assert states[datetime(2026, 9, 29, 21, 28, tzinfo=UTC)] == "restricted"
    assert states[datetime(2026, 9, 29, 21, 31, tzinfo=UTC)] == "ready"
    events = [line.split("\t")[2] for line in log.tail(50)]
    assert events.count("datafeed_restricted") == 3
    assert events.count("datafeed_recovered_auto") == 3
    assert "datafeed_degraded" not in events


def test_sunday_open_fixture_recovers_before_deadline(tmp_path):
    conn, machine = _machine(tmp_path)
    states = _replay(conn, machine, _load("usdjpy-1m-20260913-sunday-open.json"),
                     datetime(2026, 9, 13, 21, 2, tzinfo=UTC),
                     datetime(2026, 9, 13, 21, 23, tzinfo=UTC))

    assert states[datetime(2026, 9, 13, 21, 3, tzinfo=UTC)] == "restricted"
    assert machine.status()["restricted_deadline_at"] is None
    assert states[datetime(2026, 9, 13, 21, 23, tzinfo=UTC)] == "ready"


def test_contiguous_fixture_has_no_state_transition_or_activity(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    conn, machine = _machine(tmp_path, activity=log)
    states = _replay(conn, machine, _load("usdjpy-1m-20260930-no-gap.json"),
                     datetime(2026, 9, 30, 20, 47, tzinfo=UTC),
                     datetime(2026, 9, 30, 21, 50, tzinfo=UTC))

    assert set(states.values()) == {"ready"}
    assert log.tail(50) == []
