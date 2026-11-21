from datetime import datetime, timezone

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops.mission_finalize import finalize_mission
from agentic_fx.runners import turn_budget as tbm
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import missions
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def _setup(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    mid = missions.start(conn, "trade", "local", "m", NOW, trigger="signal")
    return conn, mid, ActivityLog(tmp_path / "activity.log"), FixedClock(NOW)


def _lines(tmp_path):
    path = tmp_path / "activity.log"
    return path.read_text("utf-8").splitlines() if path.exists() else []


def test_summary_is_written_once_with_the_mission_reference(tmp_path):
    conn, mid, activity, clock = _setup(tmp_path)
    result = MissionResult("completed", {"decision": "accept"}, [])
    result.turn_summary = tbm.TurnGuard("signal", 6).summary("completed", None)
    finalize_mission(conn, activity, clock, mid, result)
    rows = [l.split("\t") for l in _lines(tmp_path) if "\tturn_summary\t" in l]
    assert len(rows) == 1
    assert rows[0][1] == "AGGREGATE" and rows[0][4] == str(mid)
    assert "terminal=completed" in rows[0][3]
    assert "decision" not in rows[0][3]


def test_a_missing_summary_is_recorded_as_missing(tmp_path):
    conn, mid, activity, clock = _setup(tmp_path)
    result = MissionResult("failed", None, [])
    result.turn_summary = dict(tbm.MISSING_SUMMARY)
    finalize_mission(conn, activity, clock, mid, result)
    rows = [l.split("\t") for l in _lines(tmp_path) if "\tturn_summary\t" in l]
    assert len(rows) == 1 and rows[0][3] == "missing=1"


def test_no_summary_writes_nothing(tmp_path):
    conn, mid, activity, clock = _setup(tmp_path)
    finalize_mission(conn, activity, clock, mid, MissionResult("completed", {}, []))
    assert not any("turn_summary" in l for l in _lines(tmp_path))


def test_a_conflicting_second_finalize_writes_no_second_summary(tmp_path):
    conn, mid, activity, clock = _setup(tmp_path)
    result = MissionResult("completed", {}, [])
    result.turn_summary = tbm.TurnGuard("signal", 6).summary("completed", None)
    finalize_mission(conn, activity, clock, mid, result)
    finalize_mission(conn, activity, clock, mid, result)
    assert sum("\tturn_summary\t" in l for l in _lines(tmp_path)) == 1
