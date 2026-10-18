from __future__ import annotations

import os

import pytest

from agentic_fx.plugin import deploy_disable, deploy_ops, deploy_reconcile
from agentic_fx.store import plugin_deploy_ops as deploy_store
from tests.plugin._crash import (
    crash_at,
    crash_after_intent_commit,
    crash_before_set_phase,
    crash_inside_record_intent,
    crashed,
    fail_syscall,
)
from tests.plugin.conftest import SETTINGS
from tests.plugin.deploy_world import World, at


NOW = at(181)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _disable(world, **kwargs):
    return deploy_disable.disable_plugin(
        world.conn, world.root, "sma", revoke=False, force=False, now=NOW,
        settings=SETTINGS, **kwargs)


def test_a_crash_before_disable_intent_recording_leaves_no_durable_effect(world, monkeypatch):
    world.three_generations()
    before = world.snapshot("sma")
    crash_at(monkeypatch, deploy_ops, "record_intent")

    crashed(world, _disable, world)

    assert world.snapshot("sma") == before


def test_a_crash_after_the_disable_intent_commit_is_completed_by_reconcile(world, monkeypatch):
    _a, _b, c = world.three_generations()
    crash_after_intent_commit(monkeypatch)
    crashed(world, _disable, world)
    row = deploy_store.get_open_by_name(world.conn, "sma")

    deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)

    assert row is not None
    assert deploy_store.get_by_id(world.conn, row["op_id"])["phase"] == "done"
    assert not os.path.lexists(world.root / "sma") and c.is_dir()


def test_a_crash_after_disable_unlink_is_completed_by_reconcile(world, monkeypatch):
    world.three_generations()
    crash_before_set_phase(monkeypatch, "done")
    crashed(world, _disable, world)
    row = deploy_store.get_open_by_name(world.conn, "sma")

    deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)

    assert row is not None
    assert deploy_store.get_by_id(world.conn, row["op_id"])["phase"] == "done"
    assert not os.path.lexists(world.root / "sma")


def test_a_reconcile_crash_before_disable_unlink_is_retried_on_the_next_start(world, monkeypatch):
    """A process death, unlike an ordinary syscall error, escapes this reconcile pass."""
    _a, _b, c = world.three_generations()
    crash_after_intent_commit(monkeypatch)
    crashed(world, _disable, world)
    live = world.root / "sma"
    fail_syscall(monkeypatch, "unlink", path_pred=lambda path: path == live, crash=True, times=1)

    with pytest.raises(BaseException):
        deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)
    from tests.plugin._crash import restart
    restart(world)

    deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)
    row = deploy_store.list_by_name(world.conn, "sma")[0]
    assert row["phase"] == "done" and c.is_dir() and not os.path.lexists(live)


def test_a_reconcile_crash_after_disable_unlink_is_completed_on_the_next_start(world, monkeypatch):
    world.three_generations()
    crash_after_intent_commit(monkeypatch)
    crashed(world, _disable, world)
    crash_before_set_phase(monkeypatch, "done")

    with pytest.raises(BaseException):
        deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)
    from tests.plugin._crash import restart
    restart(world)

    deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)
    row = deploy_store.list_by_name(world.conn, "sma")[0]
    assert row["phase"] == "done" and not os.path.lexists(world.root / "sma")


@pytest.mark.parametrize("stage", ["after_insert", "after_cascade"])
def test_a_crash_inside_disable_intent_rolls_back_every_database_effect(world, monkeypatch, stage):
    world.three_generations()
    before = world.snapshot("sma")
    crash_inside_record_intent(monkeypatch, stage)
    crashed(world, _disable, world)

    assert world.snapshot("sma") == before


def test_a_persistent_disable_unlink_failure_stays_open_until_the_same_request_is_retried(world, monkeypatch):
    _a, _b, c = world.three_generations()
    live = world.root / "sma"
    heal = fail_syscall(monkeypatch, "unlink", path_pred=lambda path: path == live)

    first = _disable(world)
    deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)
    second = deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)

    row = deploy_store.get_by_id(world.conn, first.op_id)
    assert first.outcome == "needs_retry"
    assert (row["phase"], row["fail_reason"]) == ("needs_retry", "syscall_failed")
    assert second[-1].outcome == "needs_retry" and live.is_symlink()
    heal()
    done = _disable(world)
    assert done.op_id == first.op_id and done.outcome == "disabled"
    assert c.is_dir() and not os.path.lexists(live)


def _revoke_setup(world):
    old = world.version("sma", "old")
    current = world.version("sma", "current")
    world.approve("sma", old, 1)
    world.deploy("sma", current, None, 2)
    content = world.content(old)
    signal_id = world.signal("sma", content)
    return content, signal_id


@pytest.mark.parametrize(
    "seam",
    [
        (deploy_disable.approvals_store, "append_revocation"),
        (deploy_store, "insert_done_revoke"),
        (deploy_disable.signals_store, "abandon_pending_for_identities"),
    ],
)
def test_a_crash_during_revoke_transaction_leaves_no_partial_database_effect(world, monkeypatch, seam):
    content, signal_id = _revoke_setup(world)
    crash_at(monkeypatch, seam[0], seam[1], when="after")

    crashed(
        world, deploy_disable.revoke_version, world.conn, world.root, "sma",
        content_hash=content, now=NOW,
    )

    assert world.conn.execute("SELECT count(*) FROM approval_requests WHERE status='rejected'").fetchone()[0] == 0
    assert world.conn.execute("SELECT count(*) FROM plugin_deploy_ops").fetchone()[0] == 0
    assert world.conn.execute("SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()[0] == "pending"
    assert world.conn.execute("SELECT count(*) FROM plugin_deploy_op_signals").fetchone()[0] == 0


def test_a_revoke_committed_before_notification_is_recovered_at_next_start(world, monkeypatch):
    content, signal_id = _revoke_setup(world)
    from tests.plugin._crash import crash_before_notify
    crash_before_notify(monkeypatch)

    crashed(
        world, deploy_disable.revoke_version, world.conn, world.root, "sma",
        content_hash=content, now=NOW,
    )

    row = deploy_store.list_by_name(world.conn, "sma")[0]
    assert row["op_kind"] == "revoke" and row["notify_attempted"] == 0
    deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)
    assert deploy_store.get_by_id(world.conn, row["op_id"])["notify_attempted"] == 1
    assert world.conn.execute("SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()[0] == "abandoned"
