from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.backtest.replay import ReplayClock
from agentic_fx.backtest.runner import (
    _RecordingActivity, _RecordingStateStore, _auto_release_kill_switch,
    _kill_switch_event, _release_is_due,
    _executor_transition_snapshot_id,
)
from agentic_fx.store import snapshots
from agentic_fx.store.db import connect, init_db

UTC = timezone.utc


def _mtm(conn, ts, *, equity=98_000.0, hwm=100_000.0):
    ident = snapshots.add(conn, ts=ts, balance=98_000.0, equity=equity,
                          hwm=hwm, cashflow=0, source="paper")
    return {**snapshots.latest(conn), "id": ident}


@pytest.mark.parametrize(("now", "expected"), [
    (datetime(2026, 9, 5, 21, tzinfo=UTC), False),
    (datetime(2026, 9, 6, 21, tzinfo=UTC), True),
    (datetime(2025, 12, 25, 20, 59, tzinfo=UTC), False),
    (datetime(2025, 12, 25, 21, tzinfo=UTC), True),
])
def test_release_due_waits_for_real_market_open(now, expected):
    latched_at = (datetime(2026, 9, 4, 20, tzinfo=UTC)
                  if now.month == 9 else datetime(2025, 12, 24, 20, tzinfo=UTC))
    mtm = {"id": 2, "ts": now.isoformat(), "source": "paper"}
    assert _release_is_due(now=now, latched_at=latched_at, mtm=mtm,
                           before_id=1) is expected


def test_release_due_includes_exact_rollover_boundary():
    latched_at = datetime(2026, 9, 3, 20, tzinfo=UTC)
    rollover = datetime(2026, 9, 3, 21, tzinfo=UTC)
    mtm = {"id": 2, "ts": rollover.isoformat(), "source": "paper"}

    assert not _release_is_due(
        now=rollover - timedelta(microseconds=1), latched_at=latched_at,
        mtm={**mtm, "ts": (rollover - timedelta(microseconds=1)).isoformat()},
        before_id=1)
    assert _release_is_due(
        now=rollover, latched_at=latched_at, mtm=mtm, before_id=1)


def test_release_requires_fresh_same_tick_paper_mtm():
    now = datetime(2026, 9, 6, 21, tzinfo=UTC)
    latched = datetime(2026, 9, 4, 20, tzinfo=UTC)
    for mtm in (None,
                {"id": 1, "ts": now.isoformat(), "source": "paper"},
                {"id": 2, "ts": now.isoformat(), "source": "stale"},
                {"id": 2, "ts": latched.isoformat(), "source": "paper"}):
        assert not _release_is_due(now=now, latched_at=latched, mtm=mtm,
                                   before_id=1)


def test_auto_release_rebases_without_cashflow_and_publishes_once(tmp_path):
    now = datetime(2026, 9, 6, 21, tzinfo=UTC)
    conn = connect(tmp_path / "local.db"); init_db(conn)
    mtm = _mtm(conn, now)
    clock = ReplayClock(now)
    state = _RecordingStateStore(tmp_path / "state.json", clock=clock)
    state.update(kill_switch_latched=True, transition_snapshot_id=mtm["id"])
    activity = _RecordingActivity(clock)

    assert _auto_release_kill_switch(
        conn, state=state, activity=activity, now=now, mtm=mtm,
        latched_at=datetime(2026, 9, 4, 20, tzinfo=UTC))
    rebased = snapshots.latest(conn)
    assert (rebased["equity"], rebased["hwm"], rebased["cashflow"],
            rebased["source"]) == (98_000.0, 98_000.0, 0.0,
                                    "replay_ks_rebase")
    assert state.load().kill_switch_latched is False
    assert [t["kind"] for t in state.kill_switch_transitions] == [
        "latched", "released"]
    event = _kill_switch_event(conn, state.kill_switch_transitions[-1],
                               "released", activity.entries)
    assert event["drawdown_pct"] == 0
    entry = activity.entries[-1]
    assert entry["kind"] == "replay_kill_switch_auto_release"
    for field in ("latched_at=", "released_at=", "hwm_before=",
                  "hwm_after=", "rebase_id="):
        assert field in entry["text"]


def test_transition_snapshot_callback_runs_before_state_save(tmp_path):
    now = datetime(2026, 9, 6, 21, tzinfo=UTC)
    state = _RecordingStateStore(
        tmp_path / "state.json", clock=ReplayClock(now),
        snapshot_id_fn=lambda: (_ for _ in ()).throw(RuntimeError("snapshot")))
    with pytest.raises(RuntimeError, match="snapshot"):
        state.update(kill_switch_latched=True)
    assert state.load().kill_switch_latched is False


def test_future_mtm_is_rejected_without_transaction(tmp_path):
    now = datetime(2026, 9, 6, 21, tzinfo=UTC)
    conn = connect(tmp_path / "local.db"); init_db(conn)
    clock = ReplayClock(now); state = _RecordingStateStore(tmp_path/"s", clock=clock)
    state.update(kill_switch_latched=True)
    activity = _RecordingActivity(clock)
    mtm = _mtm(conn, now)
    mtm["ts"] = now.replace(hour=22).isoformat()
    assert not _auto_release_kill_switch(conn, state=state, activity=activity,
        now=now, mtm=mtm, latched_at=now.replace(day=4))
    assert state.load().kill_switch_latched and not conn.in_transaction


def test_event_uses_transition_snapshot_id_not_later_row(tmp_path):
    now = datetime(2026, 9, 6, 21, tzinfo=UTC)
    conn = connect(tmp_path/"x.db"); init_db(conn)
    first = _mtm(conn, now, equity=80_000, hwm=100_000)
    transition = {"ts": now.isoformat(), "kind": "latched", "reason": "x",
                  "snapshot_id": first["id"]}
    _mtm(conn, now, equity=90_000, hwm=100_000)
    assert _kill_switch_event(conn, transition, "latched")["drawdown_pct"] == 20


def test_event_with_explicit_null_snapshot_has_null_drawdown(tmp_path):
    now = datetime(2026, 9, 6, 21, tzinfo=UTC)
    conn = connect(tmp_path / "x.db"); init_db(conn)
    _mtm(conn, now, equity=80_000, hwm=100_000)
    transition = {"ts": now.isoformat(), "kind": "latched", "reason": "x",
                  "snapshot_id": None}

    event = _kill_switch_event(conn, transition, "latched")

    assert event["drawdown_pct"] is None


class _FaultConn:
    def __init__(self, conn, fault): self.conn, self.fault = conn, fault
    def execute(self, sql, params=()):
        if self.fault == "insert" and sql.startswith("INSERT INTO account_snapshots"):
            raise RuntimeError("insert")
        if self.fault == "post" and sql.startswith("INSERT INTO account_snapshots"):
            params = (*params[:3], params[3] + 1, *params[4:])
        return self.conn.execute(sql, params)
    def commit(self):
        if self.fault == "commit": raise RuntimeError("commit")
        return self.conn.commit()
    def rollback(self): return self.conn.rollback()
    @property
    def in_transaction(self): return self.conn.in_transaction


@pytest.mark.parametrize("fault", ["insert", "post"])
def test_insert_or_postcondition_failure_defers_and_rolls_back(tmp_path, fault):
    now=datetime(2026,9,6,21,tzinfo=UTC); raw=connect(tmp_path/"x.db"); init_db(raw)
    mtm=_mtm(raw,now); clock=ReplayClock(now)
    state=_RecordingStateStore(tmp_path/"s",clock=clock); state.update(kill_switch_latched=True)
    activity=_RecordingActivity(clock); before=raw.execute("SELECT count(*) FROM account_snapshots").fetchone()[0]
    assert not _auto_release_kill_switch(_FaultConn(raw,fault),state=state,
        activity=activity,now=now,mtm=mtm,latched_at=now.replace(day=4))
    assert raw.execute("SELECT count(*) FROM account_snapshots").fetchone()[0] == before
    assert state.load().kill_switch_latched


def test_state_update_failure_rolls_back_rebase(tmp_path):
    now=datetime(2026,9,6,21,tzinfo=UTC); conn=connect(tmp_path/"x.db"); init_db(conn)
    mtm=_mtm(conn,now); clock=ReplayClock(now); state=_RecordingStateStore(tmp_path/"s",clock=clock)
    state.update(kill_switch_latched=True); activity=_RecordingActivity(clock)
    original=state.update
    state.update=lambda **kw: (_ for _ in ()).throw(RuntimeError("save")) if kw.get("kill_switch_latched") is False else original(**kw)
    assert not _auto_release_kill_switch(conn,state=state,activity=activity,now=now,
        mtm=mtm,latched_at=now.replace(day=4))
    assert snapshots.latest(conn)["source"] == "paper" and state.load().kill_switch_latched


def test_commit_failure_compensates_without_publishing(tmp_path):
    now=datetime(2026,9,6,21,tzinfo=UTC); raw=connect(tmp_path/"x.db"); init_db(raw)
    mtm=_mtm(raw,now); clock=ReplayClock(now); state=_RecordingStateStore(tmp_path/"s",clock=clock)
    state.update(kill_switch_latched=True); before=len(state.kill_switch_transitions)
    assert not _auto_release_kill_switch(_FaultConn(raw,"commit"),state=state,
        activity=_RecordingActivity(clock),now=now,mtm=mtm,latched_at=now.replace(day=4))
    assert state.load().kill_switch_latched
    assert len(state.kill_switch_transitions)==before
    assert snapshots.latest(raw)["source"] == "paper"


def test_executor_transition_rejects_stale_or_non_paper_snapshot():
    now=datetime(2026,9,6,21,tzinfo=UTC)
    assert _executor_transition_snapshot_id(
        {"id": 3, "ts": now.isoformat(), "source": "paper"}, now) == 3
    assert _executor_transition_snapshot_id(
        {"id": 3, "ts": now.replace(minute=59).isoformat(), "source": "paper"}, now) is None
    assert _executor_transition_snapshot_id(
        {"id": 3, "ts": now.isoformat(), "source": "replay_ks_rebase"}, now) is None


def test_executor_transition_accepts_missing_latest_snapshot():
    now = datetime(2026, 9, 6, 21, tzinfo=UTC)

    assert _executor_transition_snapshot_id(None, now) is None


def test_repeated_latched_value_does_not_publish_duplicate_transition(tmp_path):
    now = datetime(2026, 9, 6, 21, tzinfo=UTC)
    state = _RecordingStateStore(tmp_path / "state.json", clock=ReplayClock(now))

    state.update(kill_switch_latched=True)
    state.update(kill_switch_latched=True)

    assert [transition["kind"] for transition in state.kill_switch_transitions] == [
        "latched"]


def test_compensation_failure_aborts_replay_operation(tmp_path):
    now=datetime(2026,9,6,21,tzinfo=UTC); raw=connect(tmp_path/"x.db"); init_db(raw)
    mtm=_mtm(raw,now); clock=ReplayClock(now); state=_RecordingStateStore(tmp_path/"s",clock=clock)
    state.update(kill_switch_latched=True); original=state.update
    def failing_update(**kw):
        if kw.get("kill_switch_latched") is True: raise RuntimeError("compensation")
        return original(**kw)
    state.update=failing_update
    with pytest.raises(RuntimeError, match="compensation failed"):
        _auto_release_kill_switch(_FaultConn(raw,"commit"),state=state,
            activity=_RecordingActivity(clock),now=now,mtm=mtm,
            latched_at=now.replace(day=4))
    assert not raw.in_transaction and snapshots.latest(raw)["source"] == "paper"
