"""起動補完: 導入時点の停止中の episode から 0〜1 件だけ通知の記録を補う。

state の行は SQL で直接置く。時刻は固定の例 (水曜の開場中) で、期待値は設計の優先順から手で書いた。
"""
from __future__ import annotations

import contextlib
import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agentic_fx.ops import datafeed_notices
from agentic_fx.store import alert_state

UTC = timezone.utc
FLOOR = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)  # 水曜 10:00 UTC (開場中)


@pytest.fixture
def rig(ops_env):
    ops_env.wall.now = FLOOR
    service = ops_env.service(datafeed_notices=True)
    return SimpleNamespace(env=ops_env, service=service, conn=ops_env.conn)


def _state(conn, **cols):
    base = {"id": 1, "state": "degraded", "epoch": 66, "confirmed": 0,
            "entered_degraded_at": (FLOOR - timedelta(minutes=2)).isoformat(),
            "pending_human_confirmation": 0, "resume_requested_at": None,
            "resume_acknowledge": 0, "recovered_notified_epoch": None,
            "updated_at": FLOOR.isoformat()}
    base.update(cols)
    conn.execute("DELETE FROM datafeed_outage_state")
    conn.execute(f"INSERT INTO datafeed_outage_state({','.join(base)}) "
                 f"VALUES ({','.join('?' * len(base))})", tuple(base.values()))
    conn.commit()


def _records(conn):
    return [(r["epoch"], r["notice_state"], r["request_key"], r["occurred_at"], r["event_id"])
            for r in conn.execute("SELECT * FROM datafeed_outage_notices "
                                  "ORDER BY epoch, notice_state, request_key")]


def _events(conn):
    return [(json.loads(r["fields_json"])["state"], json.loads(r["fields_json"])["generation"])
            for r in conn.execute("SELECT fields_json FROM ops_events ORDER BY id")]


def _start(rig):
    rig.service.emit_pending_notices(improve_enabled=False)


def test_a_pending_request_supplements_nothing(rig):
    _state(rig.conn, confirmed=1, recovered_notified_epoch=66,
           resume_requested_at="2026-10-07T09:00:00+00:00")
    _start(rig)
    assert _records(rig.conn) == [] and _events(rig.conn) == []


def test_the_present_snapshot_with_a_request_supplements_nothing(rig):
    """導入時点の本番の形: degraded・epoch 66・confirmed・pending・待ちの印・ack 付きの要求。"""
    _state(rig.conn, confirmed=1, pending_human_confirmation=1, recovered_notified_epoch=66,
           resume_requested_at="2026-11-20T23:24:09.461457+00:00", resume_acknowledge=1,
           entered_degraded_at="2026-11-19T22:40:00+00:00")
    _start(rig)
    _start(rig)
    assert _records(rig.conn) == [] and _events(rig.conn) == []


def test_a_wait_mark_without_a_request_supplements_the_wait_only(rig):
    _state(rig.conn, confirmed=1, recovered_notified_epoch=66)
    _start(rig)
    assert [(r[0], r[1], r[2]) for r in _records(rig.conn)] == [(66, "awaiting_resume", "")]
    assert _events(rig.conn) == [("awaiting_resume", 66)]


def test_the_supplement_takes_the_enabled_time_as_its_occurrence_time(rig):
    _state(rig.conn, confirmed=1, recovered_notified_epoch=66)
    _start(rig)
    assert _records(rig.conn)[0][3] == FLOOR.isoformat()


def test_a_confirmed_stop_without_a_mark_supplements_the_stop_only(rig):
    _state(rig.conn, confirmed=1)
    _start(rig)
    assert [(r[0], r[1]) for r in _records(rig.conn)] == [(66, "stopped")]
    assert _events(rig.conn) == [("stopped", 66)]


def test_an_unconfirmed_stop_past_ten_open_minutes_supplements_the_stop(rig):
    _state(rig.conn, entered_degraded_at=(FLOOR - timedelta(minutes=10)).isoformat())
    _start(rig)
    assert _events(rig.conn) == [("stopped", 66)]


def test_an_unconfirmed_stop_short_of_ten_open_minutes_supplements_nothing(rig):
    _state(rig.conn, entered_degraded_at=(FLOOR - timedelta(minutes=9)).isoformat())
    _start(rig)
    assert _records(rig.conn) == [] and _events(rig.conn) == []


@pytest.mark.parametrize("state", ["ready", "restricted"])
def test_ready_and_restricted_supplement_nothing(rig, state):
    _state(rig.conn, state=state, confirmed=1, recovered_notified_epoch=66)
    _start(rig)
    assert _records(rig.conn) == [] and _events(rig.conn) == []


def test_no_state_row_supplements_nothing(rig):
    _start(rig)
    assert _records(rig.conn) == []


def test_closed_episodes_of_earlier_epochs_are_not_supplemented(rig):
    rig.conn.execute(
        "INSERT INTO datafeed_outage_notices VALUES (65,'stopped','',?,NULL)",
        ((FLOOR - timedelta(days=1)).isoformat(),))
    rig.conn.commit()
    _state(rig.conn, confirmed=1)
    _start(rig)
    assert _events(rig.conn) == [("stopped", 66)]
    assert [r[0] for r in _records(rig.conn)] == [65, 66]


def test_rerunning_the_supplement_adds_nothing_and_never_moves_the_time(rig):
    _state(rig.conn, confirmed=1, recovered_notified_epoch=66)
    _start(rig)
    first = _records(rig.conn)
    rig.env.wall.advance(hours=5)
    rig.env.service(datafeed_notices=True).emit_pending_notices(improve_enabled=False)
    assert _records(rig.conn) == first
    assert len(_events(rig.conn)) == 1


def test_an_earlier_unsent_record_of_the_same_key_is_moved_to_the_enabled_time(rig):
    old = FLOOR - timedelta(days=2)
    rig.conn.execute("INSERT INTO datafeed_outage_notices VALUES (66,'stopped','',?,NULL)",
                     (old.isoformat(),))
    rig.conn.commit()
    _state(rig.conn, confirmed=1)
    _start(rig)
    rows = _records(rig.conn)
    assert len(rows) == 1 and rows[0][3] == FLOOR.isoformat() and rows[0][4] is not None
    assert _events(rig.conn) == [("stopped", 66)]


def test_a_record_made_after_the_enabled_time_is_left_as_it_is(rig):
    _state(rig.conn, confirmed=1)
    _start(rig)
    later = (FLOOR + timedelta(hours=1)).isoformat()
    rig.conn.execute("UPDATE datafeed_outage_notices SET occurred_at=?, event_id=NULL", (later,))
    rig.conn.commit()
    rig.env.service(datafeed_notices=True).emit_pending_notices(improve_enabled=False)
    assert _records(rig.conn)[0][3] == later


def test_a_sent_record_is_never_touched_by_the_supplement(rig):
    _state(rig.conn, confirmed=1)
    _start(rig)
    before = _records(rig.conn)
    rig.env.service(datafeed_notices=True).emit_pending_notices(improve_enabled=False)
    assert _records(rig.conn) == before and len(_events(rig.conn)) == 1


def test_the_supplement_runs_once_per_process_not_every_tick(rig):
    _start(rig)  # 起動: state の行は無い
    _state(rig.conn, confirmed=1)
    _start(rig)  # tick
    assert _records(rig.conn) == []


def test_a_failed_supplement_is_retried_on_the_next_tick(rig):
    _state(rig.conn, confirmed=1)
    rig.conn.execute("ALTER TABLE datafeed_outage_notices RENAME TO notices_away")
    rig.conn.commit()
    _start(rig)
    rig.conn.execute("ALTER TABLE notices_away RENAME TO datafeed_outage_notices")
    rig.conn.commit()
    _start(rig)
    assert _events(rig.conn) == [("stopped", 66)]


def test_a_daemon_without_the_producer_flag_supplements_nothing(ops_env):
    ops_env.wall.now = FLOOR
    _state(ops_env.conn, confirmed=1)
    ops_env.service().emit_pending_notices(improve_enabled=False)
    assert _records(ops_env.conn) == []
    assert alert_state.get(ops_env.conn, datafeed_notices.PRODUCER_KEY) is None


def test_a_later_restart_supplements_with_the_time_it_was_made_not_the_enabled_time(rig):
    _state(rig.conn, entered_degraded_at=(FLOOR - timedelta(minutes=2)).isoformat())
    _start(rig)                                   # 導入: 停止条件は未到達で 0 件
    assert _records(rig.conn) == []
    other = FLOOR + timedelta(minutes=5)          # 間に別の記録がある
    rig.conn.execute("INSERT INTO datafeed_outage_notices VALUES (66,'resume_rejected','k',?,NULL)",
                     (other.isoformat(),))
    rig.conn.commit()
    later = FLOOR + timedelta(minutes=12)
    rig.env.wall.now = later
    rig.env.service(datafeed_notices=True).emit_pending_notices(improve_enabled=False)
    stopped = [r for r in _records(rig.conn) if r[1] == "stopped"]
    assert len(stopped) == 1 and stopped[0][3] == later.isoformat()
    order = [(json.loads(r["fields_json"])["state"]) for r in
             rig.conn.execute("SELECT fields_json FROM ops_events ORDER BY id")]
    assert order == ["resume_rejected", "stopped"]


def _fast_service(env):
    from agentic_fx.ops.service import OpsLimits
    return env.service(datafeed_notices=True, limits=OpsLimits(
        request_deadline_seconds=0.1, ops_lock_wait_seconds=0.05))


@contextlib.contextmanager
def _lane_held(service):
    held, release = threading.Event(), threading.Event()

    def holder():
        with service._ops_lock:
            held.set()
            release.wait(10)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(5)
    try:
        yield
    finally:
        release.set()
        thread.join(5)


def test_a_stop_that_became_true_after_the_snapshot_is_dated_when_it_was_made(ops_env):
    """導入時は条件未到達で、最初の補完が lane の混雑で遅れる間に停止が成り立つ。"""
    ops_env.wall.now = FLOOR
    _state(ops_env.conn, entered_degraded_at=(FLOOR - timedelta(minutes=2)).isoformat())
    service = _fast_service(ops_env)
    with _lane_held(service):
        service.emit_pending_notices(improve_enabled=False)
    ops_env.conn.execute("INSERT INTO datafeed_outage_notices VALUES (66,'resume_rejected','k',?,NULL)",
                         ((FLOOR + timedelta(minutes=5)).isoformat(),))
    ops_env.conn.commit()
    later = FLOOR + timedelta(minutes=12)
    ops_env.wall.now = later
    service.emit_pending_notices(improve_enabled=False)
    stopped = [r for r in _records(ops_env.conn) if r[1] == "stopped"]
    assert [r[3] for r in stopped] == [later.isoformat()]
    assert _events(ops_env.conn) == [("resume_rejected", 66), ("stopped", 66)]


def test_a_stop_that_was_true_at_the_snapshot_keeps_the_enabled_time_when_the_lane_was_busy(ops_env):
    ops_env.wall.now = FLOOR
    _state(ops_env.conn, confirmed=1)
    service = _fast_service(ops_env)
    with _lane_held(service):
        service.emit_pending_notices(improve_enabled=False)
    ops_env.wall.now = FLOOR + timedelta(minutes=30)
    service.emit_pending_notices(improve_enabled=False)
    assert [r[3] for r in _records(ops_env.conn)] == [FLOOR.isoformat()]
    assert _events(ops_env.conn) == [("stopped", 66)]
