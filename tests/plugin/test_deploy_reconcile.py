from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from agentic_fx.plugin import deploy_reconcile, switch
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store import plugin_switch_journal as journal_store
from agentic_fx.store import signals as signals_store
from tests.plugin.conftest import SETTINGS
from tests.plugin.deploy_world import Activity, World, at

NOW = at(120)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _reconcile(world, activity=None):
    return deploy_reconcile.reconcile_deploy_ops(
        world.conn, world.root, now=NOW, settings=SETTINGS, activity=activity)


def _row(world, op_id):
    return deploy_store.get_by_id(world.conn, op_id)


def _state(world, op_id):
    row = _row(world, op_id)
    return row["phase"], row["fail_reason"]


def _fail(monkeypatch, name):
    def boom(*args, **kwargs):
        raise OSError(errno.EXDEV, "injected")
    monkeypatch.setattr(os, name, boom)


def _temp_links(world):
    return [p for p in world.root.iterdir() if ".link-" in p.name]


# ---- 6.1 disable_symlink ----

def test_disable_with_live_at_from_unlinks_and_completes(world):
    a, b, c = world.three_generations()
    op = world.intent("sma", op_kind="disable_symlink", from_ver=c)
    activity = Activity()

    results = _reconcile(world, activity)

    assert not os.path.lexists(world.root / "sma") and c.is_dir()
    assert _state(world, op) == ("done", None) and _row(world, op)["notify_attempted"] == 1
    assert [r.outcome for r in results] == ["done"]
    assert [e for _, e, _ in activity.events] == ["plugin_disabled"]


def test_disable_unlink_failure_stays_open_without_touching_live(world, monkeypatch):
    a, b, c = world.three_generations()
    op = world.intent("sma", op_kind="disable_symlink", from_ver=c)
    activity = Activity()
    _fail(monkeypatch, "unlink")

    _reconcile(world, activity)

    assert os.readlink(world.root / "sma") == f".versions/sma/{c.name}"
    assert _state(world, op) == ("needs_retry", "syscall_failed")
    assert deploy_store.is_open(_row(world, op))
    assert [e for _, e, _ in activity.events] == ["plugin_deploy_op_needs_retry"]


def test_disable_with_live_already_absent_completes(world):
    a, b, c = world.three_generations()
    op = world.intent("sma", op_kind="disable_symlink", from_ver=c)
    (world.root / "sma").unlink()

    _reconcile(world)

    assert _state(world, op) == ("done", None)


@pytest.mark.parametrize("kind", ["other_symlink", "plain"])
def test_disable_with_a_foreign_live_is_closed_without_touching_it(world, kind):
    a, b, c = world.three_generations()
    op = world.intent("sma", op_kind="disable_symlink", from_ver=c)
    live = world.root / "sma"
    live.unlink()
    if kind == "plain":
        live.mkdir()
    else:
        world.link("sma", a)
    before = world.snapshot("sma")[0]

    _reconcile(world)

    assert world.snapshot("sma")[0] == before
    assert _state(world, op) == ("foreign", None) and not deploy_store.is_open(_row(world, op))


def test_reconcile_retire_closes_replaced_plain_identity_as_foreign_without_renaming(world):
    original = world.version("sma", "original")
    replacement = world.version("sma", "replacement")
    original_content = world.content(original)
    os.rename(original, world.root / "sma")
    op = world.intent(
        "sma", op_kind="retire_plain", retired_path="_retired/sma-crash-1",
        subject_content_hash=original_content)
    os.rename(world.root / "sma", world.root / "old-sma")
    os.rename(replacement, world.root / "sma")

    results = _reconcile(world)

    assert results[0].outcome == "foreign" and _state(world, op) == ("foreign", None)
    assert (world.root / "sma").is_dir()
    assert not (world.root / "_retired" / "sma-crash-1").exists()


# ---- 6.2 rollback_symlink ----

def test_rollback_with_live_at_to_target_completes_without_verifying(world):
    a, b, c = world.three_generations()
    op = world.intent("sma", from_ver=c, to_ver=b)
    world.link("sma", b)
    (b / "plugin.py").write_text("tampered")

    _reconcile(world)

    assert _state(world, op) == ("done", None)
    assert os.readlink(world.root / "sma") == f".versions/sma/{b.name}"


def test_rollback_from_the_source_version_reverifies_and_swaps(world):
    a, b, c = world.three_generations()
    op = world.intent("sma", from_ver=c, to_ver=b)

    _reconcile(world)

    assert _state(world, op) == ("done", None)
    assert os.readlink(world.root / "sma") == f".versions/sma/{b.name}"
    assert not _temp_links(world)


def test_a_stale_temp_symlink_is_replaced(world):
    a, b, c = world.three_generations()
    op = world.intent("sma", from_ver=c, to_ver=b)
    (world.root / f".sma.link-d{op}").symlink_to(f".versions/sma/{a.name}")

    _reconcile(world)

    assert _state(world, op) == ("done", None)
    assert os.readlink(world.root / "sma") == f".versions/sma/{b.name}"
    assert not _temp_links(world)


def test_a_failed_verification_unlinks_the_source_and_closes_as_verify_failed(world):
    a, b, c = world.three_generations()
    op = world.intent("sma", from_ver=c, to_ver=b)
    plugin = b / "plugin.py"
    plugin.write_text(plugin.read_text() + " ")
    activity = Activity()

    results = _reconcile(world, activity)

    assert not os.path.lexists(world.root / "sma") and c.is_dir()
    assert _state(world, op) == ("needs_retry", "verify_failed")
    assert not deploy_store.is_open(_row(world, op)) and deploy_store.is_closed(_row(world, op))
    assert deploy_store.get_open_by_name(world.conn, "sma") is None
    assert [r.outcome for r in results] == ["needs_retry"]
    assert [e for _, e, _ in activity.events] == ["plugin_deploy_op_needs_retry"]
    history = deploy_store.plugin_live_history(world.conn, "sma")
    assert history[-1]["kind"] == "rollback_symlink" and history[-1]["to"] == "absent"


def test_source_unlink_failure_after_failed_verification_stays_open(world, monkeypatch):
    a, b, c = world.three_generations()
    op = world.intent("sma", from_ver=c, to_ver=b)
    plugin = b / "plugin.py"
    plugin.write_text(plugin.read_text() + " ")
    _fail(monkeypatch, "unlink")

    _reconcile(world)

    assert os.readlink(world.root / "sma") == f".versions/sma/{c.name}"
    assert _state(world, op) == ("needs_retry", "syscall_failed")
    assert deploy_store.is_open(_row(world, op))


def test_swap_failure_keeps_live_at_from_and_the_op_open(world, monkeypatch):
    a, b, c = world.three_generations()
    op = world.intent("sma", from_ver=c, to_ver=b)
    _fail(monkeypatch, "rename")

    _reconcile(world)

    assert os.readlink(world.root / "sma") == f".versions/sma/{c.name}"
    assert _state(world, op) == ("needs_retry", "syscall_failed")
    assert deploy_store.get_open_by_name(world.conn, "sma")["op_id"] == op


def test_from_symlink_with_live_absent_swaps_when_verification_passes(world):
    a, b, c = world.three_generations()
    op = world.intent("sma", from_ver=c, to_ver=b)
    (world.root / "sma").unlink()

    _reconcile(world)

    assert _state(world, op) == ("done", None)
    assert os.readlink(world.root / "sma") == f".versions/sma/{b.name}"


def test_from_symlink_with_live_absent_stays_absent_when_verification_fails(world):
    a, b, c = world.three_generations()
    op = world.intent("sma", from_ver=c, to_ver=b)
    (world.root / "sma").unlink()
    approved = world.conn.execute(
        "SELECT payload_json FROM approval_requests").fetchall()
    assert approved
    (b / "config.yaml").write_text("kind: indicator\noutputs: [value, extra]\n")

    _reconcile(world)

    assert not os.path.lexists(world.root / "sma")
    assert _state(world, op) == ("needs_retry", "verify_failed")


def test_from_absent_swaps_or_stays_absent_by_verification(world):
    a, b, c = world.three_generations()
    world.intent("sma", op_kind="disable_symlink", from_ver=c, phase="done", notify=True,
                 minute=5)
    ok = world.intent("sma", from_ver=None, to_ver=b)
    (world.root / "sma").unlink()

    _reconcile(world)

    assert _state(world, ok) == ("done", None)
    assert os.readlink(world.root / "sma") == f".versions/sma/{b.name}"


def test_from_absent_with_a_failed_verification_stays_absent(world):
    a, b, c = world.three_generations()
    world.intent("sma", op_kind="disable_symlink", from_ver=c, phase="done", notify=True,
                 minute=5)
    op = world.intent("sma", from_ver=None, to_ver=b)
    (world.root / "sma").unlink()
    world.conn.execute("UPDATE approval_requests SET status='rejected' "
                       "WHERE payload_json LIKE ?", (f"%{b.name}%",))
    world.conn.commit()

    _reconcile(world)

    assert not os.path.lexists(world.root / "sma")
    assert _state(world, op) == ("needs_retry", "verify_failed")


def test_a_third_party_symlink_is_foreign_and_left_alone(world):
    a, b, c = world.three_generations()
    op = world.intent("sma", from_ver=c, to_ver=b)
    world.link("sma", a)

    _reconcile(world)

    assert os.readlink(world.root / "sma") == f".versions/sma/{a.name}"
    assert _state(world, op) == ("foreign", None)


def test_a_plain_directory_in_place_of_the_live_link_is_foreign(world):
    a, b, c = world.three_generations()
    op = world.intent("sma", from_ver=c, to_ver=b)
    (world.root / "sma").unlink()
    (world.root / "sma").mkdir()

    _reconcile(world)

    assert _state(world, op) == ("foreign", None)
    assert (world.root / "sma").is_dir()


def _strategy_world(world):
    ind = world.version("ind", "i1")
    world.deploy("ind", ind, None, 1)
    s1 = world.version("strat", "s1", kind="strategy")
    s2 = world.version("strat", "s2", kind="strategy", indicator="ind",
                       pin=world.content(ind))
    world.deploy("strat", s1, None, 2)
    world.deploy("strat", s2, s1, 3)
    world.link("strat", s1)
    return ind, s1, s2


@pytest.mark.parametrize("force", [False, True])
def test_a_dependency_that_changed_after_the_intent_fails_verification_even_with_force(
        world, force):
    ind, s1, s2 = _strategy_world(world)
    op = world.intent("strat", from_ver=s1, to_ver=s2, force=force)
    ind2 = world.version("ind", "i2")
    world.deploy("ind", ind2, ind, 4)

    _reconcile(world)

    assert _state(world, op) == ("needs_retry", "verify_failed")
    assert not os.path.lexists(world.root / "strat")


def test_a_newly_unresolvable_dependent_is_cascaded_before_the_swap(world, monkeypatch):
    a, b, c = world.three_generations("ind")
    strat = world.version("strat", "s", kind="strategy", indicator="ind",
                          pin=world.content(c))
    world.deploy("strat", strat, None, 5)
    pending = world.signal("strat", world.content(strat))
    claimed = world.signal("strat", world.content(strat), claimed=True)
    op = world.intent("ind", from_ver=c, to_ver=b, force=True)
    calls = []
    real = signals_store.abandon_pending_for_identities

    def spy(conn, identities, **kwargs):
        calls.append((conn.in_transaction, os.readlink(world.root / "ind"), list(identities)))
        return real(conn, identities, **kwargs)

    monkeypatch.setattr(signals_store, "abandon_pending_for_identities", spy)
    _reconcile(world)

    assert _state(world, op) == ("done", None)
    assert os.readlink(world.root / "ind") == f".versions/ind/{b.name}"
    assert calls == [(True, f".versions/ind/{c.name}", [("strat", world.content(strat))])]
    assert dict(world.conn.execute("SELECT id,status FROM signals").fetchall()) == {
        pending: "abandoned", claimed: "claimed"}
    assert {tuple(r) for r in world.conn.execute(
        "SELECT signal_id,status_at_link FROM plugin_deploy_op_signals WHERE op_id=?",
        (op,))} == {(pending, "pending"), (claimed, "claimed")}


def test_a_newly_unresolvable_dependent_without_force_stops_the_rollback(world):
    a, b, c = world.three_generations("ind")
    strat = world.version("strat", "s", kind="strategy", indicator="ind",
                          pin=world.content(c))
    world.deploy("strat", strat, None, 5)
    pending = world.signal("strat", world.content(strat))
    op = world.intent("ind", from_ver=c, to_ver=b, force=False)

    _reconcile(world)

    assert _state(world, op) == ("needs_retry", "verify_failed")
    assert not os.path.lexists(world.root / "ind")
    assert world.conn.execute("SELECT status FROM signals WHERE id=?",
                              (pending,)).fetchone()[0] == "pending"


# ---- 6.3 retire_plain ----

def _plain_op(world, minute=10):
    retired = "_retired/sma-20261016-1"
    return world.intent("sma", op_kind="retire_plain", retired_path=retired), retired


def _plain_live(world):
    live = world.root / "sma"
    live.mkdir()
    (live / "plugin.py").write_text("x")
    (world.root / "_retired").mkdir(exist_ok=True)
    return live


def test_retire_plain_renames_the_live_directory(world):
    op, retired = _plain_op(world)
    live = _plain_live(world)

    _reconcile(world)

    assert _state(world, op) == ("done", None)
    assert not live.exists() and (world.root / retired / "plugin.py").is_file()


def test_retire_plain_with_the_directory_already_moved_completes(world):
    op, retired = _plain_op(world)
    (world.root / retired).mkdir(parents=True)

    _reconcile(world)

    assert _state(world, op) == ("done", None)


def test_retire_plain_collision_is_foreign_and_touches_nothing(world):
    op, retired = _plain_op(world)
    live = _plain_live(world)
    (world.root / retired).mkdir()

    _reconcile(world)

    assert _state(world, op) == ("foreign", None)
    assert (live / "plugin.py").is_file()


def test_retire_plain_with_everything_gone_is_foreign(world):
    op, _retired = _plain_op(world)

    _reconcile(world)

    assert _state(world, op) == ("foreign", None)


def test_retire_plain_with_a_symlink_in_place_is_foreign(world):
    a, b, c = world.three_generations()
    op, _retired = _plain_op(world)

    _reconcile(world)

    assert _state(world, op) == ("foreign", None)
    assert os.path.islink(world.root / "sma")


def test_retire_plain_rename_failure_stays_open_and_moves_nothing(world, monkeypatch):
    op, retired = _plain_op(world)
    live = _plain_live(world)
    _fail(monkeypatch, "rename")

    _reconcile(world)

    assert live.is_dir() and not (world.root / retired).exists()
    assert _state(world, op) == ("needs_retry", "syscall_failed")
    assert deploy_store.is_open(_row(world, op))


def test_the_rename_call_is_isolated_in_one_replaceable_function(world, monkeypatch):
    op, retired = _plain_op(world)
    live = _plain_live(world)
    seen = []
    monkeypatch.setattr(deploy_reconcile, "rename_plain_dir",
                        lambda src, dst: seen.append((Path(src), Path(dst))))

    _reconcile(world)

    assert seen == [(live, world.root / retired)]


# ---- notifications, conflicts, wiring ----

def test_a_finished_op_whose_notification_was_never_attempted_is_notified_once(world):
    a, b, c = world.three_generations()
    missed = world.intent("sma", op_kind="disable_symlink", from_ver=c, phase="done",
                          notify=False)
    done = world.intent("sma", from_ver=None, to_ver=b, phase="done", notify=True, minute=11)
    failed = world.intent("sma", from_ver=c, to_ver=b, phase="needs_retry",
                          fail_reason="verify_failed", notify=False, minute=12)
    activity = Activity()

    first = _reconcile(world, activity)
    second = _reconcile(world, activity)

    assert sorted(r.op_id for r in first) == [missed, failed]
    assert all(r.outcome == "notified" for r in first) and second == []
    assert [e for _, e, _ in activity.events] == [
        "plugin_disabled", "plugin_deploy_op_needs_retry"]
    assert _row(world, missed)["notify_attempted"] == 1
    assert _row(world, done)["notify_attempted"] == 1


def test_both_an_open_deploy_op_and_an_open_switch_journal_are_left_untouched(world):
    a, b, c = world.three_generations()
    op = world.intent("sma", op_kind="disable_symlink", from_ver=c)
    journal = world.journal("sma", "approve", c, a, 20)
    journal_store.set_phase(world.conn, journal, "switched", now=at(21))
    activity = Activity()
    before = world.snapshot("sma")

    results = _reconcile(world, activity)

    assert [r.outcome for r in results] == ["conflict"]
    assert world.snapshot("sma") == before
    assert _state(world, op) == ("intent", None)
    assert [e for _, e, _ in activity.events] == ["plugin_deploy_op_conflict"]


def test_an_op_that_is_not_open_is_not_reconciled(world):
    a, b, c = world.three_generations()
    op = world.intent("sma", op_kind="disable_symlink", from_ver=c, phase="foreign",
                      notify=True)

    assert _reconcile(world) == []
    assert os.path.islink(world.root / "sma") and _state(world, op) == ("foreign", None)


def test_the_new_modules_are_not_reachable_from_tools_or_ops():
    src = Path(__file__).resolve().parents[2] / "src" / "agentic_fx"
    hits = []
    for package in ("tools", "ops"):
        for path in (src / package).rglob("*.py"):
            text = path.read_text()
            if "deploy_rollback" in text or "deploy_reconcile" in text:
                hits.append(str(path))
    assert hits == []


# ---- journals of dependents, lock deadline, vanishing live ----

def test_an_open_switch_journal_of_a_dependent_leaves_the_op_untouched(world):
    a, b, c = world.three_generations("ind")
    strat = world.version("strat", "s", kind="strategy", indicator="ind",
                          pin=world.content(c))
    world.deploy("strat", strat, None, 5)
    op = world.intent("ind", from_ver=c, to_ver=b, force=True)
    journal = world.journal("strat", "approve", None, strat, 20)
    journal_store.set_phase(world.conn, journal, "switched", now=at(21))
    activity = Activity()

    results = _reconcile(world, activity)

    assert [r.outcome for r in results] == ["conflict"]
    assert os.readlink(world.root / "ind") == f".versions/ind/{c.name}"
    assert _state(world, op) == ("intent", None)
    assert [e for _, e, _ in activity.events] == ["plugin_deploy_op_conflict"]


def test_a_name_lock_held_elsewhere_skips_only_that_op(world):
    a, b, c = world.three_generations()
    x = world.version("other", "x")
    world.deploy("other", x, None, 6)
    x2 = world.version("other", "x2")
    world.deploy("other", x2, x, 7)
    busy = world.intent("sma", from_ver=c, to_ver=b)
    free = world.intent("other", from_ver=x2, to_ver=x)
    activity = Activity()

    with switch._plugin_lock(world.root, "sma"):
        results = deploy_reconcile.reconcile_deploy_ops(
            world.conn, world.root, now=NOW, settings=SETTINGS, activity=activity,
            lock_timeout_sec=0.2)

    assert {r.name: r.outcome for r in results} == {"sma": "lock_timeout", "other": "done"}
    assert _state(world, busy) == ("intent", None)
    assert _state(world, free) == ("done", None)
    assert os.readlink(world.root / "sma") == f".versions/sma/{c.name}"
    assert "plugin_deploy_op_lock_timeout" in [e for _, e, _ in activity.events]

    again = _reconcile(world)
    assert [r.outcome for r in again] == ["done"] and _state(world, busy) == ("done", None)


def test_an_unexpected_error_in_one_op_does_not_stop_the_next(world, monkeypatch):
    a, b, c = world.three_generations()
    x = world.version("other", "x")
    world.deploy("other", x, None, 6)
    x2 = world.version("other", "x2")
    world.deploy("other", x2, x, 7)
    broken = world.intent("other", from_ver=x2, to_ver=x)
    healthy = world.intent("sma", from_ver=c, to_ver=b)
    real = deploy_reconcile._reconcile_one

    def flaky(conn, root, row, **kwargs):
        if row["name"] == "other":
            raise RuntimeError("injected")
        return real(conn, root, row, **kwargs)

    monkeypatch.setattr(deploy_reconcile, "_reconcile_one", flaky)
    results = _reconcile(world)

    assert [(r.name, r.outcome) for r in results] == [
        ("other", "error"), ("sma", "done")]
    assert _state(world, broken) == ("intent", None)
    assert _state(world, healthy) == ("done", None)


def test_the_lock_wait_budget_is_one_deadline_shared_by_every_op(world, monkeypatch):
    a, b, c = world.three_generations()
    x = world.version("other", "x")
    world.deploy("other", x, None, 6)
    x2 = world.version("other", "x2")
    world.deploy("other", x2, x, 7)
    world.intent("sma", from_ver=c, to_ver=b)
    world.intent("other", from_ver=x2, to_ver=x)
    deadlines = []
    real_acquire = deploy_reconcile.deploy_ops.acquire_op_locks

    def spy(*args, **kwargs):
        deadlines.append(kwargs["deadline"])
        return real_acquire(*args, **kwargs)

    monkeypatch.setattr(deploy_reconcile.deploy_ops, "acquire_op_locks", spy)
    readings = iter([-1e6, -2e6, -3e6, -4e6])
    calls = []

    def clock():
        calls.append(1)
        return next(readings)

    with switch._plugin_lock(world.root, "sma"), switch._plugin_lock(world.root, "other"):
        results = deploy_reconcile.reconcile_deploy_ops(
            world.conn, world.root, now=NOW, settings=SETTINGS, activity=Activity(),
            lock_timeout_sec=30.0, monotonic=clock)

    assert [r.outcome for r in results] == ["lock_timeout", "lock_timeout"]
    assert len(calls) == 1
    assert deadlines == [-1e6 + 30.0, -1e6 + 30.0]


def _unlink_then_vanish(monkeypatch):
    real = os.unlink

    def vanish(path, *args, **kwargs):
        real(path, *args, **kwargs)
        raise FileNotFoundError(errno.ENOENT, "gone")

    monkeypatch.setattr(os, "unlink", vanish)


def _readlink_then_vanish(monkeypatch):
    real = os.readlink
    first = True

    def vanish(path, *args, **kwargs):
        nonlocal first
        if first:
            first = False
            os.unlink(path)
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", vanish)


def test_a_disable_whose_live_vanishes_before_readlink_completes(world, monkeypatch):
    a, b, c = world.three_generations()
    op = world.intent("sma", op_kind="disable_symlink", from_ver=c)
    monkeypatch.setattr(deploy_reconcile, "_row_dependents", lambda *args: {"sma"})
    _readlink_then_vanish(monkeypatch)

    results = _reconcile(world)

    assert _state(world, op) == ("done", None)
    assert [result.outcome for result in results] == ["done"]
    assert "error" not in [result.outcome for result in results]


def test_a_rollback_whose_live_vanishes_before_readlink_uses_absent_path(world, monkeypatch):
    a, b, c = world.three_generations()
    op = world.intent("sma", from_ver=c, to_ver=b)
    monkeypatch.setattr(deploy_reconcile, "_row_dependents", lambda *args: {"sma"})
    _readlink_then_vanish(monkeypatch)

    results = _reconcile(world)

    assert _state(world, op) == ("done", None)
    assert os.readlink(world.root / "sma") == f".versions/sma/{b.name}"
    assert [result.outcome for result in results] == ["done"]
    assert "error" not in [result.outcome for result in results]


def test_a_disable_whose_live_vanishes_before_the_unlink_is_done(world, monkeypatch):
    a, b, c = world.three_generations()
    op = world.intent("sma", op_kind="disable_symlink", from_ver=c)
    _unlink_then_vanish(monkeypatch)

    _reconcile(world)

    assert _state(world, op) == ("done", None)


def test_a_rollback_source_that_vanishes_before_the_unlink_still_closes_as_verify_failed(
        world, monkeypatch):
    a, b, c = world.three_generations()
    op = world.intent("sma", from_ver=c, to_ver=b)
    plugin = b / "plugin.py"
    plugin.write_text(plugin.read_text() + " ")
    _unlink_then_vanish(monkeypatch)

    _reconcile(world)

    assert _state(world, op) == ("needs_retry", "verify_failed")
    assert not os.path.lexists(world.root / "sma")
