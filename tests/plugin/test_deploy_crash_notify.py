from __future__ import annotations

import pytest

from agentic_fx.plugin import deploy_disable, deploy_ops, deploy_reconcile
from agentic_fx.store import plugin_deploy_ops as deploy_store
from tests.plugin._crash import (
    CrashPoint,
    crash_after_activity,
    crash_before_notify,
    crash_in_activity_write,
    crashed,
)
from tests.plugin.conftest import SETTINGS
from tests.plugin.deploy_world import Activity, World, at


NOW = at(180)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _closed_op(world, kind: str, phase: str) -> int:
    a, b, c = world.three_generations()
    if kind == "disable_symlink":
        return world.intent("sma", op_kind=kind, from_ver=c, phase=phase,
                            fail_reason="syscall_failed" if phase == "needs_retry" else None)
    if kind == "rollback_symlink":
        return world.intent("sma", op_kind=kind, from_ver=c, to_ver=b, phase=phase,
                            fail_reason="verify_failed" if phase == "needs_retry" else None)
    if kind == "revoke":
        old = world.version("sma", "old")
        current = world.version("sma", "current")
        world.approve("sma", old, 1)
        world.deploy("sma", current, None, 2)
        outcome = deploy_disable.revoke_version(
            world.conn, world.root, "sma", content_hash=world.content(old), now=NOW)
        world.conn.execute(
            "UPDATE plugin_deploy_ops SET notify_attempted=0 WHERE op_id=?", (outcome.op_id,))
        world.conn.commit()
        return outcome.op_id
    return world.intent("sma", op_kind=kind, retired_path="_retired/sma-1",
                        phase=phase, fail_reason="syscall_failed" if phase == "needs_retry" else None)


NOTIFY_CASES = [
    ("disable_symlink", "done"), ("disable_symlink", "needs_retry"),
    ("disable_symlink", "foreign"), ("rollback_symlink", "done"),
    ("rollback_symlink", "needs_retry"), ("rollback_symlink", "foreign"),
    ("retire_plain", "done"), ("retire_plain", "needs_retry"),
    ("retire_plain", "foreign"), ("revoke", "done"),
]


@pytest.mark.parametrize(
    ("kind", "phase"),
    NOTIFY_CASES,
)
def test_reconcile_recovers_a_notification_interrupted_before_the_write(world, monkeypatch, kind, phase):
    op_id = _closed_op(world, kind, phase)
    activity = Activity()
    crash_before_notify(monkeypatch)
    crashed(
        world, deploy_ops.try_notify, world.conn, deploy_store.get_by_id(world.conn, op_id), activity,
        now=NOW,
    )

    results = deploy_reconcile.reconcile_deploy_ops(
        world.conn, world.root, now=NOW, settings=SETTINGS, activity=activity)

    row = deploy_store.get_by_id(world.conn, op_id)
    assert row["notify_attempted"] == 1
    assert results
    assert len(activity.events) == 1


@pytest.mark.parametrize(("kind", "phase"), NOTIFY_CASES)
def test_reconcile_retries_after_activity_was_written_but_before_its_claim(world, monkeypatch, kind, phase):
    op_id = _closed_op(world, kind, phase)
    activity = Activity()
    crash_after_activity(monkeypatch)
    crashed(
        world, deploy_ops.try_notify, world.conn, deploy_store.get_by_id(world.conn, op_id), activity,
        now=NOW,
    )

    deploy_reconcile.reconcile_deploy_ops(
        world.conn, world.root, now=NOW, settings=SETTINGS, activity=activity)

    assert deploy_store.get_by_id(world.conn, op_id)["notify_attempted"] == 1
    assert len(activity.events) == 2


@pytest.mark.parametrize(("kind", "phase"), NOTIFY_CASES)
def test_activity_write_exception_still_claims_each_terminal_deploy_notification(world, kind, phase):
    op_id = _closed_op(world, kind, phase)

    class BrokenActivity:
        def write(self, *_args):
            raise RuntimeError("activity backend unavailable")

    assert deploy_ops.try_notify(
        world.conn, deploy_store.get_by_id(world.conn, op_id), BrokenActivity(), now=NOW)
    row = deploy_store.get_by_id(world.conn, op_id)
    assert row["phase"] == phase and row["notify_attempted"] == 1


@pytest.mark.parametrize(("kind", "phase"), NOTIFY_CASES)
def test_reconcile_skips_an_already_claimed_terminal_deploy_notification(world, kind, phase):
    op_id = _closed_op(world, kind, phase)
    activity = Activity()
    assert deploy_ops.try_notify(
        world.conn, deploy_store.get_by_id(world.conn, op_id), activity, now=NOW)

    results = deploy_reconcile.reconcile_deploy_ops(
        world.conn, world.root, now=NOW, settings=SETTINGS, activity=activity)

    assert len(activity.events) == 1
    assert all(result.outcome != "notified" for result in results)


def test_a_base_exception_from_activity_keeps_the_notification_unclaimed_until_next_start(world, monkeypatch):
    op_id = _closed_op(world, "disable_symlink", "done")
    activity = Activity()
    crash_in_activity_write(monkeypatch, activity)
    with pytest.raises(CrashPoint):
        deploy_reconcile.reconcile_deploy_ops(
            world.conn, world.root, now=NOW, settings=SETTINGS, activity=activity)
    from tests.plugin._crash import restart
    restart(world)

    deploy_reconcile.reconcile_deploy_ops(
        world.conn, world.root, now=NOW, settings=SETTINGS, activity=Activity())

    assert deploy_store.get_by_id(world.conn, op_id)["notify_attempted"] == 1
