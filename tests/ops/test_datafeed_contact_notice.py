"""接触・不明の停止通知は、通常の停止通知とは別の event になる。

通常の停止の記録 (epoch, stopped, '') と接触の記録 (epoch, stopped, 'contact') は、同じ epoch に
並んでも一方が他方に吸収されない。実 sqlite と実 EventStore で動かし、期待値は設計の表から手で書く。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agentic_fx.ops import datafeed_notices
from agentic_fx.store import alert_state

UTC = timezone.utc
FLOOR = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)


@pytest.fixture
def rig(ops_env):
    service = ops_env.service(datafeed_notices=True)
    return SimpleNamespace(env=ops_env, service=service, conn=ops_env.conn)


def _notice(conn, epoch, state, *, at, key=""):
    conn.execute(
        "INSERT INTO datafeed_outage_notices(epoch, notice_state, request_key, occurred_at, event_id) "
        "VALUES (?,?,?,?,NULL)", (epoch, state, key, at.isoformat()))
    conn.commit()


def _events(conn):
    out = []
    for row in conn.execute("SELECT fields_json FROM ops_events WHERE code='guard_state_changed' "
                            "ORDER BY id"):
        fields = json.loads(row["fields_json"])
        if fields["guard_kind"] == "data_feed":
            out.append((fields["state"], fields["generation"]))
    return out


def _link(conn, epoch, state, key):
    return conn.execute(
        "SELECT event_id FROM datafeed_outage_notices WHERE epoch=? AND notice_state=? "
        "AND request_key=?", (epoch, state, key)).fetchone()["event_id"]


def _sweep(rig):
    rig.service.emit_pending_notices(improve_enabled=False)


def _enabled(rig):
    _sweep(rig)
    assert alert_state.get(rig.conn, datafeed_notices.PRODUCER_KEY) is not None


def test_a_contact_stop_after_an_ordinary_stop_of_the_same_epoch_is_its_own_event(rig):
    _enabled(rig)
    _notice(rig.conn, 5, "stopped", at=FLOOR + timedelta(hours=1))
    _notice(rig.conn, 5, "stopped", at=FLOOR + timedelta(hours=2), key="contact")
    _sweep(rig)
    assert _events(rig.conn) == [("stopped", 5), ("contact_stopped", 5)]
    assert _link(rig.conn, 5, "stopped", "") != _link(rig.conn, 5, "stopped", "contact")


def test_a_contact_stop_alone_is_one_event_and_a_second_sweep_adds_none(rig):
    _enabled(rig)
    _notice(rig.conn, 7, "stopped", at=FLOOR + timedelta(hours=1), key="contact")
    _sweep(rig)
    _sweep(rig)
    assert _events(rig.conn) == [("contact_stopped", 7)]


def test_an_ordinary_stop_after_a_contact_stop_is_not_swallowed_by_it(rig):
    _enabled(rig)
    _notice(rig.conn, 8, "stopped", at=FLOOR + timedelta(hours=1), key="contact")
    _notice(rig.conn, 8, "stopped", at=FLOOR + timedelta(hours=2))
    _sweep(rig)
    assert sorted(_events(rig.conn)) == [("contact_stopped", 8), ("stopped", 8)]


def test_the_contact_stop_comes_before_the_awaiting_resume_of_the_same_moment(rig):
    _enabled(rig)
    at = FLOOR + timedelta(hours=1)
    _notice(rig.conn, 9, "awaiting_resume", at=at)
    _notice(rig.conn, 9, "stopped", at=at, key="contact")
    _sweep(rig)
    assert _events(rig.conn) == [("contact_stopped", 9), ("awaiting_resume", 9)]


def test_a_contact_event_carries_no_values_beyond_the_fixed_words(rig):
    _enabled(rig)
    _notice(rig.conn, 10, "stopped", at=FLOOR + timedelta(hours=1), key="contact")
    _sweep(rig)
    row = rig.conn.execute("SELECT code, ref, fields_json FROM ops_events ORDER BY id DESC "
                           "LIMIT 1").fetchone()
    assert row["code"] == "guard_state_changed" and row["ref"] == "instance"
    assert json.loads(row["fields_json"]) == {
        "guard_kind": "data_feed", "state": "contact_stopped", "generation": 10}
