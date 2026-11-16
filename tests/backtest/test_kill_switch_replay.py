from datetime import datetime, timedelta, timezone

import json
from pathlib import Path

import pytest

import agentic_fx.backtest.runner as runner_module
import agentic_fx.core.accounting as accounting
import agentic_fx.core.scheduler as scheduler_module
import agentic_fx.store.state as state_module
from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.backtest.replay import ReplayClock
from agentic_fx.backtest.runner import (
    _RecordingActivity, _RecordingStateStore, _auto_release_kill_switch,
    _kill_switch_event, _release_is_due,
    _executor_transition_snapshot_id, run_replay,
)
from agentic_fx.store import ohlcv, snapshots
from agentic_fx.store.db import connect, init_db


from tests.backtest.factories import SETTINGS, _conn, _row_at

UTC = timezone.utc
DATASET_1M = HistoryDataset("dukascopy", "1m")
THU = datetime(2026, 7, 23, 21, 58, tzinfo=UTC)
SUN_OPEN = datetime(2026, 7, 26, 21, 0, tzinfo=UTC)

SAFE_OPEN = {
    "action": "open", "pair": "USDJPY", "direction": "long",
    "entry_type": "market", "horizon": "swing",
    "stop_loss": 147.00, "take_profit": 151.50,
    "reasoning": "weekend-release-pin",
}


def test_replay_database_initialization_includes_empty_manual_rebase_ledger(tmp_path):
    conn = connect(tmp_path / "replay-init.db")
    init_db(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM kill_switch_hwm_rebases").fetchone()[0] == 0


def _capture_replay_observers(monkeypatch):
    observed = {"states": [], "activities": [], "connections": [],
                "release_daily_equity": []}
    real_state_init = runner_module._RecordingStateStore.__init__
    real_activity_init = runner_module._RecordingActivity.__init__
    real_connect = runner_module.connect
    real_release = runner_module._auto_release_kill_switch

    def state_init(instance, *args, **kwargs):
        real_state_init(instance, *args, **kwargs)
        observed["states"].append(instance)

    def activity_init(instance, *args, **kwargs):
        real_activity_init(instance, *args, **kwargs)
        observed["activities"].append(instance)

    def captured_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        observed["connections"].append(conn)
        return conn

    def release_with_daily_equity(conn, **kwargs):
        now = kwargs["now"]
        before = accounting.daily_start_equity(conn, now)
        released = real_release(conn, **kwargs)
        after = accounting.daily_start_equity(conn, now)
        observed["release_daily_equity"].append((before, after))
        return released

    monkeypatch.setattr(runner_module._RecordingStateStore, "__init__", state_init)
    monkeypatch.setattr(runner_module._RecordingActivity, "__init__", activity_init)
    monkeypatch.setattr(runner_module, "connect", captured_connect)
    monkeypatch.setattr(runner_module, "_auto_release_kill_switch",
                        release_with_daily_equity)
    return observed


def _weekend_replay(tmp_path, monkeypatch, *, stale_release_tick=False):
    """Thursday gap latch through Sunday reopen, using the real replay loop."""
    hist = _conn(tmp_path)
    rows = [
        _row_at(THU, o=148.5, h=148.6, l=148.4, c=148.5),
        _row_at(THU + timedelta(minutes=1), o=148.5, h=148.6,
                l=148.4, c=148.5),
        _row_at(THU + timedelta(minutes=2), o=147.5, h=147.55,
                l=147.4, c=147.5),
        _row_at(THU + timedelta(minutes=3), o=147.5, h=147.55,
                l=147.4, c=147.5),
        _row_at(THU + timedelta(minutes=4), o=147.5, h=147.55,
                l=147.4, c=147.5),
        _row_at(datetime(2026, 7, 25, 20, 59, tzinfo=UTC), o=147.5,
                h=147.55, l=147.45, c=147.5),
        _row_at(datetime(2026, 7, 25, 21, 0, tzinfo=UTC), o=147.5,
                h=147.55, l=147.45, c=147.5),
    ]
    if not stale_release_tick:
        rows.append(_row_at(SUN_OPEN - timedelta(minutes=1), o=147.8,
                            h=147.85, l=147.75, c=147.8))
    rows.extend([
        _row_at(SUN_OPEN, o=147.8, h=147.85, l=147.75, c=147.8),
        _row_at(SUN_OPEN + timedelta(minutes=1), o=147.8, h=147.85,
                l=147.75, c=147.8),
    ])
    ohlcv.import_history_bars(hist, rows, source="dukascopy")
    observed = _capture_replay_observers(monkeypatch)
    calls = []

    def source(bar):
        calls.append(bar.ts)
        return dict(SAFE_OPEN)

    settings = SETTINGS.model_copy(update={
        "risk": SETTINGS.risk.model_copy(update={"drawdown_kill_pct": 0.05})})
    result = run_replay(
        settings, symbol="USDJPY", dataset=DATASET_1M,
        start=THU, end=SUN_OPEN + timedelta(minutes=3),
        intent_source=source, eval_timeframe="1m", history_conn=hist)
    observed["calls"] = calls
    return result, observed


def test_weekend_replay_pins_release_mtm_order_and_post_release_open(
        tmp_path, monkeypatch):
    """K1/K3/K7/K9/K12: replay-visible weekend release contract."""
    result, observed = _weekend_replay(tmp_path, monkeypatch)
    events = result.kill_switch_events
    assert [(event["kind"], event["ts"]) for event in events] == [
        ("latched", (THU + timedelta(minutes=3)).isoformat()),
        ("released", SUN_OPEN.isoformat()),
    ]
    assert result.kill_switch_latches == 1

    rebase = [row for row in result.snapshots
              if row["source"] == "replay_ks_rebase"]
    assert len(rebase) == 1
    rebase = rebase[0]
    same_tick_mtm = [row for row in result.snapshots
                     if row["ts"] == SUN_OPEN.isoformat()
                     and row["source"] == "paper"]
    assert len(same_tick_mtm) == 1
    assert same_tick_mtm[0]["id"] < rebase["id"]
    assert rebase["equity"] == same_tick_mtm[0]["equity"]
    assert rebase["cashflow"] == 0.0

    reopened = [order for order in result.orders
                if order["created_at"] > SUN_OPEN.isoformat()]
    assert len(reopened) == 1
    assert reopened[0]["created_at"] == (
        SUN_OPEN + timedelta(minutes=1)).isoformat()
    assert len(observed["release_daily_equity"]) == 1
    daily_before, daily_after = observed["release_daily_equity"][0]
    assert daily_before is not None
    assert daily_after == daily_before
    activity = observed["activities"][0].entries
    post_release_rejections = [entry for entry in activity
                               if entry["kind"] == "gate_rejected"
                               and entry["ts"] > SUN_OPEN.isoformat()]
    assert all("daily loss" not in entry["text"]
               for entry in post_release_rejections)


def test_weekend_replay_stale_release_tick_defers_until_healthy_mtm(
        tmp_path, monkeypatch):
    """K10: no bar means no release; the next healthy tick releases."""
    result, observed = _weekend_replay(
        tmp_path, monkeypatch, stale_release_tick=True)
    released = [event for event in result.kill_switch_events
                if event["kind"] == "released"]
    assert [event["ts"] for event in released] == [
        (SUN_OPEN + timedelta(minutes=1)).isoformat()]
    assert not [row for row in result.snapshots
                if row["source"] == "replay_ks_rebase"
                and row["ts"] == SUN_OPEN.isoformat()]
    assert [row["ts"] for row in result.snapshots
            if row["source"] == "replay_ks_rebase"] == [
                (SUN_OPEN + timedelta(minutes=1)).isoformat()]
    assert len(observed["release_daily_equity"]) == 1


def _mtm(conn, ts, *, equity=98_000.0, hwm=100_000.0):
    ident = snapshots.add(conn, ts=ts, balance=98_000.0, equity=equity,
                          hwm=hwm, cashflow=0, source="paper")
    return {**snapshots.latest(conn), "id": ident}


@pytest.mark.parametrize(("now", "expected"), [
    (datetime(2026, 9, 5, 21, tzinfo=UTC), False),
    (datetime(2026, 9, 6, 21, tzinfo=UTC), True),
    (datetime(2025, 12, 25, 21, 59, tzinfo=UTC), False),
    (datetime(2025, 12, 25, 22, tzinfo=UTC), True),
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
    state.update(kill_switch_latched=True); before=list(state.kill_switch_transitions)
    assert not _auto_release_kill_switch(_FaultConn(raw,"commit"),state=state,
        activity=_RecordingActivity(clock),now=now,mtm=mtm,latched_at=now.replace(day=4))
    assert state.load().kill_switch_latched
    assert state.kill_switch_transitions == before
    assert snapshots.latest(raw)["source"] == "paper"
    assert not raw.in_transaction
    assert not raw.execute(
        "SELECT 1 FROM account_snapshots WHERE source='replay_ks_rebase'"
    ).fetchall()


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


def test_compensation_failure_aborts_replay_operation(tmp_path, monkeypatch):
    now=datetime(2026,9,6,21,tzinfo=UTC); raw=connect(tmp_path/"x.db"); init_db(raw)
    mtm=_mtm(raw,now); clock=ReplayClock(now); state=_RecordingStateStore(tmp_path/"s",clock=clock)
    state.update(kill_switch_latched=True)
    before = list(state.kill_switch_transitions)
    real_replace = state_module.os.replace
    def fail_compensation_replace(src, dst):
        payload = json.loads(Path(src).read_text(encoding="utf-8"))
        if payload["kill_switch_latched"] is True:
            raise OSError("compensation replace")
        return real_replace(src, dst)
    monkeypatch.setattr(state_module.os, "replace", fail_compensation_replace)
    with pytest.raises(RuntimeError, match="compensation failed"):
        _auto_release_kill_switch(_FaultConn(raw,"commit"),state=state,
            activity=_RecordingActivity(clock),now=now,mtm=mtm,
            latched_at=now.replace(day=4))
    assert not raw.in_transaction
    assert snapshots.latest(raw)["source"] == "paper"
    assert not raw.execute(
        "SELECT 1 FROM account_snapshots WHERE source='replay_ks_rebase'"
    ).fetchall()
    assert state.kill_switch_transitions == before


def test_scheduler_snapshot_failure_executor_latch_has_null_drawdown(
        tmp_path, monkeypatch):
    """K19: a failed Scheduler MTM cannot attach the preceding paper row."""
    hist = _conn(tmp_path)
    start = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    rows = [_row_at(start + timedelta(minutes=i), o=148.5, h=148.6,
                    l=148.4, c=148.5) for i in range(3)]
    ohlcv.import_history_bars(hist, rows, source="dukascopy")
    real_record = scheduler_module.accounting.record_snapshot
    failed_tick = start + timedelta(minutes=2)

    def injected_record(conn, *, now, balance, equity, **kwargs):
        if now == failed_tick:
            raise RuntimeError("injected scheduler snapshot failure")
        if now == start + timedelta(minutes=1):
            balance = equity = 970_000.0
        return real_record(conn, now=now, balance=balance, equity=equity,
                           **kwargs)

    monkeypatch.setattr(scheduler_module.accounting, "record_snapshot",
                        injected_record)
    result = run_replay(
        SETTINGS, symbol="USDJPY", dataset=DATASET_1M,
        start=start, end=start + timedelta(minutes=3),
        intent_source=lambda bar: dict(SAFE_OPEN), eval_timeframe="1m",
        history_conn=hist)

    latched = [event for event in result.kill_switch_events
               if event["kind"] == "latched"]
    assert len(latched) == 1
    assert latched[0]["ts"] == failed_tick.isoformat()
    assert latched[0]["drawdown_pct"] is None
    assert not [row for row in result.snapshots
                if row["ts"] == failed_tick.isoformat()
                and row["source"] == "paper"]


# --- ラッチ中・DD 閾値以上の約定停止 ---

START = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
LIMIT_BELOW = {
    "action": "open", "pair": "USDJPY", "direction": "long",
    "entry_type": "limit", "horizon": "swing", "limit_price": 148.0,
    "expires_in": "20h", "stop_loss": 147.0, "take_profit": 151.5,
    "reasoning": "pending-fill-stop",
}
MARKET_OPEN = {
    "action": "open", "pair": "USDJPY", "direction": "long",
    "entry_type": "market", "horizon": "swing",
    "stop_loss": 147.0, "take_profit": 151.5,
    "reasoning": "pending-fill-stop",
}


def _drawdown_replay(tmp_path, intents, *, drop, threshold=0.05, minutes=14):
    """分足 flat → drop 以降で下げる。intents は呼び出し番号 → intent。"""
    hist = _conn(tmp_path)
    rows = []
    for i in range(minutes):
        px = 148.5 if i < drop else 147.5
        rows.append(_row_at(START + timedelta(minutes=i), o=px, h=px + 0.05,
                            l=px - 0.05, c=px))
    ohlcv.import_history_bars(hist, rows, source="dukascopy")
    calls = []

    def source(bar):
        calls.append(bar.ts)
        return intents.get(len(calls))

    settings = SETTINGS.model_copy(update={
        "risk": SETTINGS.risk.model_copy(update={
            "drawdown_kill_pct": threshold, "max_positions": 5})})
    result = run_replay(
        settings, symbol="USDJPY", dataset=DATASET_1M, start=START,
        end=START + timedelta(minutes=minutes), intent_source=source,
        eval_timeframe="1m", history_conn=hist)
    return result


def _summary(result):
    return (
        [(o["id"], o["status"], o["close_reason"], o["avg_fill_price"])
         for o in result.orders],
        [(e["kind"], e["ts"]) for e in result.kill_switch_events],
        result.kill_switch_latches, result.equity_curve)


def test_replay_drawdown_threshold_stops_a_reserved_limit_without_a_latch(
        tmp_path):
    intents = {1: dict(MARKET_OPEN), 2: dict(LIMIT_BELOW)}
    (tmp_path / "a").mkdir()
    stopped = _drawdown_replay(tmp_path / "a", intents, drop=7)
    assert stopped.kill_switch_latches == 0
    limit = [o for o in stopped.orders if o["entry_type"] == "limit"]
    assert len(limit) == 1
    assert limit[0]["status"] == "pending_fill"
    assert limit[0]["close_reason"] is None
    (tmp_path / "b").mkdir()
    free = _drawdown_replay(tmp_path / "b", intents, drop=7, threshold=50.0)
    assert free.kill_switch_latches == 0
    free_limit = [o for o in free.orders if o["entry_type"] == "limit"]
    assert free_limit[0]["status"] == "open"
    assert free_limit[0]["avg_fill_price"] == pytest.approx(147.505)


def test_replay_latch_cancels_the_reserved_limit_with_the_kill_switch_reason(
        tmp_path):
    intents = {1: dict(MARKET_OPEN), 2: dict(LIMIT_BELOW), 10: dict(MARKET_OPEN)}
    result = _drawdown_replay(tmp_path, intents, drop=7)
    assert result.kill_switch_latches == 1
    limit = [o for o in result.orders if o["entry_type"] == "limit"]
    assert [(o["status"], o["close_reason"]) for o in limit] == [
        ("cancelled", "kill_switch")]
    latched = [e for e in result.kill_switch_events if e["kind"] == "latched"]
    assert len(latched) == 1


def test_replay_with_the_same_input_is_deterministic(tmp_path):
    intents = {1: dict(MARKET_OPEN), 2: dict(LIMIT_BELOW), 10: dict(MARKET_OPEN)}
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    first = _drawdown_replay(tmp_path / "x", intents, drop=7)
    second = _drawdown_replay(tmp_path / "y", intents, drop=7)
    assert _summary(first) == _summary(second)
    assert first.snapshots == second.snapshots


def test_replay_without_latch_or_drawdown_is_unchanged_by_the_new_stop(
        tmp_path):
    intents = {1: dict(MARKET_OPEN), 2: dict(LIMIT_BELOW)}
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    base = _drawdown_replay(tmp_path / "x", intents, drop=99, threshold=2.0)
    wide = _drawdown_replay(tmp_path / "y", intents, drop=99, threshold=50.0)
    assert base.kill_switch_latches == 0
    assert [e["drawdown_pct"] for e in base.kill_switch_events] == []
    assert _summary(base) == _summary(wide)
    assert [(o["status"], o["close_reason"]) for o in base.orders
            if o["entry_type"] == "limit"] == [("pending_fill", None)]


def test_weekend_replay_cancels_the_limit_at_the_latch_and_it_never_fills_later(
        tmp_path, monkeypatch):
    hist = _conn(tmp_path)
    flat = dict(o=148.5, h=148.6, l=148.4, c=148.5)
    low = dict(o=147.5, h=147.55, l=147.4, c=147.5)
    rows = [_row_at(THU, **flat),
            _row_at(THU + timedelta(minutes=1), **flat),
            _row_at(THU + timedelta(minutes=2), **flat),
            _row_at(THU + timedelta(minutes=3), **flat),
            _row_at(THU + timedelta(minutes=4), **low),
            _row_at(THU + timedelta(minutes=5), **low),
            _row_at(THU + timedelta(minutes=6), **low),
            _row_at(datetime(2026, 7, 25, 20, 59, tzinfo=UTC), **low),
            _row_at(datetime(2026, 7, 25, 21, 0, tzinfo=UTC), **low),
            _row_at(SUN_OPEN - timedelta(minutes=1), o=147.8, h=147.85,
                    l=147.75, c=147.8),
            _row_at(SUN_OPEN, o=147.8, h=147.85, l=147.75, c=147.8),
            _row_at(SUN_OPEN + timedelta(minutes=1), o=147.8, h=147.85,
                    l=147.75, c=147.8)]
    ohlcv.import_history_bars(hist, rows, source="dukascopy")
    sequence = {1: dict(SAFE_OPEN), 2: {
        "action": "open", "pair": "USDJPY", "direction": "long",
        "entry_type": "limit", "horizon": "swing", "limit_price": 147.9,
        "expires_in": "20h", "stop_loss": 147.0, "take_profit": 151.5,
        "reasoning": "stale-limit"}, 4: dict(MARKET_OPEN)}
    calls = []

    def source(bar):
        calls.append(bar.ts)
        return sequence.get(len(calls))

    settings = SETTINGS.model_copy(update={
        "risk": SETTINGS.risk.model_copy(update={
            "drawdown_kill_pct": 0.05, "max_positions": 5})})
    result = run_replay(
        settings, symbol="USDJPY", dataset=DATASET_1M, start=THU,
        end=SUN_OPEN + timedelta(minutes=3), intent_source=source,
        eval_timeframe="1m", history_conn=hist)
    limits = [o for o in result.orders if o["entry_type"] == "limit"]
    assert [(o["status"], o["close_reason"]) for o in limits] == [
        ("cancelled", "kill_switch")]
    kinds = [e["kind"] for e in result.kill_switch_events]
    assert kinds[:2] == ["latched", "released"]
    assert result.kill_switch_latches >= 1
    assert not [o for o in limits if o["avg_fill_price"] is not None]
