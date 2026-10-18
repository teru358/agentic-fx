from __future__ import annotations

import os

import pytest

from agentic_fx.plugin import deploy_disable, deploy_ops, deploy_reconcile
from agentic_fx.plugin.source_state import source_state
from agentic_fx.store import plugin_deploy_ops as deploy_store
from tests.plugin._crash import (
    CrashPoint,
    SCENE_IDS,
    SCENES,
    Scene,
    crash_at,
    crash_after_intent_commit,
    crash_before_set_phase,
    crash_before_notify,
    crash_inside_record_intent,
    crashed,
    db_effects,
    fail_syscall,
    op_count,
    reconcile,
    restart,
)
from tests.plugin.conftest import SETTINGS
from tests.plugin.deploy_world import Activity, World, at


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

    with pytest.raises(CrashPoint):
        deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)
    restart(world)

    deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)
    row = deploy_store.list_by_name(world.conn, "sma")[0]
    assert row["phase"] == "done" and c.is_dir() and not os.path.lexists(live)


def test_a_reconcile_crash_after_disable_unlink_is_completed_on_the_next_start(world, monkeypatch):
    world.three_generations()
    crash_after_intent_commit(monkeypatch)
    crashed(world, _disable, world)
    crash_before_set_phase(monkeypatch, "done")

    with pytest.raises(CrashPoint):
        deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)
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


# ---- request replays and scenes with a revoke request and a cascade ----

def _disable_scene(world, scene, **kwargs):
    return deploy_disable.disable_plugin(
        world.conn, world.root, "sma", revoke=scene.revoke, force=scene.force, now=NOW,
        settings=SETTINGS, **kwargs)


def _live(world):
    return world.root / "sma"


def _rejected_rows(world) -> int:
    return world.conn.execute(
        "SELECT count(*) FROM approval_requests WHERE status='rejected'").fetchone()[0]


def _complete(world, scene, route, activity=None):
    if route == "reconcile":
        return reconcile(world, SETTINGS, now=NOW, activity=activity)
    return _disable_scene(world, scene, activity=activity)


ROUTES = ["reconcile", "cli"]


def test_a_crash_before_intent_recording_lets_the_same_request_start_from_scratch(world, monkeypatch):
    a, b, c = world.three_generations()
    crash_at(monkeypatch, deploy_ops, "record_intent")
    crashed(world, _disable, world)
    assert deploy_reconcile.reconcile_deploy_ops(
        world.conn, world.root, now=NOW, settings=SETTINGS) == []

    done = _disable(world)

    assert done.outcome == "disabled" and op_count(world) == 1
    assert not os.path.lexists(_live(world)) and c.is_dir()


INTENT_STAGES = [
    pytest.param(stage, revoke, cascade, id=f"{stage}-{scene_id}")
    for stage in ("after_revocation", "after_insert", "after_cascade")
    for (revoke, cascade), scene_id in zip(SCENES, SCENE_IDS)
    if revoke or stage != "after_revocation"
]


@pytest.mark.parametrize(("stage", "revoke", "cascade"), INTENT_STAGES)
def test_a_crash_inside_the_intent_transaction_leaves_every_table_untouched_and_the_request_reruns(
        world, monkeypatch, stage, revoke, cascade):
    scene = Scene(world, revoke=revoke, cascade=cascade)
    before = (world.snapshot("sma"), db_effects(world))
    crash_inside_record_intent(monkeypatch, stage)
    crashed(world, _disable_scene, world, scene)

    assert (world.snapshot("sma"), db_effects(world)) == before
    assert reconcile(world, SETTINGS, now=NOW) == []

    done = _disable_scene(world, scene)

    assert done.outcome == "disabled" and op_count(world) == 1
    assert _rejected_rows(world) == (1 if revoke else 0)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(("revoke", "cascade"), SCENES, ids=SCENE_IDS)
def test_a_crash_after_the_intent_commit_is_completed_by_either_route_with_every_effect_kept(
        world, monkeypatch, revoke, cascade, route):
    scene = Scene(world, revoke=revoke, cascade=cascade)
    crash_after_intent_commit(monkeypatch)
    crashed(world, _disable_scene, world, scene)
    op = deploy_store.get_open_by_name(world.conn, "sma")
    assert op is not None and op["phase"] == "intent"
    assert source_state(
        world.conn, world.root, name="sma", content_hash=scene.content,
        artifact_hash=scene.c.name, indicators=[]) == (
            "strategy_not_approved" if revoke else "open_deploy_op")
    assert _rejected_rows(world) == (1 if revoke else 0)
    before = db_effects(world)

    result = _complete(world, scene, route)

    if route == "cli":
        assert result.outcome == "disabled" and result.op_id == op["op_id"]
    assert deploy_store.get_by_id(world.conn, op["op_id"])["phase"] == "done"
    assert not os.path.lexists(_live(world)) and scene.c.is_dir()
    assert op_count(world) == 1
    assert db_effects(world)[1:] == before[1:]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(("revoke", "cascade"), SCENES, ids=SCENE_IDS)
def test_a_crash_between_the_unlink_and_the_done_phase_is_completed_by_either_route(
        world, monkeypatch, revoke, cascade, route):
    scene = Scene(world, revoke=revoke, cascade=cascade)
    crash_before_set_phase(monkeypatch, "done")
    crashed(world, _disable_scene, world, scene)
    op = deploy_store.get_open_by_name(world.conn, "sma")
    assert op is not None and not os.path.lexists(_live(world))
    before = db_effects(world)

    result = _complete(world, scene, route)

    if route == "cli":
        assert result.outcome == "disabled" and result.op_id == op["op_id"]
    assert deploy_store.get_by_id(world.conn, op["op_id"])["phase"] == "done"
    assert op_count(world) == 1 and db_effects(world)[1:] == before[1:]


def test_a_live_swapped_after_the_intent_is_refused_by_the_cli_and_closed_foreign_by_reconcile(
        world, monkeypatch):
    a, b, c = world.three_generations()
    crash_after_intent_commit(monkeypatch)
    crashed(world, _disable, world)
    op = deploy_store.get_open_by_name(world.conn, "sma")
    world.link("sma", a)
    before = (world.snapshot("sma"), db_effects(world))

    with pytest.raises(deploy_ops.SignatureMismatchError) as refused:
        _disable(world)

    assert str(op["op_id"]) in str(refused.value)
    assert (world.snapshot("sma"), db_effects(world)) == before
    results = reconcile(world, SETTINGS, now=NOW)
    assert [r.outcome for r in results if r.op_id == op["op_id"]] == ["foreign"]
    assert _live(world).is_symlink() and os.readlink(_live(world)) == world.target("sma", a)

    fresh = _disable(world)

    assert fresh.outcome == "disabled" and fresh.op_id != op["op_id"]
    assert op_count(world) == 2 and not os.path.lexists(_live(world))


@pytest.mark.parametrize(
    "changed", [{"revoke": True}, {"force": True}, {"revoke": True, "force": True}],
    ids=["revoke", "force", "both"])
def test_a_request_with_other_flags_is_refused_until_reconcile_and_then_checked_as_a_new_op(
        world, monkeypatch, changed):
    a, b, c = world.three_generations()
    crash_after_intent_commit(monkeypatch)
    crashed(world, _disable, world)
    op = deploy_store.get_open_by_name(world.conn, "sma")
    before = (world.snapshot("sma"), db_effects(world))
    kwargs = {"revoke": False, "force": False, **changed}

    with pytest.raises(deploy_ops.SignatureMismatchError) as refused:
        deploy_disable.disable_plugin(
            world.conn, world.root, "sma", now=NOW, settings=SETTINGS, **kwargs)

    assert str(op["op_id"]) in str(refused.value)
    assert (world.snapshot("sma"), db_effects(world)) == before
    reconcile(world, SETTINGS, now=NOW)
    again = deploy_disable.disable_plugin(
        world.conn, world.root, "sma", now=NOW, settings=SETTINGS, **kwargs)
    if kwargs["revoke"]:
        assert again.outcome == "revoke_requires_version"
        assert again.version_candidate == world.content(c)
    else:
        assert again.outcome == "already_disabled"
    assert op_count(world) == 1


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("change", ["dependent_removed", "dependent_added"])
def test_a_dependency_change_after_the_intent_does_not_stop_the_disable(
        world, monkeypatch, change, route):
    scene = Scene(world, revoke=False, cascade=True)
    crash_after_intent_commit(monkeypatch)
    crashed(world, _disable_scene, world, scene)
    op = deploy_store.get_open_by_name(world.conn, "sma")
    if change == "dependent_removed":
        os.unlink(world.root / "strat")
    else:
        extra = world.version("extra", "strategy", kind="strategy", indicator="sma")
        world.deploy("extra", extra, None, 5)

    result = _complete(world, scene, route)

    if route == "cli":
        assert result.outcome == "disabled" and result.op_id == op["op_id"]
    assert deploy_store.get_by_id(world.conn, op["op_id"])["phase"] == "done"
    assert not os.path.lexists(_live(world))


# ---- unlink failures that do not heal ----

@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(("revoke", "cascade"), SCENES, ids=SCENE_IDS)
def test_a_disable_whose_unlink_keeps_failing_stays_open_with_every_effect_kept_until_it_heals(
        world, monkeypatch, revoke, cascade, route):
    scene = Scene(world, revoke=revoke, cascade=cascade)
    live = _live(world)
    heal = fail_syscall(monkeypatch, "unlink", path_pred=lambda path: path == live)
    activity = Activity()

    first = _disable_scene(world, scene, activity=activity)
    kept = db_effects(world)[1:]
    starts = [reconcile(world, SETTINGS, now=NOW, activity=activity) for _ in range(2)]

    row = deploy_store.get_by_id(world.conn, first.op_id)
    assert first.outcome == "needs_retry"
    assert (row["phase"], row["fail_reason"]) == ("needs_retry", "syscall_failed")
    assert [[(r.op_id, r.outcome, r.fail_reason) for r in start] for start in starts] == [
        [(first.op_id, "needs_retry", "syscall_failed")]] * 2
    assert db_effects(world)[1:] == kept and live.is_symlink()
    assert _rejected_rows(world) == (1 if revoke else 0)
    assert [event for _, event, _ in activity.events] == ["plugin_deploy_op_needs_retry"]
    assert deploy_store.get_open_by_name(world.conn, "sma")["op_id"] == first.op_id

    heal()
    result = _complete(world, scene, route, activity)

    if route == "cli":
        assert result.outcome == "disabled" and result.op_id == first.op_id
    assert deploy_store.get_by_id(world.conn, first.op_id)["phase"] == "done"
    assert not os.path.lexists(live) and op_count(world) == 1
    assert db_effects(world)[1:] == kept


def test_a_failure_that_clears_between_starts_is_completed_by_the_next_start(world, monkeypatch):
    _a, _b, c = world.three_generations()
    live = _live(world)
    crash_after_intent_commit(monkeypatch)
    crashed(world, _disable, world)
    heal = fail_syscall(monkeypatch, "unlink", path_pred=lambda path: path == live, times=1)

    failed = reconcile(world, SETTINGS, now=NOW)
    assert [(r.outcome, r.fail_reason) for r in failed] == [("needs_retry", "syscall_failed")]
    assert live.is_symlink()

    healed = reconcile(world, SETTINGS, now=NOW)
    heal()

    assert [r.outcome for r in healed] == ["done"]
    assert not os.path.lexists(live) and c.is_dir() and op_count(world) == 1


def test_reopening_a_failed_disable_resets_the_notification_claim_until_it_completes(world, monkeypatch):
    world.three_generations()
    live = _live(world)
    heal = fail_syscall(monkeypatch, "unlink", path_pred=lambda path: path == live)
    failed = _disable(world)
    assert deploy_store.get_by_id(world.conn, failed.op_id)["notify_attempted"] == 1
    heal()
    crash_before_set_phase(monkeypatch, "done")
    crashed(world, _disable, world)

    reopened = deploy_store.get_by_id(world.conn, failed.op_id)
    assert (reopened["phase"], reopened["notify_attempted"]) == ("intent", 0)

    activity = Activity()
    done = _disable(world, activity=activity)

    assert done.outcome == "disabled" and done.op_id == failed.op_id
    assert deploy_store.get_by_id(world.conn, failed.op_id)["notify_attempted"] == 1
    assert [event for _, event, _ in activity.events] == ["plugin_disabled"]


# ---- revoke on its own ----

def _revoke(world, content):
    return deploy_disable.revoke_version(
        world.conn, world.root, "sma", content_hash=content, now=NOW)


@pytest.mark.parametrize(
    "seam",
    [
        (deploy_disable.approvals_store, "append_revocation"),
        (deploy_store, "insert_done_revoke"),
        (deploy_disable.signals_store, "abandon_pending_for_identities"),
    ],
    ids=["after_revocation", "after_op_row", "after_abandon"],
)
def test_a_revoke_rerun_after_a_process_death_inside_its_transaction_applies_it_once(
        world, monkeypatch, seam):
    content, signal_id = _revoke_setup(world)
    crash_at(monkeypatch, seam[0], seam[1], when="after")
    crashed(world, _revoke, world, content)
    assert reconcile(world, SETTINGS, now=NOW) == []
    activity = Activity()

    first = deploy_disable.revoke_version(
        world.conn, world.root, "sma", content_hash=content, now=NOW, activity=activity)
    second = deploy_disable.revoke_version(
        world.conn, world.root, "sma", content_hash=content, now=NOW, activity=activity)

    assert (first.outcome, second.outcome) == ("revoked", "already_revoked")
    assert op_count(world) == 1 and _rejected_rows(world) == 1
    assert world.conn.execute(
        "SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()[0] == "abandoned"
    assert [event for _, event, _ in activity.events] == ["plugin_deploy_op_done"]


def test_a_revoke_committed_before_its_notice_is_noticed_once_by_either_route(world, monkeypatch):
    content, signal_id = _revoke_setup(world)
    crash_before_notify(monkeypatch)
    crashed(world, _revoke, world, content)
    op = deploy_store.list_by_name(world.conn, "sma")[0]
    activity = Activity()

    rerun = deploy_disable.revoke_version(
        world.conn, world.root, "sma", content_hash=content, now=NOW, activity=activity)
    started = reconcile(world, SETTINGS, now=NOW, activity=activity)

    assert rerun.outcome == "already_revoked" and rerun.notified_op_ids == (op["op_id"],)
    assert started == [] and op_count(world) == 1
    assert [event for _, event, _ in activity.events] == ["plugin_deploy_op_done"]
    assert _rejected_rows(world) == 1


@pytest.mark.parametrize("route", [
    "reconcile",
    "cli",
])
def test_a_dependent_added_after_an_unforced_intent_does_not_stop_the_disable(
        world, monkeypatch, route):
    scene = Scene(world, revoke=False, cascade=False)
    crash_after_intent_commit(monkeypatch)
    crashed(world, _disable_scene, world, scene)
    op = deploy_store.get_open_by_name(world.conn, "sma")
    extra = world.version("extra", "strategy", kind="strategy", indicator="sma")
    world.deploy("extra", extra, None, 5)

    result = _complete(world, scene, route)

    if route == "cli":
        assert result.outcome == "disabled"
    assert deploy_store.get_by_id(world.conn, op["op_id"])["phase"] == "done"
    assert not os.path.lexists(_live(world))
