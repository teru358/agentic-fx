"""観測の中断・読めない注文・Q と期限超過の同時成立・起動待ちの案内・手動再開の入口 x 状態の表。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.datafeed import guidance_text
from agentic_fx.datafeed.outage import OutageStateMachine, resume_guidance
from agentic_fx.datafeed.outage_transition import Facts, KeyView, outage_transition
from agentic_fx.store import outage_facts
from tests.datafeed.test_outage_observe_inspect import (
    GRACE, KEY, SRC, WIDTHS, _at, _bars, _contacts, _failed, _ok, _order, _rig, _row, _state,
    _tick)
from tests.datafeed.test_outage_transition_table import WED, _f, _k, _row as _trow

UTC = timezone.utc


def _restart(conn, **kw):
    return OutageStateMachine(conn, hard_keys=frozenset({KEY}), interval_widths=WIDTHS,
                              grace=GRACE, storage_source=SRC, **kw)


# ---- 観測の中断: 保存した停滞の起点が無い ready ----------------------------------

def test_a_ready_stop_of_forty_minutes_with_a_position_is_checked_and_stops_on_the_bulk_arrival(
        tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "ready", observed=_at(10, 0), updated=_at(10, 1), epoch=3)
    oid = _order(conn)
    _bars(conn, _at(10, 1), _at(10, 40), touch=_at(10, 20))
    restarted = _restart(conn)
    assert restarted.observe(_at(10, 42), _ok()) == "degraded"
    assert _row(conn)["epoch"] == 4
    assert [c[1] for c in _contacts(conn)] == [oid]


def test_a_flat_ready_stop_past_the_configured_limit_stops_without_a_human_wait(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "ready", observed=_at(10, 0), updated=_at(10, 1), epoch=3)
    _bars(conn, _at(10, 1), _at(11, 20))
    assert _restart(conn).observe(_at(11, 22), _ok()) == "degraded"
    assert _row(conn)["pending_human_confirmation"] == 0


@pytest.mark.parametrize("grace", [0, 1800])
def test_a_short_restart_of_a_ready_machine_does_not_stop(tmp_path, grace):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "ready", observed=_at(10, 0), updated=_at(10, 3), epoch=3)
    _order(conn)
    _bars(conn, _at(10, 1), _at(10, 2))
    restarted = _restart(conn, flat_stall_max_sec=grace)
    assert restarted.observe(_at(10, 4), _ok()) == "ready"


# ---- 読めない注文は SQL の絞込みで落とさない ------------------------------------

def test_an_unknown_status_order_with_an_old_update_time_is_still_checked(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), updated=_at(10, 41))
    oid = _order(conn, status="some_new_status", created=_at(10, 0) - timedelta(days=10))
    _bars(conn, _at(10, 1), _at(10, 40))
    machine.observe(_at(10, 42), _ok())
    assert {(c[1], c[3]) for c in _contacts(conn)} == {(oid, "unknown")}


def test_an_order_with_an_unreadable_time_is_still_checked(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "degraded", observed=_at(10, 0), updated=_at(10, 41))
    oid = _order(conn, status="closed", created=_at(10, 0) - timedelta(days=10),
                 closed_at=_at(10, 0) - timedelta(days=9))
    conn.execute("UPDATE orders SET created_at='garbage' WHERE id=?", (oid,))
    conn.commit()
    _bars(conn, _at(10, 1), _at(10, 40))
    machine.observe(_at(10, 42), _ok())
    assert {(c[1], c[3]) for c in _contacts(conn)} == {(oid, "unknown")}


# ---- Q と期限超過の同時成立 -------------------------------------------------------

def test_the_restricted_limit_and_an_overdue_stall_together_still_confirm_the_stop():
    since = (WED - timedelta(minutes=31)).isoformat()
    key = _k(stalled=True, s=WED - timedelta(minutes=40))
    nxt, eff = outage_transition(_trow("restricted", restricted_since=since),
                                 _f(keys=[key], healthy=False))
    assert nxt["state"] == "degraded" and nxt["confirmed"] == 1 and eff.confirmed_stop
    assert ("stopped", "") in eff.notices


# ---- restricted_since の無い旧い行 -------------------------------------------------

def test_a_restricted_row_without_a_start_cannot_return_and_waits_for_a_human():
    nxt, eff = outage_transition(_trow("restricted"), _f())
    assert nxt["state"] == "degraded" and eff.set_migration


def test_the_missing_start_is_saved_as_a_human_confirmation_wait(tmp_path):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, "restricted", observed=_at(10, 0), updated=_at(10, 4))
    _bars(conn, _at(10, 1), _at(10, 4))
    assert _tick(conn, machine, _at(10, 6)) == "degraded"
    assert _row(conn)["migration_confirmation_pending"] == 1


# ---- 起動待ちの案内 --------------------------------------------------------------

def test_the_startup_wait_is_a_published_phase_with_a_fixed_sentence():
    pair = resume_guidance(observe_failed=False, state="ready", request_pending=False,
                           last_result=None, awaiting_mark=False, unprocessed=None,
                           startup_wait=True)
    assert pair == ("starting", "wait_tick") and pair in guidance_text.PAIRS
    reason, _ = guidance_text.guidance_lines({"phase": "starting", "next_action": "wait_tick"})
    assert "起動直後" in reason


def test_the_snapshot_of_a_waiting_machine_matches_the_gate(tmp_path):
    conn, machine = _rig(tmp_path, startup_recovery=True)
    _state(conn, machine, "ready", epoch=2)
    snap = machine.guidance_snapshot(_at(10, 0), conn=conn)
    assert (snap["state"], snap["phase"]) == ("degraded", "starting")
    assert machine.state == "degraded"


# ---- 手動再開 x 接触 x ack x 手動健全 x 状態 ---------------------------------------

@pytest.mark.parametrize("healthy", [True, False])
@pytest.mark.parametrize("ack", [False, True])
@pytest.mark.parametrize("contact", [False, True])
@pytest.mark.parametrize("state", ["degraded", "restricted", "ready"])
def test_the_manual_resume_table(tmp_path, state, contact, ack, healthy):
    conn, machine = _rig(tmp_path)
    _state(conn, machine, state, epoch=3, observed=_at(10, 0), updated=_at(10, 4),
           since=_at(10, 0) if state == "restricted" else None)
    _bars(conn, _at(10, 1), _at(10, 4))
    if contact:
        outage_facts.add_contact(conn, epoch=3, order_id=9, pair="USDJPY", bar_time=_at(10, 2),
                                 kind="sl", detected_at=_at(10, 3))
        conn.commit()
    machine.request_resume(_at(10, 5), acknowledge=ack)
    now = _at(10, 6)
    result_state = _tick(conn, machine, now, _ok() if healthy else _failed())
    row = _row(conn)
    if not healthy:
        # ready でも失敗の tick は restricted を開き、要求は不健全で拒否される
        want = ("rejected_watermark", "restricted" if state == "ready" else state)
    elif state == "ready":
        want = ("already_ready", "ready")
    elif state == "restricted" and not contact:
        want = ("already_ready", "ready")        # 自動復帰が先に ready にする
    elif contact and not ack:
        want = ("rejected_unprocessed", state)
    else:
        want = ("accepted", "ready")
    assert (row["resume_last_result"], result_state) == want
    if want[0] == "accepted":
        assert _contacts(conn) == []
