"""What keeps working, and what stops, once a re-evaluation request is rejected.

A rejected request stops the deployment like ``afx plugin disable``. These tests run the
real trading entrances (signal producer, trade loop OPEN gate, deployed-set refresh,
shadow observation worker, executor protection ticks) against the same database and
plugins directory the rejection wrote to.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from agentic_fx.core.contracts import Origin, TradeIntent
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.core.supervisor import SubmitResult
from agentic_fx.core.contracts import Bar
from agentic_fx.plugin import deploy_disable, deploy_ops, deploy_reconcile
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.resolve import ResolvedIndicatorSet
from agentic_fx.plugin.signal_producer import SignalProducer
from agentic_fx.runners.base import MissionResult
from agentic_fx.service import DeployedStrategies
from agentic_fx.store import missions, orders
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store import signal_decisions as sd
from agentic_fx.store import signal_shadow as ss
from agentic_fx.store import signals as signals_store
from agentic_fx.store.state import StateStore
from agentic_fx.tools import plugin_loader
from tests.core.test_econ_shadow_worker import (
    LATE, SEED_START, UTC, _Clock, _seed, _World as ShadowWorld, _worker)
from tests.loops.test_trade_loop import NOW as LOOP_NOW
from tests.loops.test_trade_loop import _loop
from tests.plugin._conc_support import FIRST_TICK, NEXT_TICK, Activity as ProducerActivity, Sandbox
from tests.plugin.conftest import SETTINGS
from tests.plugin.deploy_world import at
from tests.plugin.test_reevaluation_decision import NAME, Case
from tests.plugin.test_signal_producer import SOURCE, H, _open_result, _seed_flat

ACCEPT = {"decision": "accept", "reject_code": None}


@pytest.fixture
def case(tmp_path):
    made = Case(tmp_path)
    _seed_flat(made.conn, H - timedelta(hours=3), 8 * 60 + 1)
    return made


def _meta(case: Case) -> PluginMeta:
    return PluginMeta(
        name=NAME, kind="strategy", path=case.version, params={}, timeframe="1h",
        pairs=("USDJPY",), max_bars=2, content_hash=case.content,
        artifact_hash=case.artifact)


def _produce(case: Case, now, activity) -> int:
    meta = _meta(case)
    resolved = {(meta.name, meta.content_hash): ResolvedIndicatorSet(
        inventory_root=case.root, items=(), all_pinned=False)}
    return SignalProducer().evaluate_due_plugins(
        case.conn, plugins=[meta], now=now, source=SOURCE,
        sandbox_run=Sandbox(_open_result()), settings=SETTINGS,
        resolved_by_identity=resolved, activity=activity, plugins_root=case.root)


def _fail_unlink(monkeypatch):
    real_unlink = os.unlink

    def failing(path, *args, **kwargs):
        if os.path.basename(str(path)) == NAME:
            raise OSError("denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", failing)
    return real_unlink


# ------------------------------------------------------------------ signal injection

@pytest.mark.parametrize(
    ("unlink_fails", "reason"),
    [(False, "strategy_not_live"), (True, "strategy_not_approved")],
    ids=["symlink_removed", "symlink_still_present"])
def test_the_producer_inserts_no_signal_once_the_rejection_is_committed(
        case, monkeypatch, unlink_fails, reason):
    first = ProducerActivity()
    assert _produce(case, FIRST_TICK, first) == 1
    assert first.events == []
    if unlink_fails:
        _fail_unlink(monkeypatch)

    case.reject()

    # The symlink may or may not be gone; either way the committed decision stops new signals.
    assert (case.root / NAME).is_symlink() is unlink_fails
    after = ProducerActivity()
    assert _produce(case, NEXT_TICK, after) == 0
    assert after.events == [("plugin_skipped_not_live", f"name={NAME} reason={reason}")]
    assert case.conn.execute(
        "SELECT COUNT(*) FROM signals WHERE status='pending'").fetchone()[0] == 0


# ------------------------------------------------------------------ claimed OPEN

def test_a_signal_claimed_before_the_rejection_is_dropped_at_the_open_gate(case):
    conn, loop, runner, _ = _loop(
        case.world.root.parent, [MissionResult("completed", ACCEPT, [])], conn=case.conn)
    signal_id = signals_store.add(
        conn, plugin=NAME, content_hash=case.content, pair="USDJPY", timeframe="1h",
        bar_ts=LOOP_NOW.isoformat(), kind="strategy",
        payload={"action": "open", "pair": "USDJPY", "direction": "long",
                 "entry_type": "market", "horizon": "day", "stop_loss": 147.0,
                 "take_profit": 151.5}, now=LOOP_NOW, contract_version="executable_v1")
    loop._plugins_root = case.root
    run = runner.run

    def reject_during_the_mission(mission):
        case.reject()
        return run(mission)

    runner.run = reject_during_the_mission

    out = loop.run_once("signal")

    assert out is None
    row = conn.execute(
        "SELECT status, abandon_reason FROM signals WHERE id=?", (signal_id,)).fetchone()
    assert tuple(row) == ("abandoned", "source_inactive")
    assert conn.execute("SELECT COUNT(*) FROM signal_decisions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trade_intents").fetchone()[0] == 0
    assert case.decision() == (case.approval_id, "rejected")


# ------------------------------------------------------------------ deployed set refresh

def _deployed(case: Case) -> DeployedStrategies:
    """The same loaders the service wires into the deployed-set refresh."""
    def load():
        return list(plugin_loader.approved_plugins(
            case.conn, case.root, settings=SETTINGS).inventory.metas)

    def version():
        row = case.conn.execute(
            "SELECT COALESCE(MAX(op_id), 0), COALESCE(MAX(updated_at), '') "
            "FROM plugin_deploy_ops").fetchone()
        return (row[0], row[1])

    return DeployedStrategies(load(), load_fn=load, version_fn=version)


def test_the_next_tick_refresh_drops_the_rejected_strategy_from_the_deployed_set(case):
    deployed = _deployed(case)
    assert [m.name for m in deployed.current()] == [NAME]
    assert deployed.refresh(at(45)) is True  # first marker
    assert deployed.refresh(at(46)) is False  # nothing changed

    case.reject()

    assert deployed.refresh(at(51)) is True
    assert deployed.current() == []
    assert deployed.refresh(at(52)) is False


# ------------------------------------------------------------------ shadow observation

def _shadow_for_the_strategy(db, case: Case, *, decided=None) -> int:
    decided = decided or (SEED_START + timedelta(hours=12, seconds=30))
    payload = {"action": "open", "direction": "long", "entry_type": "market",
               "limit_price": None, "stop_loss": 149.0, "take_profit": None}
    mission = db.execute(
        "INSERT INTO missions (loop,runner,model,status,started_at) "
        "VALUES ('trade','local','m','running',?)", (decided.isoformat(),)).lastrowid
    signal = db.execute(
        "INSERT INTO signals (plugin,content_hash,pair,timeframe,bar_ts,kind,"
        "payload_json,status,claimed_by_mission_id,claimed_at,created_at,"
        "contract_version) VALUES (?,?,'USDJPY','15m',?,'strategy',?,"
        "'claimed',?,?,?,'executable_v1')",
        (NAME, case.content, SEED_START.isoformat(), json.dumps(payload), mission,
         decided.isoformat(), decided.isoformat())).lastrowid
    db.commit()
    spread = ss.ShadowSpread(pip_size=0.01, spread_pips=2.0, source="assumed")
    return sd.commit_reject(
        db, mission_id=mission, signal_id=signal, actor="system",
        reject_code="econ_window", spread=spread, output=None, transcript=[],
        now=decided).shadow_id


def test_a_shadow_in_progress_keeps_being_observed_after_the_rejection(case):
    watcher = ShadowWorld(case.world.db_path)
    try:
        _seed(watcher.db)
        shadow = _shadow_for_the_strategy(watcher.db, case)
        assert ss.get(watcher.db, shadow)["state"] == "pending_entry"

        case.reject()

        assert ss.get(watcher.db, shadow)["state"] == "pending_entry"
        candidates = ss.unresolved_candidates(watcher.db, decided_before=LATE, limit=10)
        assert [row["id"] for row in candidates] == [shadow]
        worker = _worker(watcher, _Clock(LATE))
        worker.run_cycle()
        row = ss.get(watcher.db, shadow)
        assert row["state"] == "resolved"
        assert row["result_code"] is not None
    finally:
        watcher.close()


# ------------------------------------------------------------------ open positions

def _scheduler_and_position(case: Case, tmp_path: Path):
    conn, loop, _runner, _ = _loop(tmp_path, [MissionResult("completed", ACCEPT, [])],
                                   conn=case.conn)
    bars: dict = {}
    scheduler = Scheduler(
        conn=conn, executor=loop.executor, settings=SETTINGS,
        state_store=StateStore(tmp_path / "scheduler.json"), activity=loop.activity,
        bars_fn=lambda pair: bars.get(pair),
        on_trade_mission=lambda reason, **_: SubmitResult.rejected(
            "shutdown", checked_at=0.0),
        on_news_cycle=lambda: None, on_econ_cycle=lambda: None)
    intent = TradeIntent.from_llm_dict(
        {"action": "open", "pair": "USDJPY", "direction": "long", "entry_type": "limit",
         "horizon": "day", "limit_price": 148.20, "expires_in": "6h",
         "stop_loss": 147.80, "take_profit": 149.00, "confidence": 0.8,
         "reasoning": "held across the rejection"}, origin=Origin.SCHEDULER)
    mission = missions.start(conn, "trade", "local", "manual", LOOP_NOW)
    placed = loop.executor.handle_intent(intent, mission)
    order_id = placed["order_id"]
    bars["USDJPY"] = Bar("USDJPY", "1m", LOOP_NOW, 148.30, 148.32, 148.18, 148.25, 100)
    scheduler.tick(LOOP_NOW + timedelta(minutes=1))
    assert orders.get(conn, order_id)["status"] == "open"
    return conn, scheduler, bars, order_id


def test_an_open_position_keeps_its_protection_and_is_not_closed_by_the_rejection(
        case, tmp_path):
    conn, scheduler, bars, order_id = _scheduler_and_position(case, tmp_path)
    before = dict(orders.get(conn, order_id))

    case.reject()

    # Nothing closed the position, and its stop and target are unchanged.
    after = dict(orders.get(conn, order_id))
    assert after["status"] == "open"
    assert (after["stop_loss"], after["take_profit"]) == (
        before["stop_loss"], before["take_profit"])
    bars["USDJPY"] = Bar("USDJPY", "1m", LOOP_NOW + timedelta(minutes=1),
                         148.40, 148.60, 148.30, 148.50, 100)
    scheduler.tick(LOOP_NOW + timedelta(minutes=2))
    assert orders.get(conn, order_id)["status"] == "open"

    # The protective stop still works.
    bars["USDJPY"] = Bar("USDJPY", "1m", LOOP_NOW + timedelta(minutes=2),
                         148.10, 148.20, 147.70, 147.75, 100)
    scheduler.tick(LOOP_NOW + timedelta(minutes=3))
    closed = orders.get(conn, order_id)
    assert (closed["status"], closed["close_reason"]) == ("closed", "sl")


# ------------------------------------------------------------------ interrupted notification

def _reject_and_crash_before_the_notification(case: Case, monkeypatch):
    real_notify = deploy_ops.try_notify
    monkeypatch.setattr(
        deploy_ops, "try_notify",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        case.reject()
    monkeypatch.setattr(deploy_ops, "try_notify", real_notify)
    op = deploy_store.list_by_name(case.conn, NAME)[0]
    assert (op["phase"], op["notify_attempted"]) == ("done", 0)
    assert case.decision() == (case.approval_id, "rejected")
    assert not os.path.lexists(case.root / NAME)
    return op["op_id"]


def test_disable_recovers_the_notification_a_rejection_did_not_reach(case, monkeypatch):
    op_id = _reject_and_crash_before_the_notification(case, monkeypatch)
    activity = ProducerActivity()

    again = deploy_disable.disable_plugin(
        case.conn, case.root, NAME, revoke=False, force=False, now=at(60),
        settings=SETTINGS, activity=activity)

    assert again.outcome == "already_disabled"
    assert again.notified_op_ids == (op_id,)
    assert activity.names() == ["plugin_disabled"]
    assert deploy_store.list_by_name(case.conn, NAME)[0]["notify_attempted"] == 1


def test_the_startup_reconcile_recovers_the_notification_a_rejection_did_not_reach(
        case, monkeypatch):
    op_id = _reject_and_crash_before_the_notification(case, monkeypatch)
    activity = ProducerActivity()

    results = deploy_reconcile.reconcile_deploy_ops(
        case.conn, case.root, now=at(60), settings=SETTINGS, activity=activity)

    assert [(r.op_id, r.outcome) for r in results] == [(op_id, "notified")]
    assert activity.names() == ["plugin_disabled"]
    again = deploy_reconcile.reconcile_deploy_ops(
        case.conn, case.root, now=at(61), settings=SETTINGS, activity=activity)
    assert again == []
    assert activity.names() == ["plugin_disabled"]
