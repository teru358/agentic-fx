"""終端 snapshot を outbox とする、起動時の取りこぼし回収。"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta, timezone

import pytest

from agentic_fx.store import improve_runs, improve_waves, missions

from ._improve_seed import seed

CODE = "improve_run_changed"


def events(env):
    return [dict(row) for row in env.conn.execute(
        "SELECT id,code,ref,fields_json FROM ops_events ORDER BY id").fetchall()]


def finished_refs(env):
    return [(e["ref"], json.loads(e["fields_json"])) for e in events(env)
            if e["code"] == CODE]


def put_event(env, *, code=CODE, ref, state, extra=None):
    fields = {"state": state, "trigger": "manual", "result_code": None,
              "reason_code": None, "tool_calls": None}
    fields.update(extra or {})
    env.conn.execute("INSERT INTO ops_events(ts,code,ref,fields_json) VALUES (?,?,?,?)",
                     (env.wall().isoformat(), code, ref, json.dumps(fields)))
    env.conn.commit()


def made(env, *, age=timedelta(0), **kwargs):
    ended = env.wall() - age
    return seed(env, started=ended - timedelta(minutes=5), ended=ended, **kwargs)


def test_unannounced_terminal_mission_gets_one_finished_event(ops_env):
    mission_id = made(ops_env, trigger="scheduled", tool_calls=12)
    svc = ops_env.service()
    assert svc.emit_pending_improve_notices(enabled=True) == 1
    assert finished_refs(ops_env) == [(f"improve:{mission_id}", {
        "state": "finished", "trigger": "scheduled", "result_code": "gate_failed",
        "reason_code": "unprofitable", "tool_calls": 12})]


def test_a_second_start_does_not_announce_again(ops_env):
    made(ops_env)
    svc = ops_env.service()
    assert svc.emit_pending_improve_notices(enabled=True) == 1
    assert svc.emit_pending_improve_notices(enabled=True) == 0
    assert ops_env.service().emit_pending_improve_notices(enabled=True) == 0
    assert len(finished_refs(ops_env)) == 1


def test_a_started_event_alone_does_not_count_as_announced(ops_env):
    mission_id = made(ops_env)
    put_event(ops_env, ref=f"improve:{mission_id}", state="started")
    assert ops_env.service().emit_pending_improve_notices(enabled=True) == 1
    states = [f["state"] for _, f in finished_refs(ops_env)]
    assert states == ["started", "finished"]


def test_started_is_never_supplied(ops_env):
    made(ops_env)
    ops_env.service().emit_pending_improve_notices(enabled=True)
    assert {f["state"] for _, f in finished_refs(ops_env)} == {"finished"}


def test_other_missions_other_codes_and_other_refs_do_not_count(ops_env):
    mission_id = made(ops_env)
    ref = f"improve:{mission_id}"
    put_event(ops_env, ref=f"improve:{mission_id + 1}", state="finished",
              extra={"result_code": "submitted"})
    put_event(ops_env, code="approval_state_changed", ref=ref, state="finished")
    put_event(ops_env, ref=ref, state="started")
    assert ops_env.service().emit_pending_improve_notices(enabled=True) == 1


def test_a_finished_event_with_the_same_code_and_ref_counts(ops_env):
    mission_id = made(ops_env)
    put_event(ops_env, ref=f"improve:{mission_id}", state="finished",
              extra={"result_code": "gate_failed", "reason_code": "unprofitable"})
    assert ops_env.service().emit_pending_improve_notices(enabled=True) == 0


@pytest.mark.parametrize("age, expected", [
    (timedelta(days=7) - timedelta(seconds=1), 1),
    (timedelta(days=7), 1),
    (timedelta(days=7) + timedelta(seconds=1), 0),
    (timedelta(days=8), 0),
    (timedelta(days=30), 0),
    (timedelta(0), 1)])
def test_seven_day_boundary_includes_exactly_seven_days(ops_env, age, expected):
    made(ops_env, age=age)
    assert ops_env.service().emit_pending_improve_notices(enabled=True) == expected


def test_old_snapshots_are_left_unannounced_but_remain(ops_env):
    made(ops_env, age=timedelta(days=9))
    ops_env.service().emit_pending_improve_notices(enabled=True)
    assert ops_env.conn.execute("SELECT count(*) FROM improve_run_notices").fetchone()[0] == 1


def test_offset_notation_is_compared_as_a_time_not_as_text(ops_env):
    mission_id = made(ops_env)
    # +09:00 表記: 文字列では wall より大きいが、時刻としては 8 日前。
    old = (ops_env.wall() - timedelta(days=8)).astimezone(timezone(timedelta(hours=9)))
    ops_env.conn.execute("DROP TRIGGER improve_run_notices_no_update")
    ops_env.conn.execute("UPDATE improve_run_notices SET created_at=? WHERE mission_id=?",
                         (old.isoformat(), mission_id))
    ops_env.conn.commit()
    assert ops_env.service().emit_pending_improve_notices(enabled=True) == 0
    recent = (ops_env.wall() - timedelta(days=1)).astimezone(timezone(timedelta(hours=-5)))
    ops_env.conn.execute("UPDATE improve_run_notices SET created_at=? WHERE mission_id=?",
                         (recent.isoformat(), mission_id))
    ops_env.conn.commit()
    assert ops_env.service().emit_pending_improve_notices(enabled=True) == 1


def test_naive_timestamps_are_treated_as_utc(ops_env):
    mission_id = made(ops_env)
    naive = (ops_env.wall() - timedelta(days=1)).replace(tzinfo=None)
    ops_env.conn.execute("DROP TRIGGER improve_run_notices_no_update")
    ops_env.conn.execute("UPDATE improve_run_notices SET created_at=? WHERE mission_id=?",
                         (naive.isoformat(), mission_id))
    ops_env.conn.commit()
    assert ops_env.service().emit_pending_improve_notices(enabled=True) == 1


def test_disabled_emits_nothing_and_keeps_the_snapshot(ops_env):
    made(ops_env)
    assert ops_env.service().emit_pending_improve_notices(enabled=False) == 0
    assert events(ops_env) == []
    assert ops_env.conn.execute("SELECT count(*) FROM improve_run_notices").fetchone()[0] == 1
    # 設定を戻した次の起動で、期限内の分は出る。
    assert ops_env.service().emit_pending_improve_notices(enabled=True) == 1


def test_oldest_first_then_mission_id(ops_env):
    newer = made(ops_env, age=timedelta(hours=1))
    older = made(ops_env, age=timedelta(hours=3))
    same_a = made(ops_env, age=timedelta(hours=2))
    same_b = made(ops_env, age=timedelta(hours=2))
    assert ops_env.service().emit_pending_improve_notices(enabled=True) == 4
    order = [ref for ref, _ in finished_refs(ops_env)]
    assert order == [f"improve:{older}", f"improve:{same_a}", f"improve:{same_b}",
                     f"improve:{newer}"]
    assert same_a < same_b


def test_one_invalid_snapshot_does_not_stop_the_rest(ops_env):
    first = made(ops_env, age=timedelta(hours=3))
    bad = made(ops_env, age=timedelta(hours=2))
    last = made(ops_env, age=timedelta(hours=1))
    ops_env.conn.execute("DROP TRIGGER improve_run_notices_no_update")
    ops_env.conn.execute(
        "UPDATE improve_run_notices SET reason_code='pf:7.7777' WHERE mission_id=?", (bad,))
    ops_env.conn.commit()
    assert ops_env.service().emit_pending_improve_notices(enabled=True) == 2
    assert [ref for ref, _ in finished_refs(ops_env)] == [
        f"improve:{first}", f"improve:{last}"]
    assert "7.7777" not in json.dumps(events(ops_env))


def test_failed_insert_is_retried_on_the_next_start(ops_env, monkeypatch):
    mission_id = made(ops_env)
    svc = ops_env.service()

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as patch:
        patch.setattr(svc.events, "emit", broken)
        assert svc.emit_pending_improve_notices(enabled=True) == 0
    assert finished_refs(ops_env) == []
    assert ops_env.service().emit_pending_improve_notices(enabled=True) == 1
    assert [ref for ref, _ in finished_refs(ops_env)] == [f"improve:{mission_id}"]


def test_outbox_failure_is_swallowed_with_a_fixed_word(ops_env, caplog, monkeypatch):
    made(ops_env)
    svc = ops_env.service()

    def boom(*args, **kwargs):
        raise RuntimeError("secret detail /home/x https://u.example/?k=1")

    monkeypatch.setattr(type(svc), "_emit_pending_improve_notices", boom)
    with caplog.at_level(logging.DEBUG):
        assert svc.emit_pending_improve_notices(enabled=True) == 0
    messages = [record.getMessage() for record in caplog.records]
    assert "improve_notice_skipped reason=outbox_failed" in messages
    assert not any("secret" in m or "https" in m for m in messages)
    assert all(r.exc_info is None for r in caplog.records)


def test_schema_skip_logs_only_a_fixed_word(ops_env, caplog):
    mission_id = made(ops_env)
    ops_env.conn.execute("DROP TRIGGER improve_run_notices_no_update")
    ops_env.conn.execute(
        "UPDATE improve_run_notices SET reason_code='OSError:boom 12' WHERE mission_id=?",
        (mission_id,))
    ops_env.conn.commit()
    with caplog.at_level(logging.DEBUG):
        ops_env.service().emit_pending_improve_notices(enabled=True)
    assert [r.getMessage() for r in caplog.records
            if "improve_notice_skipped" in r.getMessage()] == [
        "improve_notice_skipped reason=schema"]


# ---- 再起動回収との組み合わせ ----

def _running_improve(env, *, trigger):
    now = env.wall() - timedelta(hours=2)
    mission_id = missions.start(env.conn, "improve", "local", "m", now)
    improve_runs.start(env.conn, None, now, mission_id=mission_id, trigger=trigger)
    return mission_id


@pytest.mark.parametrize("trigger", ["manual", "scheduled", None])
def test_interrupted_recovery_is_announced_with_the_runs_trigger(ops_env, trigger):
    mission_id = _running_improve(ops_env, trigger=trigger)
    missions.recover_interrupted(ops_env.conn, now=ops_env.wall(), max_requeue=3)
    assert ops_env.service().emit_pending_improve_notices(enabled=True) == 1
    assert finished_refs(ops_env) == [(f"improve:{mission_id}", {
        "state": "finished", "trigger": trigger or "manual", "result_code": "failed",
        "reason_code": "interrupted", "tool_calls": None})]


def test_scheduled_trigger_survives_slot_cleanup_and_pre_ready_rollback(ops_env):
    now = ops_env.wall() - timedelta(hours=2)
    improve_waves.create_wave_and_slots(ops_env.conn, period_key="2026-W43", now=now,
                                        expected=1, commit=True)
    mission_id = missions.start(ops_env.conn, "improve", "local", "m", now)
    improve_runs.start(ops_env.conn, None, now, mission_id=mission_id, trigger="scheduled")
    improve_waves.claim_slot(ops_env.conn, period_key="2026-W43", k=0,
                             mission_id=mission_id, now=now)
    improve_waves.revert_to_reserved(ops_env.conn, period_key="2026-W43", k=0, now=now,
                                     commit=True)
    missions.recover_interrupted(ops_env.conn, now=ops_env.wall(), max_requeue=3)
    slot = ops_env.conn.execute(
        "SELECT mission_id FROM improve_wave_slots").fetchone()
    assert slot["mission_id"] is None
    ops_env.service().emit_pending_improve_notices(enabled=True)
    assert finished_refs(ops_env)[0][1]["trigger"] == "scheduled"


def test_crash_between_recovery_commit_and_emit_is_announced_next_start(ops_env):
    mission_id = _running_improve(ops_env, trigger="scheduled")
    missions.recover_interrupted(ops_env.conn, now=ops_env.wall(), max_requeue=3)
    assert finished_refs(ops_env) == []
    assert ops_env.service().emit_pending_improve_notices(enabled=True) == 1
    assert ops_env.service().emit_pending_improve_notices(enabled=True) == 0
    assert [r for r, _ in finished_refs(ops_env)] == [f"improve:{mission_id}"]


def test_rows_outside_the_window_are_removed_by_sql_not_by_python(ops_env):
    made(ops_env, age=timedelta(days=30))
    made(ops_env, age=timedelta(days=9))
    fresh = made(ops_env)
    svc = ops_env.service()
    fetched = []
    original = svc.events.unannounced_improve_finishes

    def spy(**kwargs):
        rows = original(**kwargs)
        fetched.append([row["mission_id"] for row in rows])
        return rows

    svc.events.unannounced_improve_finishes = spy
    assert svc.emit_pending_improve_notices(enabled=True) == 1
    assert fetched == [[fresh]]
