from __future__ import annotations

import os

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.plugin import (
    deploy_disable, deploy_ops, deploy_reconcile, deploy_retire, deploy_rollback)
from agentic_fx.store import plugin_deploy_ops as deploy_store
from tests.plugin._crash import (
    CrashPoint,
    crash_after_activity,
    crash_before_notify,
    crash_in_activity_write,
    crashed,
    fail_syscall,
    op_count,
    restart,
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
    restart(world)

    deploy_reconcile.reconcile_deploy_ops(
        world.conn, world.root, now=NOW, settings=SETTINGS, activity=Activity())

    assert deploy_store.get_by_id(world.conn, op_id)["notify_attempted"] == 1


@pytest.mark.parametrize(("kind", "phase"), NOTIFY_CASES)
def test_a_process_death_between_the_notice_and_its_claim_during_reconcile_repeats_the_notice(
        world, monkeypatch, kind, phase):
    op_id = _closed_op(world, kind, phase)
    activity = Activity()
    crash_after_activity(monkeypatch)
    with pytest.raises(CrashPoint):
        deploy_reconcile.reconcile_deploy_ops(
            world.conn, world.root, now=NOW, settings=SETTINGS, activity=activity)
    restart(world)
    assert deploy_store.get_by_id(world.conn, op_id)["notify_attempted"] == 0
    assert len(activity.events) == 1

    deploy_reconcile.reconcile_deploy_ops(
        world.conn, world.root, now=NOW, settings=SETTINGS, activity=activity)

    assert deploy_store.get_by_id(world.conn, op_id)["notify_attempted"] == 1
    assert len(activity.events) == 2


# ---- the same windows crossed by a CLI process and re-entered by the CLI ----

def _disable_cli(world):
    world.three_generations()
    return lambda activity: deploy_disable.disable_plugin(
        world.conn, world.root, "sma", revoke=False, force=False, now=NOW,
        settings=SETTINGS, activity=activity)


def _retire_cli(world):
    os.rename(world.version("sma", "plain"), world.root / "sma")
    return lambda activity: deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS,
        activity=activity)


def _revoke_cli(world):
    old = world.version("sma", "old")
    current = world.version("sma", "current")
    world.approve("sma", old, 1)
    world.deploy("sma", current, None, 2)
    return lambda activity: deploy_disable.revoke_version(
        world.conn, world.root, "sma", content_hash=world.content(old), now=NOW,
        activity=activity)


def _rollback_cli(world):
    _a, b, _c = world.three_generations()
    return lambda activity: deploy_rollback.rollback_plugin(
        world.conn, world.root, "sma", to=b.name, revoke=False, force=False, now=NOW,
        settings=SETTINGS, activity=activity)


CLI_KINDS = {"disable": _disable_cli, "retire": _retire_cli, "revoke": _revoke_cli,
             "rollback": _rollback_cli}
# the notice lost before the write, during it, and between the write and its claim
CLI_WINDOWS = [("before_the_write", crash_before_notify, 1),
               ("during_the_write", None, 1),
               ("after_the_write", crash_after_activity, 2)]


@pytest.mark.parametrize(("window", "arm", "events"), CLI_WINDOWS,
                         ids=[w[0] for w in CLI_WINDOWS])
@pytest.mark.parametrize("kind", CLI_KINDS)
def test_a_cli_rerun_notices_a_finished_operation_once_more_and_then_stays_quiet(
        world, monkeypatch, kind, window, arm, events):
    run = CLI_KINDS[kind](world)
    activity = Activity()
    if arm is None:
        crash_in_activity_write(monkeypatch, activity)
    else:
        arm(monkeypatch)
    with pytest.raises(CrashPoint):
        run(activity)
    restart(world)
    row = deploy_store.list_by_name(world.conn, "sma")[-1]
    assert row["phase"] == "done" and row["notify_attempted"] == 0

    rerun = run(activity)

    assert rerun.notified_op_ids == (row["op_id"],)
    assert deploy_store.get_by_id(world.conn, row["op_id"])["notify_attempted"] == 1
    assert len(activity.events) == events
    assert op_count(world) == 1

    third = run(activity)

    assert third.notified_op_ids == () and len(activity.events) == events
    assert op_count(world) == 1


@pytest.mark.parametrize("kind", CLI_KINDS)
def test_a_cli_whose_activity_log_cannot_be_written_still_finishes_and_claims_the_notice(
        world, tmp_path, kind):
    run = CLI_KINDS[kind](world)
    log = ActivityLog(tmp_path / "activity.log")
    (tmp_path / "activity.log").mkdir()

    run(log)

    row = deploy_store.list_by_name(world.conn, "sma")[-1]
    assert row["phase"] == "done" and row["notify_attempted"] == 1
    assert op_count(world) == 1


@pytest.mark.parametrize(("kind", "syscall"), [
    ("disable", "unlink"), ("retire", "rename"), ("rollback", "rename")])
def test_a_cli_rerun_of_a_failed_operation_notices_the_failure_once(
        world, monkeypatch, kind, syscall):
    run = CLI_KINDS[kind](world)
    live = world.root / "sma"
    fail_syscall(monkeypatch, syscall, path_pred=lambda path: (
        path == live or path.name.startswith(".sma.link-d")))
    activity = Activity()
    crash_before_notify(monkeypatch)
    with pytest.raises(CrashPoint):
        run(activity)
    restart(world)
    row = deploy_store.list_by_name(world.conn, "sma")[-1]
    assert (row["phase"], row["fail_reason"], row["notify_attempted"]) == (
        "needs_retry", "syscall_failed", 0)

    rerun = run(activity)

    assert rerun.outcome == "needs_retry" and rerun.op_id == row["op_id"]
    assert deploy_store.get_by_id(world.conn, row["op_id"])["notify_attempted"] == 1
    assert [event for _, event, _ in activity.events] == ["plugin_deploy_op_needs_retry"]
    assert op_count(world) == 1
