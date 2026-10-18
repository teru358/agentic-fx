from __future__ import annotations

import os
import sqlite3

import pytest

from agentic_fx.plugin import (
    deploy_disable, deploy_ops, deploy_reconcile, deploy_retire, deploy_rollback, switch)
from agentic_fx.plugin.signal_producer import SignalProducer
from agentic_fx.plugin.source_state import source_state
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store import signals as signals_store
from tests.loops.test_trade_loop import NOW as LOOP_NOW
from tests.loops.test_trade_loop import _loop
from tests.loops.test_trade_loop_source_gate import OPEN
from tests.plugin._conc_support import FIRST_TICK, Activity as ProducerActivity, ConcWorld, Sandbox
from tests.plugin.conftest import SETTINGS
from tests.plugin.deploy_world import World, at
from tests.plugin.test_deploy_ops_common import _open_disable, _settings, _strategy_candidate
from tests.plugin.test_signal_producer import _open_result


NOW = at(183)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.mark.parametrize(
    ("phase", "reason", "blocking"),
    [
        ("intent", None, True),
        ("needs_retry", "syscall_failed", True),
        ("needs_retry", "verify_failed", False),
        ("done", None, False),
    ],
)
def test_open_consumers_share_the_deploy_operation_predicate(world, phase, reason, blocking):
    _a, _b, c = world.three_generations()
    op_id = world.intent(
        "sma", op_kind="rollback_symlink" if reason == "verify_failed" else "disable_symlink",
        from_ver=c, to_ver=_b if reason == "verify_failed" else None,
        phase=phase, fail_reason=reason,
    )
    row = deploy_store.get_by_id(world.conn, op_id)

    assert deploy_store.is_open(row) is blocking
    assert (deploy_store.get_open_by_name(world.conn, "sma") is not None) is blocking
    assert source_state(
        world.conn, world.root, name="sma", content_hash=world.content(c),
        artifact_hash=c.name, indicators=[],
    ) == ("open_deploy_op" if blocking else None)
    if blocking:
        with pytest.raises(deploy_ops.DeployOpBlockedError):
            deploy_ops.require_no_open_deploy_ops(world.conn, {"sma"}, operation="approval")
        with pytest.raises(sqlite3.IntegrityError):
            world.intent("sma", op_kind="disable_symlink", from_ver=c)
    else:
        deploy_ops.require_no_open_deploy_ops(world.conn, {"sma"}, operation="approval")


# ---- every consumer of the open predicate, on the same four rows ----

ROWS = [
    pytest.param("intent", None, True, id="intent"),
    pytest.param("needs_retry", "syscall_failed", True, id="syscall_failed"),
    pytest.param("needs_retry", "verify_failed", False, id="verify_failed"),
    pytest.param("done", None, False, id="done"),
]


def _row(world, name, live, other, phase, reason, *, kind="disable_symlink"):
    if reason == "verify_failed":
        kind = "rollback_symlink"
    return world.intent(
        name, op_kind=kind, from_ver=live,
        to_ver=other if kind == "rollback_symlink" else None,
        phase=phase, fail_reason=reason)


def _strategy_world(tmp_path, phase, reason):
    world = ConcWorld(tmp_path)
    other = world.version("s", "s2", kind="strategy")
    _row(world, "s", world.strategy_dir, other, phase, reason)
    return world


@pytest.mark.parametrize(("phase", "reason", "blocking"), ROWS)
def test_the_signal_producer_inserts_nothing_while_the_source_has_an_open_operation(
        tmp_path, phase, reason, blocking):
    world = _strategy_world(tmp_path, phase, reason)
    activity = ProducerActivity()

    inserted = world.evaluate(SignalProducer(), FIRST_TICK, Sandbox(_open_result()), activity)

    assert inserted == (0 if blocking else 1)
    assert world.total() == inserted
    if blocking:
        assert activity.events == [
            ("plugin_skipped_not_live", "name=s reason=open_deploy_op")]
    else:
        assert activity.events == []


@pytest.mark.parametrize(("phase", "reason", "blocking"), ROWS)
def test_the_open_gate_treats_a_source_with_an_open_operation_as_inactive(
        tmp_path, phase, reason, blocking):
    from agentic_fx.loops.trade_loop import signal_source_active

    world = _strategy_world(tmp_path, phase, reason)

    assert signal_source_active(
        world.conn, world.root, "s", world.content(world.strategy_dir), None) is (not blocking)


@pytest.mark.parametrize(("phase", "reason", "blocking"), ROWS)
def test_a_claimed_open_signal_is_rejected_by_the_commit_core_only_for_an_open_operation(
        tmp_path, phase, reason, blocking):
    world = World(tmp_path)
    strategy = world.version("strat", "s", kind="strategy")
    other = world.version("strat", "s2", kind="strategy")
    world.approve("strat", strategy)
    world.link("strat", strategy)
    _row(world, "strat", strategy, other, phase, reason)
    conn, loop, _runner, _ = _loop(
        tmp_path, [MissionResult("completed", OPEN, [])], conn=world.conn)
    signal_id = signals_store.add(
        conn, plugin="strat", content_hash=world.content(strategy), pair="USDJPY",
        timeframe="1h", bar_ts=LOOP_NOW.isoformat(), kind="strategy",
        payload={"direction": "long"}, now=LOOP_NOW)
    loop._plugins_root = world.root

    out = loop.run_once("signal")

    status = conn.execute("SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()[0]
    if blocking:
        assert out == {"result": "rejected", "order_id": None,
                       "reasons": ["signal_source_inactive"]}
        assert status == "abandoned"
    else:
        assert "signal_source_inactive" not in out.get("reasons", [])


# a rollback or retire attempt meets a disable row; a disable meets a rollback row
START_ATTEMPTS = ["rollback", "revoke", "disable", "retire"]


def _attempt(world, kind, versions):
    a, b, _c = versions
    if kind == "rollback":
        return deploy_rollback.rollback_plugin(
            world.conn, world.root, "sma", to=b.name, revoke=False, force=False, now=NOW,
            settings=SETTINGS, dry_run=True)
    if kind == "revoke":
        return deploy_disable.revoke_version(
            world.conn, world.root, "sma", content_hash=world.content(a), now=NOW)
    if kind == "disable":
        return deploy_disable.disable_plugin(
            world.conn, world.root, "sma", revoke=False, force=False, now=NOW,
            settings=SETTINGS, dry_run=True)
    return deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS, dry_run=True)


@pytest.mark.parametrize("attempt", START_ATTEMPTS)
@pytest.mark.parametrize(("phase", "reason", "blocking"), ROWS)
def test_another_operation_is_refused_only_while_a_different_kind_is_open(
        tmp_path, phase, reason, blocking, attempt):
    world = World(tmp_path)
    versions = world.three_generations()
    kind = "disable_symlink" if attempt in {"rollback", "revoke"} else "rollback_symlink"
    _row(world, "sma", versions[2], versions[1], phase, reason, kind=kind)
    before = world.snapshot("sma")

    if blocking:
        with pytest.raises(deploy_ops.DeployOpBlockedError):
            _attempt(world, attempt, versions)
        assert world.snapshot("sma") == before
    else:
        result = _attempt(world, attempt, versions)
        assert result.outcome in {"dry_run", "revoked"}


@pytest.mark.parametrize("entry", ["approve", "retry", "bless"])
@pytest.mark.parametrize("phase", ["intent", "syscall_failed"])
def test_the_switch_entries_are_refused_while_an_operation_is_open_in_either_open_phase(
        tmp_path, phase, entry):
    from agentic_fx.store import approvals as approvals_store
    from agentic_fx.store import db as db_store

    conn = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(conn)
    plugins_root = tmp_path / "plugins"
    op_id = _open_disable(conn, "config_dep")
    if phase == "syscall_failed":
        deploy_store.set_phase(conn, op_id, "needs_retry", now=NOW, fail_reason="syscall_failed")
    if entry == "bless":
        candidate = plugins_root / "_human" / "strategy"
    else:
        candidate = plugins_root / "_staging" / "1" / "strategy"
    _strategy_candidate(candidate, plugin="config_dep")
    payload = {"name": "strategy", "candidate_origin": "staging",
               "candidate_path": "plugins/_staging/1/strategy", "indicator_deps": {}}
    approval_id = approvals_store.create(conn, "plugin", payload, NOW)

    with pytest.raises(switch.OpenDeployOpError, match=f"config_dep.*op_id={op_id}"):
        if entry == "approve":
            switch.approve_candidate(
                conn, approval_id, decided_by="human", now=NOW, plugins_root=plugins_root,
                settings=_settings())
        elif entry == "retry":
            switch.retry_approval(
                conn, approval_id, decided_by="human", now=NOW, plugins_root=plugins_root,
                settings=_settings())
        else:
            switch.bless_candidate(
                conn, name="strategy", human_dir=candidate, settings=_settings(), now=NOW,
                decided_by="human")


@pytest.mark.parametrize(("phase", "reason", "blocking"), ROWS)
def test_reconcile_selects_exactly_the_open_rows_and_tries_each_once(
        tmp_path, monkeypatch, phase, reason, blocking):
    world = World(tmp_path)
    versions = world.three_generations()
    _row(world, "sma", versions[2], versions[1], phase, reason)
    live = world.root / "sma"
    unlinks = []
    real_unlink = os.unlink

    def failing_unlink(path, *args, **kwargs):
        if path == live:
            unlinks.append(path)
            raise OSError(13, "denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", failing_unlink)

    results = deploy_reconcile.reconcile_deploy_ops(
        world.conn, world.root, now=NOW, settings=SETTINGS)

    attempted = [r for r in results if r.outcome != "notified"]
    if blocking:
        assert [(r.name, r.outcome, r.fail_reason) for r in attempted] == [
            ("sma", "needs_retry", "syscall_failed")]
        assert unlinks == [live]
    else:
        assert attempted == [] and unlinks == []


@pytest.mark.parametrize(("phase", "reason", "blocking"), ROWS)
def test_a_failed_syscall_adds_no_live_event_to_the_read_model(
        tmp_path, phase, reason, blocking):
    world = World(tmp_path)
    versions = world.three_generations()
    _row(world, "sma", versions[2], versions[1], phase, reason)

    events = [e for e in deploy_store.plugin_live_history(world.conn, "sma")
              if e["source"] == "plugin_deploy_ops"]

    assert len(events) == (0 if blocking else 1)
