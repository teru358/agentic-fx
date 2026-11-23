"""EventStore の commit しない INSERT。既存の emit は従来どおり自分で commit する。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.ops.events import EventSchemaError, EventStore

UTC = timezone.utc
FIELDS = {"guard_kind": "data_feed", "state": "stopped", "generation": 3}


@pytest.fixture
def store(ops_env):
    return EventStore(ops_env.conn, wall_clock=ops_env.wall), ops_env


def _count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM ops_events").fetchone()["n"]


def test_insert_leaves_the_transaction_open_and_a_rollback_removes_it(store):
    events, env = store
    event_id = events.insert("guard_state_changed", "instance", FIELDS)
    assert isinstance(event_id, int) and env.conn.in_transaction
    env.conn.rollback()
    assert _count(env.conn) == 0


def test_insert_followed_by_the_callers_commit_persists_the_event(store):
    events, env = store
    event_id = events.insert("guard_state_changed", "instance", FIELDS)
    env.conn.commit()
    row = env.conn.execute("SELECT * FROM ops_events WHERE id=?", (event_id,)).fetchone()
    assert json.loads(row["fields_json"]) == FIELDS


def test_emit_still_commits_by_itself(store):
    events, env = store
    events.emit("guard_state_changed", "instance", FIELDS)
    assert not env.conn.in_transaction
    env.conn.rollback()
    assert _count(env.conn) == 1


def test_insert_checks_the_schema_before_writing_anything(store):
    events, env = store
    with pytest.raises(EventSchemaError):
        events.insert("guard_state_changed", "instance", {**FIELDS, "state": "free text"})
    assert not env.conn.in_transaction and _count(env.conn) == 0


def test_insert_uses_the_not_before_floor_for_the_time(store):
    events, env = store
    floor = env.wall() + timedelta(hours=1)
    event_id = events.insert("guard_state_changed", "instance", FIELDS, not_before=floor)
    env.conn.commit()
    ts = env.conn.execute("SELECT ts FROM ops_events WHERE id=?", (event_id,)).fetchone()["ts"]
    assert datetime.fromisoformat(ts) == floor


def test_insert_refuses_a_floor_that_is_not_utc(store):
    events, env = store
    naive = datetime(2026, 10, 5, 1, 0)
    with pytest.raises(ValueError):
        events.insert("guard_state_changed", "instance", FIELDS, not_before=naive)
    assert _count(env.conn) == 0
