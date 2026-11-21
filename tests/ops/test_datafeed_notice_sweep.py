"""データの停止・再開待ちの通知記録を event にする sweep。

実 sqlite と実 EventStore で動かす。時刻は固定の例で、期待値は設計の表から手で書いた値。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agentic_fx.ops import datafeed_notices
from agentic_fx.ops.events import MAX_POLL_LIMIT
from agentic_fx.store import alert_state

UTC = timezone.utc
FLOOR = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)  # ops_env の wall clock の開始時刻


@pytest.fixture
def rig(ops_env):
    service = ops_env.service(datafeed_notices=True)
    return SimpleNamespace(env=ops_env, service=service, conn=ops_env.conn)


def _notice(conn, epoch, state, *, at, key="", event_id=None):
    conn.execute(
        "INSERT INTO datafeed_outage_notices(epoch, notice_state, request_key, occurred_at, event_id) "
        "VALUES (?,?,?,?,?)", (epoch, state, key, at.isoformat() if isinstance(at, datetime) else at,
                               event_id))
    conn.commit()


def _events(conn, guard_kind="data_feed"):
    out = []
    for row in conn.execute("SELECT id, ts, ref, fields_json FROM ops_events "
                            "WHERE code='guard_state_changed' ORDER BY id"):
        fields = json.loads(row["fields_json"])
        if fields["guard_kind"] == guard_kind:
            out.append((fields["state"], fields["generation"]))
    return out


def _event_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM ops_events ORDER BY id")]


def _link(conn, epoch, state, key=""):
    return conn.execute(
        "SELECT event_id FROM datafeed_outage_notices WHERE epoch=? AND notice_state=? "
        "AND request_key=?", (epoch, state, key)).fetchone()["event_id"]


def _sweep(rig, *, improve=False):
    rig.service.emit_pending_notices(improve_enabled=improve)


def _first_sweep(rig):
    """有効化の時刻 (下限) を置くだけの最初の回収。"""
    _sweep(rig)
    assert alert_state.get(rig.conn, datafeed_notices.PRODUCER_KEY) is not None


def _seed_existing_event(conn, state, generation, *, guard_kind="data_feed"):
    conn.execute(
        "INSERT INTO ops_events(ts,code,ref,fields_json) VALUES (?,?,?,?)",
        (FLOOR.isoformat(), "guard_state_changed", "instance",
         json.dumps({"guard_kind": guard_kind, "state": state, "generation": generation},
                    sort_keys=True, separators=(",", ":"))))
    conn.commit()


# ---- 有効化の下限 ---------------------------------------------------------

def test_a_daemon_without_the_producer_flag_never_sweeps_the_records(ops_env):
    service = ops_env.service()
    _notice(ops_env.conn, 5, "stopped", at=FLOOR + timedelta(minutes=1))
    service.emit_pending_notices(improve_enabled=False)
    assert _events(ops_env.conn) == []
    assert alert_state.get(ops_env.conn, datafeed_notices.PRODUCER_KEY) is None


def test_the_first_sweep_persists_the_enabled_time_and_a_restart_never_moves_it(rig):
    _sweep(rig)
    first = alert_state.get(rig.conn, datafeed_notices.PRODUCER_KEY)
    assert first == FLOOR.isoformat()
    rig.env.wall.advance(days=3)
    _sweep(rig)
    again = rig.env.service(datafeed_notices=True)
    again.emit_pending_notices(improve_enabled=False)
    assert alert_state.get(rig.conn, datafeed_notices.PRODUCER_KEY) == first


def test_a_record_older_than_the_enabled_time_is_never_sent(rig):
    _notice(rig.conn, 60, "stopped", at=FLOOR - timedelta(hours=1))
    _notice(rig.conn, 60, "awaiting_resume", at=FLOOR - timedelta(seconds=1))
    _notice(rig.conn, 61, "stopped", at=FLOOR)
    _sweep(rig)
    assert _events(rig.conn) == [("stopped", 61)]
    assert _link(rig.conn, 60, "stopped") is None


def test_a_record_with_the_enabled_time_itself_is_sent(rig):
    _notice(rig.conn, 7, "stopped", at=FLOOR)
    _sweep(rig)
    assert _events(rig.conn) == [("stopped", 7)]


def test_the_floor_compares_instants_not_strings(rig):
    """秒未満が 0 のとき isoformat は小数部を省く。文字列の大小で下限を決めない。"""
    _notice(rig.conn, 8, "stopped", at="2026-10-05T00:00:00+00:00")
    _notice(rig.conn, 9, "stopped", at="2026-10-04T23:59:59.999999+00:00")
    _sweep(rig)
    assert _events(rig.conn) == [("stopped", 8)]


# ---- event の中身と対応の保存 ---------------------------------------------

def test_a_transition_becomes_one_fixed_guard_event_and_the_record_keeps_its_id(rig):
    _first_sweep(rig)
    _notice(rig.conn, 66, "awaiting_resume", at=FLOOR + timedelta(minutes=3))
    _sweep(rig)
    row = _event_rows(rig.conn)[0]
    assert (row["code"], row["ref"]) == ("guard_state_changed", "instance")
    assert json.loads(row["fields_json"]) == {
        "guard_kind": "data_feed", "state": "awaiting_resume", "generation": 66}
    assert _link(rig.conn, 66, "awaiting_resume") == row["id"]


def test_every_notice_state_maps_to_the_event_state_of_the_same_name(rig):
    _first_sweep(rig)
    names = ("stopped", "awaiting_resume", "resume_accepted", "resume_rejected",
             "resume_already_ready", "resumed")
    for index, name in enumerate(names):
        _notice(rig.conn, 1, name, at=FLOOR + timedelta(minutes=index),
                key="k" if name.startswith("resume_") and name != "resumed" else "")
    _sweep(rig)
    assert sorted(_events(rig.conn)) == sorted((n, 1) for n in names)


def test_repeated_sweeps_and_restarts_keep_one_event_per_record(rig):
    _first_sweep(rig)
    _notice(rig.conn, 66, "stopped", at=FLOOR + timedelta(minutes=1))
    for _ in range(3):
        _sweep(rig)
    rig.env.service(datafeed_notices=True).emit_pending_notices(improve_enabled=False)
    assert _events(rig.conn) == [("stopped", 66)]


def test_the_event_time_never_falls_below_the_occurrence_time(rig):
    _first_sweep(rig)
    later = FLOOR + timedelta(hours=2)
    _notice(rig.conn, 66, "stopped", at=later)
    _sweep(rig)
    ts = datetime.fromisoformat(_event_rows(rig.conn)[0]["ts"])
    assert ts == later


# ---- 重複の抑止 -----------------------------------------------------------

def test_an_existing_event_for_the_same_transition_is_linked_not_emitted_again(rig):
    _first_sweep(rig)
    _seed_existing_event(rig.conn, "stopped", 66)
    existing = _event_rows(rig.conn)[0]["id"]
    _notice(rig.conn, 66, "stopped", at=FLOOR + timedelta(minutes=1))
    _sweep(rig)
    assert len(_event_rows(rig.conn)) == 1
    assert _link(rig.conn, 66, "stopped") == existing


def test_a_kill_switch_event_never_stands_in_for_a_data_feed_transition(rig):
    _first_sweep(rig)
    _seed_existing_event(rig.conn, "stopped", 66, guard_kind="kill_switch")
    _notice(rig.conn, 66, "stopped", at=FLOOR + timedelta(minutes=1))
    _sweep(rig)
    assert _events(rig.conn, "data_feed") == [("stopped", 66)]
    assert len(_event_rows(rig.conn)) == 2


def test_the_same_state_of_another_generation_is_a_different_transition(rig):
    _first_sweep(rig)
    _seed_existing_event(rig.conn, "stopped", 65)
    _notice(rig.conn, 66, "stopped", at=FLOOR + timedelta(minutes=1))
    _sweep(rig)
    assert _events(rig.conn) == [("stopped", 65), ("stopped", 66)]


def test_terminal_results_of_one_epoch_match_by_request_key_not_by_state(rig):
    """同じ epoch の拒否が 2 件あれば、state の一致だけで既出とせず要求ごとに 1 件ずつ出す。"""
    _first_sweep(rig)
    _seed_existing_event(rig.conn, "resume_rejected", 66)
    seeded = _event_rows(rig.conn)[0]["id"]
    _notice(rig.conn, 66, "resume_rejected", key="k1", at=FLOOR + timedelta(minutes=1),
            event_id=seeded)
    _notice(rig.conn, 66, "resume_rejected", key="k2", at=FLOOR + timedelta(minutes=2))
    _notice(rig.conn, 66, "resume_accepted", key="k3", at=FLOOR + timedelta(minutes=3))
    _sweep(rig)
    assert _events(rig.conn) == [("resume_rejected", 66), ("resume_rejected", 66),
                                 ("resume_accepted", 66)]
    ids = {_link(rig.conn, 66, s, k) for s, k in (("resume_rejected", "k1"),
                                                    ("resume_rejected", "k2"),
                                                    ("resume_accepted", "k3"))}
    assert len(ids) == 3 and seeded in ids
    _sweep(rig)
    assert len(_event_rows(rig.conn)) == 3


# ---- 順序と有限 batch -----------------------------------------------------

def test_records_go_out_by_time_then_epoch_then_stop_wait_result_resumed(rig):
    _first_sweep(rig)
    at = FLOOR + timedelta(minutes=5)
    _notice(rig.conn, 66, "resumed", at=at)
    _notice(rig.conn, 66, "resume_accepted", key="k", at=at)
    _notice(rig.conn, 66, "awaiting_resume", at=at)
    _notice(rig.conn, 66, "stopped", at=at)
    _notice(rig.conn, 65, "resumed", at=at)
    _notice(rig.conn, 70, "stopped", at=FLOOR + timedelta(minutes=1))
    _sweep(rig)
    assert _events(rig.conn) == [
        ("stopped", 70), ("resumed", 65), ("stopped", 66), ("awaiting_resume", 66),
        ("resume_accepted", 66), ("resumed", 66)]


def test_an_older_time_in_a_later_epoch_still_goes_first(rig):
    _first_sweep(rig)
    _notice(rig.conn, 9, "stopped", at=FLOOR + timedelta(minutes=9))
    _notice(rig.conn, 10, "stopped", at=FLOOR + timedelta(minutes=1))
    _sweep(rig)
    assert _events(rig.conn) == [("stopped", 10), ("stopped", 9)]


def test_a_sweep_sends_a_bounded_batch_and_the_rest_goes_next_time(rig):
    assert 0 < datafeed_notices.BATCH_LIMIT <= MAX_POLL_LIMIT
    _first_sweep(rig)
    total = datafeed_notices.BATCH_LIMIT + 7
    for epoch in range(total):
        _notice(rig.conn, epoch, "stopped", at=FLOOR + timedelta(minutes=1, seconds=epoch))
    _sweep(rig)
    assert len(_events(rig.conn)) == datafeed_notices.BATCH_LIMIT
    _sweep(rig)
    assert [g for _, g in _events(rig.conn)] == list(range(total))


def test_a_record_of_an_earlier_epoch_is_collected_after_the_epoch_moved_on(rig):
    _first_sweep(rig)
    _notice(rig.conn, 66, "stopped", at=FLOOR + timedelta(minutes=1))
    rig.conn.execute(
        "INSERT INTO datafeed_outage_state(id,state,epoch,updated_at) VALUES (1,'ready',70,?)",
        (FLOOR.isoformat(),))
    rig.conn.commit()
    _sweep(rig)
    assert _events(rig.conn) == [("stopped", 66)]


# ---- 固定 schema に収まらない記録 ----------------------------------------

def test_a_record_outside_the_event_schema_is_skipped_and_later_records_still_go(rig, caplog):
    _first_sweep(rig)
    _notice(rig.conn, -1, "stopped", at=FLOOR + timedelta(minutes=1))
    _notice(rig.conn, "x", "stopped", at=FLOOR + timedelta(minutes=2))
    _notice(rig.conn, 3, "stopped", at="not-a-time")
    _notice(rig.conn, 4, "stopped", at="2026-10-05T01:00:00")  # naive
    _notice(rig.conn, 5, "stopped", at=FLOOR + timedelta(minutes=3))
    with caplog.at_level(logging.WARNING):
        _sweep(rig)
    assert _events(rig.conn) == [("stopped", 5)]
    assert any(r.getMessage() == "datafeed_notice_skipped reason=schema" for r in caplog.records)
    _sweep(rig)
    assert _events(rig.conn) == [("stopped", 5)]


# ---- 保存の失敗 -----------------------------------------------------------

def test_a_failure_between_the_event_and_the_link_rolls_both_back(rig, caplog):
    _first_sweep(rig)
    _notice(rig.conn, 66, "stopped", at=FLOOR + timedelta(minutes=1))
    rig.conn.execute(
        "CREATE TRIGGER block_link BEFORE UPDATE ON datafeed_outage_notices "
        "BEGIN SELECT RAISE(ABORT,'blocked'); END")
    rig.conn.commit()
    with caplog.at_level(logging.WARNING):
        _sweep(rig)
    assert _event_rows(rig.conn) == []
    assert _link(rig.conn, 66, "stopped") is None
    assert any(r.getMessage() == "datafeed_notice_deferred" for r in caplog.records)
    rig.conn.execute("DROP TRIGGER block_link")
    rig.conn.commit()
    _sweep(rig)
    assert _events(rig.conn) == [("stopped", 66)]
    assert _link(rig.conn, 66, "stopped") is not None


def test_a_failed_event_insert_keeps_the_record_and_stops_the_batch_for_this_time(rig, caplog):
    _first_sweep(rig)
    _notice(rig.conn, 66, "stopped", at=FLOOR + timedelta(minutes=1))
    _notice(rig.conn, 67, "stopped", at=FLOOR + timedelta(minutes=2))
    rig.conn.execute(
        "CREATE TRIGGER block_events BEFORE INSERT ON ops_events "
        "BEGIN SELECT RAISE(ABORT,'blocked'); END")
    rig.conn.commit()
    with caplog.at_level(logging.WARNING):
        _sweep(rig)
    assert _events(rig.conn) == []
    assert _link(rig.conn, 66, "stopped") is None and _link(rig.conn, 67, "stopped") is None
    assert not rig.conn.in_transaction
    rig.conn.execute("DROP TRIGGER block_events")
    rig.conn.commit()
    _sweep(rig)
    assert _events(rig.conn) == [("stopped", 66), ("stopped", 67)]


def test_a_missing_notice_table_defers_without_raising(rig, caplog):
    rig.conn.execute("DROP TABLE datafeed_outage_notices")
    rig.conn.commit()
    with caplog.at_level(logging.WARNING):
        _sweep(rig)
    assert any(r.getMessage() == "datafeed_notice_deferred" for r in caplog.records)


# ---- 他の sweep との独立 --------------------------------------------------

def _latch_kill_switch(env):
    env.state.update(kill_switch_latched=True)
    return env.state


def test_a_broken_datafeed_sweep_does_not_stop_the_kill_switch_sweep(ops_env, monkeypatch):
    state = _latch_kill_switch(ops_env)
    service = ops_env.service(datafeed_notices=True, state_store=state)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(datafeed_notices, "sweep", boom)
    service.emit_pending_notices(improve_enabled=False)
    assert _events(ops_env.conn, "kill_switch") == [("latched", 1)]


def test_a_broken_kill_switch_sweep_does_not_stop_the_datafeed_sweep(ops_env, monkeypatch):
    service = ops_env.service(datafeed_notices=True)
    _first = service.emit_pending_notices
    _first(improve_enabled=False)
    _notice(ops_env.conn, 66, "stopped", at=FLOOR + timedelta(minutes=1))

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_guard_notices_in_lane", boom)
    service.emit_pending_notices(improve_enabled=False)
    assert _events(ops_env.conn) == [("stopped", 66)]


def test_the_datafeed_sweep_runs_with_the_improve_setting_off(rig):
    _first_sweep(rig)
    _notice(rig.conn, 66, "stopped", at=FLOOR + timedelta(minutes=1))
    _sweep(rig, improve=False)
    assert _events(rig.conn) == [("stopped", 66)]


# ---- event INSERT の primitive --------------------------------------------

def test_the_producer_key_is_a_known_alert_state_key(rig):
    alert_state.get(rig.conn, datafeed_notices.PRODUCER_KEY)


# ---- 有効化の時刻は構築時に置く ------------------------------------------

def test_the_enabled_time_is_stored_when_the_service_is_built_even_if_the_lane_is_busy(
        ops_env, monkeypatch):
    service = ops_env.service(datafeed_notices=True)
    assert alert_state.get(ops_env.conn, datafeed_notices.PRODUCER_KEY) == FLOOR.isoformat()
    from agentic_fx.ops.contracts import ErrorCode, OpsError

    def busy(*args, **kwargs):
        raise OpsError(ErrorCode.UNAVAILABLE)

    monkeypatch.setattr(service, "_ops_lane", busy)
    ops_env.wall.advance(hours=1)
    service.emit_pending_notices(improve_enabled=False)          # lane が空かず何もできない
    assert alert_state.get(ops_env.conn, datafeed_notices.PRODUCER_KEY) == FLOOR.isoformat()


def test_a_record_made_while_the_lane_was_busy_at_startup_is_sent_later(ops_env):
    service = ops_env.service(datafeed_notices=True)
    _notice(ops_env.conn, 66, "resume_rejected", key="k", at=FLOOR + timedelta(minutes=2))
    ops_env.wall.advance(hours=1)
    service.emit_pending_notices(improve_enabled=False)
    assert _events(ops_env.conn) == [("resume_rejected", 66)]


# ---- 1 件の失敗が後続を止めない ------------------------------------------

def test_a_record_that_keeps_failing_is_skipped_and_the_later_ones_still_go(rig):
    _first_sweep(rig)
    for epoch in (66, 67, 68):
        _notice(rig.conn, epoch, "stopped", at=FLOOR + timedelta(minutes=epoch - 60))
    rig.conn.execute(
        "CREATE TRIGGER block_one BEFORE UPDATE ON datafeed_outage_notices "
        "WHEN OLD.epoch=66 BEGIN SELECT RAISE(ABORT,'blocked'); END")
    rig.conn.commit()
    for _ in range(3):
        _sweep(rig)
    assert _events(rig.conn) == [("stopped", 67), ("stopped", 68)]
    assert _link(rig.conn, 66, "stopped") is None
    rig.conn.execute("DROP TRIGGER block_one")
    rig.conn.commit()
    _sweep(rig)
    assert _events(rig.conn) == [("stopped", 67), ("stopped", 68), ("stopped", 66)]


def test_a_failure_of_every_record_stops_the_pass_after_a_few_attempts(rig, monkeypatch):
    _first_sweep(rig)
    for epoch in range(10):
        _notice(rig.conn, epoch, "stopped", at=FLOOR + timedelta(minutes=1 + epoch))
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("down")

    monkeypatch.setattr(rig.service.events, "insert_data_feed_guard", fail)
    _sweep(rig)
    assert 1 <= len(calls) <= 3
