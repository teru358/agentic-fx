from __future__ import annotations

import os

import pytest

from agentic_fx.plugin import deploy_ops, deploy_reconcile, deploy_retire
from agentic_fx.store import plugin_deploy_ops as deploy_store
from tests.plugin._crash import (
    CrashPoint,
    crash_at,
    crash_after_intent_commit,
    crash_after_rename_plain,
    crash_before_rename_plain,
    crash_before_set_phase,
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


NOW = at(182)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _retire(world):
    return deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)


def test_a_crash_before_plain_retire_intent_recording_leaves_no_durable_effect(world, monkeypatch):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")
    before = _stable(world)
    crash_at(monkeypatch, deploy_retire.deploy_ops, "record_intent")

    crashed(world, _retire, world)

    assert _stable(world) == before
    assert not _retired_entries(world)
    assert reconcile(world, SETTINGS, now=NOW) == []


def test_a_crash_after_plain_retire_intent_commit_is_completed_by_reconcile(world, monkeypatch):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")
    crash_after_intent_commit(monkeypatch)
    crashed(world, _retire, world)
    row = deploy_store.get_open_by_name(world.conn, "sma")

    deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)

    assert row is not None
    reread = deploy_store.get_by_id(world.conn, row["op_id"])
    assert reread["phase"] == "done" and (world.root / reread["retired_path"]).is_dir()


def test_a_crash_after_plain_retire_rename_is_completed_by_reconcile(world, monkeypatch):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")
    crash_before_set_phase(monkeypatch, "done")
    crashed(world, _retire, world)
    row = deploy_store.get_open_by_name(world.conn, "sma")

    deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)

    assert row is not None
    assert deploy_store.get_by_id(world.conn, row["op_id"])["phase"] == "done"


def test_a_reconcile_crash_before_plain_retire_rename_is_retried_on_the_next_start(world, monkeypatch):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")
    crash_after_intent_commit(monkeypatch)
    crashed(world, _retire, world)
    crash_before_rename_plain(monkeypatch)

    with pytest.raises(CrashPoint):
        deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)
    restart(world)

    deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)
    row = deploy_store.list_by_name(world.conn, "sma")[0]
    assert row["phase"] == "done" and not os.path.lexists(world.root / "sma")


def test_a_reconcile_crash_after_plain_retire_rename_is_completed_on_the_next_start(world, monkeypatch):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")
    crash_after_intent_commit(monkeypatch)
    crashed(world, _retire, world)
    crash_after_rename_plain(monkeypatch)

    with pytest.raises(CrashPoint):
        deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)
    restart(world)

    deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)
    row = deploy_store.list_by_name(world.conn, "sma")[0]
    assert row["phase"] == "done" and not os.path.lexists(world.root / "sma")


# ---- scenes with a cascade, both routes, and the states between ----

ROUTES = ["reconcile", "cli"]


def _plain_scene(world, cascade: bool):
    os.rename(world.version("sma", "plain"), world.root / "sma")
    signals = [world.signal("sma", "any-content"), world.signal("sma", "any-content", claimed=True)]
    if cascade:
        strategy = world.version("strat", "strategy", kind="strategy", indicator="sma",
                                 pin="a" * 64)
        world.deploy("strat", strategy, None, 1)
        signals.append(world.signal("strat", world.content(strategy)))
    return signals


def _retire_forced(world, cascade: bool, **kwargs):
    return deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=cascade, now=NOW, settings=SETTINGS, **kwargs)


def _retired_entries(world) -> list[str]:
    root = world.root / "_retired"
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def _stable(world):
    """The snapshot without the empty ``_retired`` directory a retire may create first."""
    live, listing, *tables = world.snapshot("sma")
    return (live, [name for name in listing if name != "_retired"], *tables)


def _complete(world, cascade, route):
    if route == "reconcile":
        return reconcile(world, SETTINGS, now=NOW)
    return _retire_forced(world, cascade)


def _open_retire(world, monkeypatch, origin: str, cascade: bool = False):
    _plain_scene(world, cascade)
    if origin == "intent":
        crash_after_intent_commit(monkeypatch)
        crashed(world, _retire_forced, world, cascade)
    else:
        live = world.root / "sma"
        heal = fail_syscall(monkeypatch, "rename", path_pred=lambda path: path == live)
        failed = _retire_forced(world, cascade)
        assert failed.outcome == "needs_retry"
        heal()
    op = deploy_store.get_open_by_name(world.conn, "sma")
    assert op is not None
    return op


@pytest.mark.parametrize("cascade", [False, True], ids=["plain", "cascade"])
@pytest.mark.parametrize("stage", ["after_insert", "after_destination", "after_name_abandon",
                                   "after_cascade"])
def test_a_crash_inside_the_plain_retire_intent_transaction_leaves_every_table_untouched(
        world, monkeypatch, stage, cascade):
    _plain_scene(world, cascade)
    before = (_stable(world), db_effects(world))
    if stage == "after_destination":
        crash_at(monkeypatch, deploy_store, "set_retired_path", when="after")
    else:
        crash_inside_record_intent(monkeypatch, {
            "after_insert": "after_insert",
            "after_name_abandon": "after_hook",
            "after_cascade": "after_cascade",
        }[stage])

    crashed(world, _retire_forced, world, cascade)

    assert (_stable(world), db_effects(world)) == before
    assert not _retired_entries(world)
    assert reconcile(world, SETTINGS, now=NOW) == []

    done = _retire_forced(world, cascade)

    assert done.outcome == "done" and op_count(world) == 1


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("cascade", [False, True], ids=["plain", "cascade"])
def test_a_crash_after_the_plain_retire_intent_commit_is_completed_by_either_route(
        world, monkeypatch, cascade, route):
    _plain_scene(world, cascade)
    crash_after_intent_commit(monkeypatch)
    crashed(world, _retire_forced, world, cascade)
    op = deploy_store.get_open_by_name(world.conn, "sma")
    assert op["phase"] == "intent" and (world.root / "sma").is_dir()
    assert not (world.root / op["retired_path"]).exists()
    assert [r[0] for r in world.conn.execute(
        "SELECT status FROM signals WHERE plugin='sma' ORDER BY id")] == ["abandoned", "claimed"]
    if cascade:
        assert world.conn.execute(
            "SELECT status FROM signals WHERE plugin='strat'").fetchone()[0] == "abandoned"
    before = db_effects(world)

    result = _complete(world, cascade, route)

    if route == "cli":
        assert result.outcome == "done" and result.op_id == op["op_id"]
    assert deploy_store.get_by_id(world.conn, op["op_id"])["phase"] == "done"
    assert (world.root / op["retired_path"]).is_dir() and not os.path.lexists(world.root / "sma")
    assert op_count(world) == 1 and db_effects(world)[1:] == before[1:]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("cascade", [False, True], ids=["plain", "cascade"])
def test_a_crash_between_the_plain_retire_rename_and_the_done_phase_is_completed_by_either_route(
        world, monkeypatch, cascade, route):
    _plain_scene(world, cascade)
    crash_before_set_phase(monkeypatch, "done")
    crashed(world, _retire_forced, world, cascade)
    op = deploy_store.get_open_by_name(world.conn, "sma")
    assert not os.path.lexists(world.root / "sma") and (world.root / op["retired_path"]).is_dir()
    before = db_effects(world)

    result = _complete(world, cascade, route)

    if route == "cli":
        assert result.outcome == "done" and result.op_id == op["op_id"]
    assert deploy_store.get_by_id(world.conn, op["op_id"])["phase"] == "done"
    assert op_count(world) == 1 and db_effects(world)[1:] == before[1:]


@pytest.mark.parametrize("origin", ["intent", "failed_rename"])
def test_a_destination_created_by_a_third_party_is_refused_by_the_cli_and_closed_foreign_by_reconcile(
        world, monkeypatch, origin):
    op = _open_retire(world, monkeypatch, origin)
    destination = world.root / op["retired_path"]
    destination.mkdir(parents=True)
    before = db_effects(world)

    refused = _retire_forced(world, False)

    assert refused.outcome == "refused" and refused.op_id == op["op_id"]
    assert str(op["op_id"]) in refused.reason and op["retired_path"] in refused.reason
    assert db_effects(world) == before and (world.root / "sma").is_dir()
    assert deploy_store.get_open_by_name(world.conn, "sma")["op_id"] == op["op_id"]

    results = reconcile(world, SETTINGS, now=NOW)

    assert [(r.op_id, r.outcome) for r in results if r.outcome != "notified"] == [
        (op["op_id"], "foreign")]
    assert (world.root / "sma").is_dir() and not list(destination.iterdir())
    assert deploy_store.get_open_by_name(world.conn, "sma") is None


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("origin", ["intent", "failed_rename"])
def test_a_plain_directory_replaced_after_the_intent_is_closed_foreign_by_either_route(
        world, monkeypatch, origin, route):
    replacement = world.version("sma", "replacement")
    op = _open_retire(world, monkeypatch, origin)
    os.rename(world.root / "sma", world.root / "old-sma")
    os.rename(replacement, world.root / "sma")

    result = _complete(world, False, route)

    if route == "cli":
        assert result.outcome == "foreign"
    else:
        assert [(r.op_id, r.outcome) for r in result if r.outcome != "notified"] == [
            (op["op_id"], "foreign")]
    assert deploy_store.get_by_id(world.conn, op["op_id"])["phase"] == "foreign"
    assert (world.root / "sma").is_dir() and not (world.root / op["retired_path"]).exists()


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("cascade", [False, True], ids=["plain", "cascade"])
def test_a_plain_retire_whose_rename_keeps_failing_stays_open_and_moves_nothing_until_it_heals(
        world, monkeypatch, cascade, route):
    _plain_scene(world, cascade)
    live = world.root / "sma"
    heal = fail_syscall(monkeypatch, "rename", path_pred=lambda path: path == live)
    activity = Activity()

    first = _retire_forced(world, cascade, activity=activity)
    kept = db_effects(world)[1:]
    starts = [reconcile(world, SETTINGS, now=NOW, activity=activity) for _ in range(2)]

    row = deploy_store.get_by_id(world.conn, first.op_id)
    assert first.outcome == "needs_retry"
    assert (row["phase"], row["fail_reason"]) == ("needs_retry", "syscall_failed")
    assert [[(r.op_id, r.outcome, r.fail_reason) for r in start] for start in starts] == [
        [(first.op_id, "needs_retry", "syscall_failed")]] * 2
    assert db_effects(world)[1:] == kept
    assert live.is_dir() and not (world.root / row["retired_path"]).exists()
    assert [event for _, event, _ in activity.events] == ["plugin_deploy_op_needs_retry"]
    with pytest.raises(deploy_ops.SignatureMismatchError):
        _retire_forced(world, not cascade)

    heal()
    result = _complete(world, cascade, route)

    if route == "cli":
        assert result.outcome == "done" and result.op_id == first.op_id
    assert deploy_store.get_by_id(world.conn, first.op_id)["phase"] == "done"
    assert (world.root / row["retired_path"]).is_dir() and not os.path.lexists(live)
    assert op_count(world) == 1 and db_effects(world)[1:] == kept


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("change", ["dependent_removed", "dependent_added"])
def test_a_dependency_change_after_the_plain_retire_intent_does_not_stop_it(
        world, monkeypatch, change, route):
    cascade = change == "dependent_removed"
    _plain_scene(world, cascade)
    crash_after_intent_commit(monkeypatch)
    crashed(world, _retire_forced, world, cascade)
    op = deploy_store.get_open_by_name(world.conn, "sma")
    if change == "dependent_removed":
        os.unlink(world.root / "strat")
    else:
        extra = world.version("extra", "strategy", kind="strategy", indicator="sma",
                              pin="a" * 64)
        world.deploy("extra", extra, None, 5)

    result = _complete(world, cascade, route)

    if route == "cli":
        assert result.outcome == "done"
    assert deploy_store.get_by_id(world.conn, op["op_id"])["phase"] == "done"
    assert (world.root / op["retired_path"]).is_dir()
