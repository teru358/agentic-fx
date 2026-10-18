from __future__ import annotations

import errno
import os

import pytest

from agentic_fx.plugin import deploy_ops, deploy_reconcile, deploy_rollback
from agentic_fx.plugin.source_state import source_state
from agentic_fx.store import plugin_deploy_ops as deploy_store
from tests.plugin._conc_support import Holder
from tests.plugin._crash import (
    CrashPoint,
    crash_after_intent_commit,
    crash_at,
    crash_before_set_phase,
    crash_before_swap,
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

NOW = at(190)

SCENES = [(False, False), (True, False), (False, True), (True, True)]
SCENE_IDS = ["plain", "revoke", "cascade", "revoke-and-cascade"]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _rollback(world, name="sma", **kwargs):
    kwargs.setdefault("to", None)
    kwargs.setdefault("revoke", False)
    kwargs.setdefault("force", False)
    return deploy_rollback.rollback_plugin(
        world.conn, world.root, name, now=NOW, settings=SETTINGS, **kwargs)


def _reconcile(world, activity=None):
    return reconcile(world, SETTINGS, now=NOW, activity=activity)


def _tamper(version):
    plugin = version / "plugin.py"
    plugin.write_text(plugin.read_text() + " ")


def _repair(version):
    plugin = version / "plugin.py"
    plugin.write_text(plugin.read_text()[:-1])


def _live(world, name="sma"):
    return world.root / name


def _link(version, name="sma"):
    return ("link", f".versions/{name}/{version.name}")


def _temp_links(world):
    return [p.name for p in world.root.iterdir() if ".link-" in p.name]


def _op_signal_links(world):
    return [tuple(r) for r in world.conn.execute(
        "SELECT * FROM plugin_deploy_op_signals ORDER BY op_id, signal_id")]


def _signal_statuses(world):
    return {sid: status for sid, status in world.conn.execute("SELECT id,status FROM signals")}


def _only_op(world, name="sma"):
    rows = deploy_store.list_by_name(world.conn, name)
    assert len(rows) == 1
    return rows[0]


def _source_state(world, version, name="sma"):
    return source_state(
        world.conn, world.root, name=name, content_hash=world.content(version),
        artifact_hash=version.name, indicators=[])


def _history_events(world, name="sma"):
    return [e for e in deploy_store.plugin_live_history(world.conn, name)
            if e["source"] == "plugin_deploy_ops"]


class RollbackScene:
    """sma rolled back from c to b, optionally with a revoke and a pinned dependent."""

    def __init__(self, world, *, revoke: bool, cascade: bool) -> None:
        self.world = world
        self.revoke = revoke
        self.cascade = cascade
        self.a, self.b, self.c = world.three_generations()
        content = world.content(self.c)
        self.signals = [world.signal("sma", content),
                        world.signal("sma", content, claimed=True)]
        self.strategy = None
        if cascade:
            self.strategy = world.version("strat", "s", kind="strategy",
                                          indicator="sma", pin=content)
            world.deploy("strat", self.strategy, None, 5)
            strategy_content = world.content(self.strategy)
            self.signals += [world.signal("strat", strategy_content),
                             world.signal("strat", strategy_content, claimed=True)]

    @property
    def kwargs(self) -> dict:
        return {"to": self.b.name, "revoke": self.revoke, "force": self.cascade}

    def run(self, **override):
        return _rollback(self.world, **{**self.kwargs, **override})


@pytest.fixture(params=SCENES, ids=SCENE_IDS)
def scene(request, world):
    revoke, cascade = request.param
    return RollbackScene(world, revoke=revoke, cascade=cascade)


def _crash_with_open_intent(world, monkeypatch, scene):
    crash_after_intent_commit(monkeypatch)
    crashed(world, scene.run)
    row = deploy_store.get_open_by_name(world.conn, "sma")
    assert row is not None and row["phase"] == "intent"
    return int(row["op_id"])


def _converge(world, how, scene, activity=None):
    if how == "reconcile":
        return _reconcile(world, activity)
    return scene.run(activity=activity)


def _assert_rolled_back(world, scene, op_id):
    row = deploy_store.get_by_id(world.conn, op_id)
    assert (row["phase"], row["fail_reason"], row["notify_attempted"]) == ("done", None, 1)
    assert os.readlink(_live(world)) == _link(scene.b)[1]
    assert op_count(world) == 1
    assert not _temp_links(world)
    statuses = {sid: status for sid, status in world.conn.execute(
        "SELECT id,status FROM signals")}
    assert statuses[scene.signals[0]] == "abandoned"
    if scene.cascade:
        assert statuses[scene.signals[2]] == "abandoned"
    revoked = world.conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE status='rejected'").fetchone()[0]
    assert revoked == (1 if scene.revoke else 0)


# ---- before the intent is recorded ----

def test_a_crash_before_rollback_intent_recording_leaves_no_durable_effect(
        world, monkeypatch, scene):
    before = (world.snapshot("sma"), db_effects(world))
    crash_at(monkeypatch, deploy_ops, "record_intent")

    crashed(world, scene.run)

    assert (world.snapshot("sma"), db_effects(world)) == before
    assert _reconcile(world) == []
    done = scene.run()
    assert done.outcome == "rolled_back" and op_count(world) == 1


@pytest.mark.parametrize("stage", ["after_revocation", "after_insert", "after_cascade"])
def test_a_crash_inside_rollback_intent_recording_rolls_back_every_database_effect(
        world, monkeypatch, scene, stage):
    if stage == "after_revocation" and not scene.revoke:
        pytest.skip("no revocation is written without --revoke")
    before = (world.snapshot("sma"), db_effects(world))
    crash_inside_record_intent(monkeypatch, stage)

    crashed(world, scene.run)

    assert (world.snapshot("sma"), db_effects(world)) == before
    assert _reconcile(world) == []
    assert op_count(world) == 0


# ---- the intent is durable, the live link is untouched ----

@pytest.mark.parametrize("how", ["reconcile", "cli"])
@pytest.mark.parametrize("seam", ["intent_commit", "before_swap"])
def test_a_crash_after_the_rollback_intent_commit_is_completed(
        world, monkeypatch, scene, seam, how):
    if seam == "intent_commit":
        crash_after_intent_commit(monkeypatch)
    else:
        crash_before_swap(monkeypatch)
    crashed(world, scene.run)
    row = deploy_store.get_open_by_name(world.conn, "sma")
    assert row is not None and os.readlink(_live(world)) == _link(scene.c)[1]
    assert _source_state(world, scene.c) == (
        "strategy_not_approved" if scene.revoke else "open_deploy_op")
    activity = Activity()

    _converge(world, how, scene, activity)

    _assert_rolled_back(world, scene, int(row["op_id"]))
    assert [e for _, e, _ in activity.events] == ["plugin_deploy_op_done"]


@pytest.mark.parametrize("how", ["reconcile", "cli"])
def test_a_rollback_whose_target_no_longer_verifies_after_a_crash_closes_as_verify_failed(
        world, monkeypatch, scene, how):
    op_id = _crash_with_open_intent(world, monkeypatch, scene)
    _tamper(scene.b)
    activity = Activity()

    result = _converge(world, how, scene, activity)

    assert not os.path.lexists(_live(world)) and scene.c.is_dir()
    row = deploy_store.get_by_id(world.conn, op_id)
    assert (row["phase"], row["fail_reason"], row["notify_attempted"]) == (
        "needs_retry", "verify_failed", 1)
    assert deploy_store.get_open_by_name(world.conn, "sma") is None
    assert op_count(world) == 1
    if how == "cli":
        assert result.outcome == "verify_failed" and result.op_id == op_id
    assert [e for _, e, _ in activity.events] == ["plugin_deploy_op_needs_retry"]
    assert _history_events(world)[-1]["to"] == "absent"
    rejected = world.conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE status='rejected'").fetchone()[0]
    assert rejected == (1 if scene.revoke else 0)
    assert world.conn.execute(
        "SELECT status FROM signals WHERE id=?", (scene.signals[0],)).fetchone()[0] == "abandoned"


def test_a_cli_rerun_that_removes_the_live_source_after_a_failed_verification_says_so(
        world, monkeypatch, scene):
    _crash_with_open_intent(world, monkeypatch, scene)
    _tamper(scene.b)

    result = scene.run()

    assert result.outcome == "verify_failed" and result.live_removed is True
    assert not os.path.lexists(_live(world))


def test_a_cli_rerun_from_an_absent_live_that_fails_verification_removed_nothing(
        world, monkeypatch):
    scene = _absent_live_scene(world)
    crash_after_intent_commit(monkeypatch)
    crashed(world, scene.run)
    _tamper(scene.b)

    result = scene.run()

    assert result.outcome == "verify_failed" and result.live_removed is False


def test_a_failed_verification_without_an_open_operation_is_refused_and_records_nothing(
        world, scene):
    _tamper(scene.b)
    before = (world.snapshot("sma"), db_effects(world))

    outcome = scene.run()

    assert outcome.outcome in {"refused", "dependents_blocked"}
    assert (world.snapshot("sma"), db_effects(world)) == before


# ---- the swap itself ----

def _temp_then_crash_before_rename(monkeypatch, world):
    fail_syscall(monkeypatch, "rename", crash=True, times=1,
                 path_pred=lambda path: path.name.startswith(".sma.link-d"))


@pytest.mark.parametrize("how", ["reconcile", "cli"])
def test_a_crash_between_the_temp_link_and_its_rename_leaves_no_residue_once_completed(
        world, monkeypatch, scene, how):
    _temp_then_crash_before_rename(monkeypatch, world)
    crashed(world, scene.run)
    op_id = int(deploy_store.get_open_by_name(world.conn, "sma")["op_id"])
    assert _temp_links(world) == [f".sma.link-d{op_id}"]
    assert os.readlink(_live(world)) == _link(scene.c)[1]

    _converge(world, how, scene)

    _assert_rolled_back(world, scene, op_id)


@pytest.mark.parametrize("how", ["reconcile", "cli"])
def test_a_crash_after_the_swap_and_before_done_is_completed_without_touching_the_fs(
        world, monkeypatch, scene, how):
    crash_before_set_phase(monkeypatch, "done")
    crashed(world, scene.run)
    op_id = int(deploy_store.get_open_by_name(world.conn, "sma")["op_id"])
    assert os.readlink(_live(world)) == _link(scene.b)[1]
    assert _source_state(world, scene.b) == "open_deploy_op"
    activity = Activity()

    _converge(world, how, scene, activity)

    _assert_rolled_back(world, scene, op_id)
    assert [e for _, e, _ in activity.events] == ["plugin_deploy_op_done"]


@pytest.mark.parametrize("seam", ["before_swap", "before_rename"])
def test_a_reconcile_crash_before_the_swap_is_retried_on_the_next_start(
        world, monkeypatch, scene, seam):
    op_id = _crash_with_open_intent(world, monkeypatch, scene)
    if seam == "before_swap":
        crash_before_swap(monkeypatch)
    else:
        _temp_then_crash_before_rename(monkeypatch, world)

    with pytest.raises(CrashPoint):
        _reconcile(world)
    restart(world)
    assert os.readlink(_live(world)) == _link(scene.c)[1]

    _reconcile(world)

    _assert_rolled_back(world, scene, op_id)


def test_a_reconcile_crash_after_the_swap_is_completed_on_the_next_start(
        world, monkeypatch, scene):
    op_id = _crash_with_open_intent(world, monkeypatch, scene)
    crash_before_set_phase(monkeypatch, "done")

    with pytest.raises(CrashPoint):
        _reconcile(world)
    restart(world)
    assert os.readlink(_live(world)) == _link(scene.b)[1]

    _reconcile(world)

    _assert_rolled_back(world, scene, op_id)


# ---- the source link is gone before the verdict is recorded ----

def _crash_after_unlinking_the_source(world, monkeypatch, scene):
    op_id = _crash_with_open_intent(world, monkeypatch, scene)
    _tamper(scene.b)
    crash_before_set_phase(monkeypatch, "needs_retry")
    with pytest.raises(CrashPoint):
        _reconcile(world)
    restart(world)
    assert not os.path.lexists(_live(world))
    row = deploy_store.get_by_id(world.conn, op_id)
    assert (row["phase"], row["fail_reason"]) == ("intent", None)
    return op_id


@pytest.mark.parametrize("how", ["reconcile", "cli"])
def test_a_lost_source_link_with_a_target_that_still_fails_closes_as_verify_failed(
        world, monkeypatch, scene, how):
    op_id = _crash_after_unlinking_the_source(world, monkeypatch, scene)

    _converge(world, how, scene)

    row = deploy_store.get_by_id(world.conn, op_id)
    assert (row["phase"], row["fail_reason"]) == ("needs_retry", "verify_failed")
    assert not os.path.lexists(_live(world))
    assert _history_events(world)[-1]["to"] == "absent"
    assert op_count(world) == 1


@pytest.mark.parametrize("how", ["reconcile", "cli"])
def test_a_lost_source_link_with_a_repaired_target_completes_the_swap(
        world, monkeypatch, scene, how):
    op_id = _crash_after_unlinking_the_source(world, monkeypatch, scene)
    _repair(scene.b)

    _converge(world, how, scene)

    _assert_rolled_back(world, scene, op_id)


def test_a_lost_source_link_rerun_with_another_target_is_rejected_and_changes_nothing(
        world, monkeypatch):
    scene = RollbackScene(world, revoke=False, cascade=False)
    op_id = _crash_after_unlinking_the_source(world, monkeypatch, scene)
    before = (world.snapshot("sma"), db_effects(world))

    with pytest.raises(deploy_ops.SignatureMismatchError, match=str(op_id)):
        scene.run(to=scene.a.name)

    assert (world.snapshot("sma"), db_effects(world)) == before


def test_a_lost_source_link_can_be_rolled_back_to_another_version_after_the_verdict(
        world, monkeypatch):
    scene = RollbackScene(world, revoke=False, cascade=False)
    _crash_after_unlinking_the_source(world, monkeypatch, scene)
    _reconcile(world)

    outcome = scene.run(to=scene.a.name)

    assert outcome.outcome == "rolled_back"
    assert os.readlink(_live(world)) == _link(scene.a)[1]
    assert op_count(world) == 2


def test_a_lost_source_link_rerun_that_drops_the_revoke_is_rejected(world, monkeypatch):
    scene = RollbackScene(world, revoke=True, cascade=False)
    _crash_after_unlinking_the_source(world, monkeypatch, scene)
    before = (world.snapshot("sma"), db_effects(world))

    with pytest.raises(deploy_ops.SignatureMismatchError):
        scene.run(revoke=False)

    assert (world.snapshot("sma"), db_effects(world)) == before


@pytest.mark.parametrize("how", ["reconcile", "cli"])
@pytest.mark.parametrize("repaired", [False, True])
def test_a_source_link_removed_by_hand_after_the_intent_is_not_foreign(
        world, monkeypatch, how, repaired):
    scene = RollbackScene(world, revoke=False, cascade=False)
    op_id = _crash_with_open_intent(world, monkeypatch, scene)
    _live(world).unlink()
    if not repaired:
        _tamper(scene.b)

    _converge(world, how, scene)

    row = deploy_store.get_by_id(world.conn, op_id)
    if repaired:
        assert (row["phase"], row["fail_reason"]) == ("done", None)
        assert os.readlink(_live(world)) == _link(scene.b)[1]
    else:
        assert (row["phase"], row["fail_reason"]) == ("needs_retry", "verify_failed")
        assert not os.path.lexists(_live(world))


# ---- rollback from an absent live ----

def _absent_live_scene(world):
    scene = RollbackScene(world, revoke=False, cascade=False)
    from agentic_fx.plugin import deploy_disable
    deploy_disable.disable_plugin(
        world.conn, world.root, "sma", revoke=False, force=False, now=NOW,
        settings=SETTINGS)
    assert not os.path.lexists(_live(world))
    return scene


@pytest.mark.parametrize("how", ["reconcile", "cli"])
@pytest.mark.parametrize("verifies", [True, False])
def test_a_rollback_from_an_absent_live_crashes_and_converges_by_verification(
        world, monkeypatch, how, verifies):
    scene = _absent_live_scene(world)
    events_before = len(_history_events(world))
    crash_after_intent_commit(monkeypatch)
    crashed(world, scene.run)
    row = deploy_store.get_open_by_name(world.conn, "sma")
    assert row["from_kind"] == "absent"
    if not verifies:
        _tamper(scene.b)

    _converge(world, how, scene)

    after = deploy_store.get_by_id(world.conn, row["op_id"])
    if verifies:
        assert after["phase"] == "done"
        assert os.readlink(_live(world)) == _link(scene.b)[1]
    else:
        assert (after["phase"], after["fail_reason"]) == ("needs_retry", "verify_failed")
        assert not os.path.lexists(_live(world))
        assert len(_history_events(world)) == events_before


def test_a_rollback_from_an_absent_live_never_accepts_a_revoke(world):
    scene = _absent_live_scene(world)

    with pytest.raises(ValueError):
        scene.run(revoke=True)

    assert op_count(world) == 1


# ---- the target is damaged between the intent and the swap ----

def test_a_revoke_is_kept_when_the_target_fails_after_the_intent(world, monkeypatch):
    scene = RollbackScene(world, revoke=True, cascade=False)
    _crash_with_open_intent(world, monkeypatch, scene)
    _tamper(scene.b)
    _reconcile(world)

    assert world.conn.execute(
        "SELECT COUNT(*) FROM approval_requests WHERE status='rejected'").fetchone()[0] == 1
    assert world.conn.execute(
        "SELECT status FROM signals WHERE id=?", (scene.signals[0],)).fetchone()[0] == "abandoned"
    other = scene.run(to=scene.a.name, revoke=False)
    assert other.outcome == "rolled_back"


# ---- a third party changes the live link ----

def test_a_third_party_link_after_the_crash_is_foreign_and_a_rerun_is_rejected(
        world, monkeypatch):
    scene = RollbackScene(world, revoke=False, cascade=False)
    op_id = _crash_with_open_intent(world, monkeypatch, scene)
    _live(world).unlink()
    _live(world).symlink_to(_link(scene.a)[1])
    before = (world.snapshot("sma"), db_effects(world))

    with pytest.raises(deploy_ops.SignatureMismatchError, match=str(op_id)):
        scene.run()
    assert (world.snapshot("sma"), db_effects(world)) == before

    _reconcile(world)

    row = deploy_store.get_by_id(world.conn, op_id)
    assert row["phase"] == "foreign"
    assert os.readlink(_live(world)) == _link(scene.a)[1]


@pytest.mark.parametrize("replacement", ["file", "dir"])
def test_a_non_link_put_in_place_of_the_source_is_never_closed_as_a_failed_verification(
        world, monkeypatch, replacement):
    scene = RollbackScene(world, revoke=False, cascade=False)
    op_id = _crash_with_open_intent(world, monkeypatch, scene)
    _live(world).unlink()
    if replacement == "file":
        _live(world).write_text("third party")
    else:
        _live(world).mkdir()
        (_live(world) / "keep.txt").write_text("third party")
    _tamper(scene.b)
    before = (world.snapshot("sma"), db_effects(world))

    refusal = (deploy_ops.SignatureMismatchError if replacement == "file" else ValueError)
    with pytest.raises(refusal):
        scene.run()
    assert (world.snapshot("sma"), db_effects(world)) == before
    assert deploy_store.get_open_by_name(world.conn, "sma") is not None

    _reconcile(world)

    row = deploy_store.get_by_id(world.conn, op_id)
    assert (row["phase"], row["fail_reason"]) == ("foreign", None)
    assert os.path.lexists(_live(world)) and not _live(world).is_symlink()
    assert (_live(world).read_text() == "third party" if replacement == "file"
            else (_live(world) / "keep.txt").read_text() == "third party")


@pytest.mark.parametrize("replacement", ["file", "dir", "other_link"])
def test_closing_a_failed_verification_leaves_a_live_that_is_not_the_source_alone_and_is_foreign(
        world, monkeypatch, replacement):
    scene = RollbackScene(world, revoke=False, cascade=False)
    op_id = _crash_with_open_intent(world, monkeypatch, scene)
    _live(world).unlink()
    if replacement == "file":
        _live(world).write_text("third party")
    elif replacement == "dir":
        _live(world).mkdir()
    else:
        _live(world).symlink_to(_link(scene.a)[1])
    _tamper(scene.b)
    open_row = deploy_store.get_open_by_name(world.conn, "sma")
    failure = deploy_rollback.VerificationFailure(("target no longer verifies",))

    outcome = deploy_rollback.close_failed_verification(
        world.conn, world.root, open_row, op_id, failure, None, now=NOW)

    assert outcome.outcome == "foreign" and outcome.op_id == op_id
    row = deploy_store.get_by_id(world.conn, op_id)
    assert (row["phase"], row["fail_reason"]) == ("foreign", None)
    assert os.path.lexists(_live(world))
    if replacement == "other_link":
        assert os.readlink(_live(world)) == _link(scene.a)[1]
    else:
        assert not _live(world).is_symlink()


# ---- the dependency of the target strategy changes ----

def _strategy_scene(world):
    ind = world.version("ind", "i1")
    world.deploy("ind", ind, None, 1)
    s1 = world.version("strat", "s1", kind="strategy")
    s2 = world.version("strat", "s2", kind="strategy", indicator="ind",
                       pin=world.content(ind))
    world.deploy("strat", s1, None, 2)
    world.deploy("strat", s2, s1, 3)
    world.link("strat", s1)
    return ind, s1, s2


@pytest.mark.parametrize("how", ["reconcile", "cli"])
@pytest.mark.parametrize("force", [False, True])
def test_a_dependency_changed_after_the_crash_fails_verification_even_with_force(
        world, monkeypatch, how, force):
    ind, s1, s2 = _strategy_scene(world)
    crash_after_intent_commit(monkeypatch)
    crashed(world, _rollback, world, name="strat", to=s2.name, force=force)
    op_id = int(deploy_store.get_open_by_name(world.conn, "strat")["op_id"])
    world.deploy("ind", world.version("ind", "i2"), ind, 4)

    if how == "reconcile":
        _reconcile(world)
    else:
        _rollback(world, name="strat", to=s2.name, force=force)

    row = deploy_store.get_by_id(world.conn, op_id)
    assert (row["phase"], row["fail_reason"]) == ("needs_retry", "verify_failed")
    assert not os.path.lexists(_live(world, "strat"))


def test_a_dependent_that_became_unresolvable_after_the_crash_is_cascaded_by_the_rerun(
        world, monkeypatch):
    a, b, c = world.three_generations()
    crash_after_intent_commit(monkeypatch)
    crashed(world, _rollback, world, to=b.name, force=True)
    op_id = int(deploy_store.get_open_by_name(world.conn, "sma")["op_id"])
    strategy = world.version("strat", "s", kind="strategy", indicator="sma",
                             pin=world.content(c))
    world.deploy("strat", strategy, None, 6)
    pending = world.signal("strat", world.content(strategy))
    claimed = world.signal("strat", world.content(strategy), claimed=True)

    outcome = _rollback(world, to=b.name, force=True)

    assert outcome.outcome == "rolled_back" and outcome.op_id == op_id
    assert dict(world.conn.execute("SELECT id,status FROM signals").fetchall()) == {
        pending: "abandoned", claimed: "claimed"}
    assert {tuple(r) for r in world.conn.execute(
        "SELECT signal_id,status_at_link FROM plugin_deploy_op_signals WHERE op_id=?",
        (op_id,))} == {(pending, "pending"), (claimed, "claimed")}


# ---- a different request than the recorded one ----

@pytest.mark.parametrize("changed", ["to", "revoke", "force"])
def test_a_rerun_with_another_signature_after_a_crash_is_rejected_and_the_reconcile_still_converges(
        world, monkeypatch, changed):
    scene = RollbackScene(world, revoke=False, cascade=False)
    op_id = _crash_with_open_intent(world, monkeypatch, scene)
    before = (world.snapshot("sma"), db_effects(world))
    override = {"to": {"to": scene.a.name}, "revoke": {"revoke": True},
                "force": {"force": True}}[changed]

    with pytest.raises(deploy_ops.SignatureMismatchError, match=str(op_id)):
        scene.run(**override)
    assert (world.snapshot("sma"), db_effects(world)) == before

    _reconcile(world)

    _assert_rolled_back(world, scene, op_id)


@pytest.mark.parametrize("changed", ["to", "revoke", "force"])
def test_a_dry_run_after_the_swap_rejects_another_signature_and_changes_nothing(
        world, monkeypatch, changed):
    scene = RollbackScene(world, revoke=False, cascade=False)
    crash_before_set_phase(monkeypatch, "done")
    crashed(world, scene.run)
    op_id = int(deploy_store.get_open_by_name(world.conn, "sma")["op_id"])
    assert os.readlink(_live(world)) == _link(scene.b)[1]
    before = (world.snapshot("sma"), db_effects(world))
    override = {"to": {"to": scene.a.name}, "revoke": {"revoke": True},
                "force": {"force": True}}[changed]

    with pytest.raises(deploy_ops.SignatureMismatchError, match=str(op_id)):
        scene.run(dry_run=True, **override)

    assert (world.snapshot("sma"), db_effects(world)) == before


def test_a_dry_run_after_the_swap_with_the_same_signature_reports_the_open_operation(
        world, monkeypatch):
    scene = RollbackScene(world, revoke=False, cascade=False)
    crash_before_set_phase(monkeypatch, "done")
    crashed(world, scene.run)
    op_id = int(deploy_store.get_open_by_name(world.conn, "sma")["op_id"])
    before = (world.snapshot("sma"), db_effects(world))

    outcome = scene.run(dry_run=True)

    assert outcome.outcome == "dry_run" and outcome.op_id == op_id
    assert (world.snapshot("sma"), db_effects(world)) == before


# ---- persistent syscall failures ----

def _fail_swap(monkeypatch, world, variant):
    if variant == "symlink":
        return fail_syscall(monkeypatch, "symlink", errno_=errno.EACCES,
                            path_pred=lambda path: path.parts[:1] == (".versions",))
    return fail_syscall(monkeypatch, "rename", errno_=errno.EXDEV,
                        path_pred=lambda path: path.name.startswith(".sma.link-d"))


@pytest.mark.parametrize("variant", ["symlink", "rename"])
@pytest.mark.parametrize("how", ["reconcile", "cli"])
def test_a_persistent_swap_failure_keeps_the_operation_open_until_it_is_healed(
        world, monkeypatch, scene, variant, how):
    op_id = _crash_with_open_intent(world, monkeypatch, scene)
    links = _op_signal_links(world)
    assert links
    heal = _fail_swap(monkeypatch, world, variant)
    activity = Activity()

    first = _reconcile(world, activity)
    second = _reconcile(world, activity)

    for results in (first, second):
        assert [(r.outcome, r.fail_reason) for r in results if r.outcome != "notified"] == [
            ("needs_retry", "syscall_failed")]
    row = deploy_store.get_by_id(world.conn, op_id)
    assert (row["phase"], row["fail_reason"], row["notify_attempted"]) == (
        "needs_retry", "syscall_failed", 1)
    assert os.readlink(_live(world)) == _link(scene.c)[1]
    assert deploy_store.get_open_by_name(world.conn, "sma")["op_id"] == op_id
    assert _source_state(world, scene.c) == (
        "strategy_not_approved" if scene.revoke else "open_deploy_op")
    assert [e for _, e, _ in activity.events] == ["plugin_deploy_op_needs_retry"]
    if variant == "symlink":
        assert _temp_links(world) == []
    else:
        assert _temp_links(world) in ([], [f".sma.link-d{op_id}"])
    assert _op_signal_links(world) == links
    statuses = _signal_statuses(world)
    assert (statuses[scene.signals[0]], statuses[scene.signals[1]]) == ("abandoned", "claimed")

    heal()
    _converge(world, how, scene, activity)

    _assert_rolled_back(world, scene, op_id)
    assert [e for _, e, _ in activity.events] == [
        "plugin_deploy_op_needs_retry", "plugin_deploy_op_done"]


@pytest.mark.parametrize("variant", ["symlink", "rename"])
def test_a_cli_rollback_that_cannot_swap_reports_needs_retry_and_a_rerun_after_healing_finishes(
        world, monkeypatch, scene, variant):
    heal = _fail_swap(monkeypatch, world, variant)

    failed = scene.run()

    assert failed.outcome == "needs_retry" and failed.filesystem_switched is False
    assert os.readlink(_live(world)) == _link(scene.c)[1]
    heal()
    done = scene.run()
    assert done.outcome == "rolled_back" and done.op_id == failed.op_id
    _assert_rolled_back(world, scene, failed.op_id)


def test_a_needs_retry_verdict_is_noticed_and_leaves_the_read_model_event_to_absent(
        world, monkeypatch):
    scene = RollbackScene(world, revoke=False, cascade=False)
    op_id = _crash_with_open_intent(world, monkeypatch, scene)
    _tamper(scene.b)
    activity = Activity()

    _reconcile(world, activity)

    row = deploy_store.get_by_id(world.conn, op_id)
    assert (row["phase"], row["fail_reason"], row["notify_attempted"]) == (
        "needs_retry", "verify_failed", 1)
    assert _history_events(world)[-1]["to"] == "absent"
    assert [e for _, e, _ in activity.events] == ["plugin_deploy_op_needs_retry"]
    assert _reconcile(world, activity) == []
    assert len(activity.events) == 1


def _fail_source_unlink(monkeypatch, world):
    live = _live(world)
    return fail_syscall(monkeypatch, "unlink", errno_=errno.EACCES,
                        path_pred=lambda path: path == live)


@pytest.mark.parametrize("how", ["reconcile", "cli"])
def test_a_persistent_source_unlink_failure_after_a_failed_verification_stays_syscall_failed(
        world, monkeypatch, how):
    scene = RollbackScene(world, revoke=False, cascade=False)
    op_id = _crash_with_open_intent(world, monkeypatch, scene)
    _tamper(scene.b)
    links = _op_signal_links(world)
    statuses = _signal_statuses(world)
    assert links
    heal = _fail_source_unlink(monkeypatch, world)
    activity = Activity()

    first = _reconcile(world, activity)
    second = _converge(world, how, scene, activity)

    assert [r.outcome for r in first if r.outcome != "notified"] == ["needs_retry"]
    if how == "reconcile":
        assert [r.outcome for r in second if r.outcome != "notified"] == ["needs_retry"]
    else:
        assert second.outcome == "needs_retry"
    row = deploy_store.get_by_id(world.conn, op_id)
    assert (row["phase"], row["fail_reason"]) == ("needs_retry", "syscall_failed")
    assert os.readlink(_live(world)) == _link(scene.c)[1]
    assert deploy_store.get_open_by_name(world.conn, "sma") is not None
    assert _source_state(world, scene.c) == (
        "strategy_not_approved" if scene.revoke else "open_deploy_op")
    assert row["notify_attempted"] == 1
    needs_retry = ["plugin_deploy_op_needs_retry"]
    failures = 1 if how == "reconcile" else 2
    assert [e for _, e, _ in activity.events] == needs_retry * failures

    heal()
    _converge(world, how, scene, activity)

    row = deploy_store.get_by_id(world.conn, op_id)
    assert row["notify_attempted"] == 1
    assert [e for _, e, _ in activity.events] == needs_retry * (failures + 1)
    assert _op_signal_links(world) == links
    assert _signal_statuses(world) == statuses
    assert (row["phase"], row["fail_reason"]) == ("needs_retry", "verify_failed")
    assert not os.path.lexists(_live(world))
    assert op_count(world) == 1


# ---- the plugin lock is busy during the startup pass ----

def test_an_operation_skipped_for_a_busy_lock_converges_on_the_next_start(
        world, monkeypatch, tmp_path):
    scene = RollbackScene(world, revoke=False, cascade=False)
    op_id = _crash_with_open_intent(world, monkeypatch, scene)
    holder = Holder(world.root, tmp_path / "hold", ["sma"])
    try:
        results = reconcile_with_budget(world, 0.2)
        assert [r.outcome for r in results] == ["lock_timeout"]
        assert deploy_store.get_by_id(world.conn, op_id)["phase"] == "intent"
        assert os.readlink(_live(world)) == _link(scene.c)[1]
        holder.release()
    finally:
        holder.close()

    results = _reconcile(world)

    assert [r.outcome for r in results if r.outcome != "notified"] == ["done"]
    _assert_rolled_back(world, scene, op_id)


def reconcile_with_budget(world, seconds):
    return deploy_reconcile.reconcile_deploy_ops(
        world.conn, world.root, now=NOW, settings=SETTINGS, lock_timeout_sec=seconds)
