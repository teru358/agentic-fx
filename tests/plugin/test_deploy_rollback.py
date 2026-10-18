from __future__ import annotations

import os

import pytest

from agentic_fx.plugin import deploy_ops, deploy_rollback
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store import signals as signals_store
from tests.plugin.conftest import SETTINGS
from tests.plugin.deploy_world import Activity, World, at

NOW = at(60)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _resolve(world, to=None, name="sma"):
    return deploy_rollback.resolve_rollback_target(world.conn, world.root, name, to=to)


def _rollback(world, name="sma", **kwargs):
    kwargs.setdefault("to", None)
    kwargs.setdefault("revoke", False)
    kwargs.setdefault("force", False)
    return deploy_rollback.rollback_plugin(
        world.conn, world.root, name, now=NOW, settings=SETTINGS, **kwargs)


def test_rerun_after_rollback_notification_crash_recovers_the_notification_once(world, monkeypatch):
    """A completed rollback remains a no-op while claiming its missed notice."""
    _a, b, _c = world.three_generations()
    real_notify = deploy_rollback.deploy_ops.try_notify
    monkeypatch.setattr(
        deploy_rollback.deploy_ops, "try_notify",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError, match="crash"):
        _rollback(world, to=b.name)
    monkeypatch.setattr(deploy_rollback.deploy_ops, "try_notify", real_notify)

    row = deploy_store.list_by_name(world.conn, "sma")[-1]
    assert row["phase"] == "done" and row["notify_attempted"] == 0
    activity = Activity()

    rerun = _rollback(world, to=b.name, activity=activity)

    assert rerun.outcome == "refused" and rerun.refusal_code == "already_live"
    assert rerun.notified_op_ids == (row["op_id"],)
    assert [event for _, event, _ in activity.events] == ["plugin_deploy_op_done"]


def test_already_live_does_not_announce_an_older_rollback_to_the_same_target(world, monkeypatch):
    a, b, _c = world.three_generations()
    real_notify = deploy_rollback.deploy_ops.try_notify
    monkeypatch.setattr(
        deploy_rollback.deploy_ops, "try_notify",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError, match="crash"):
        _rollback(world, to=b.name)
    monkeypatch.setattr(deploy_rollback.deploy_ops, "try_notify", real_notify)
    stale = deploy_store.list_by_name(world.conn, "sma")[-1]
    assert stale["notify_attempted"] == 0
    _rollback(world, to=a.name)
    world.link("sma", b)
    activity = Activity()

    rerun = _rollback(world, to=b.name, activity=activity)

    assert rerun.outcome == "refused" and rerun.refusal_code == "already_live"
    assert rerun.notified_op_ids == ()
    assert activity.events == []
    assert deploy_store.get_by_id(world.conn, stale["op_id"])["notify_attempted"] == 0


# ---- target decision (AC-7, AC-8) ----

def test_default_target_is_the_previous_version_after_an_approval(world):
    a, b, c = world.three_generations()

    target = _resolve(world)

    assert isinstance(target, deploy_rollback.RollbackTarget)
    assert target.to_target == f".versions/sma/{b.name}"


def test_default_target_is_refused_after_a_rollback_and_to_is_required(world):
    a, b, c = world.three_generations()
    op = world.intent("sma", from_ver=c, to_ver=b, phase="done", minute=10)
    world.link("sma", b)

    refusal = _resolve(world)

    assert isinstance(refusal, deploy_rollback.RollbackRefusal)
    assert refusal.code == "target_required"
    target = _resolve(world, to=a.name[:12])
    assert target.to_target == f".versions/sma/{a.name}"


def test_default_target_is_refused_after_a_disable(world):
    a, b, c = world.three_generations()
    world.intent("sma", op_kind="disable_symlink", from_ver=c, phase="done", minute=10)
    (world.root / "sma").unlink()

    refusal = _resolve(world)

    assert refusal.code == "target_required"
    assert _resolve(world, to=b.name).to_target.endswith(b.name)


def test_default_target_is_refused_after_verify_failed_whose_effective_target_is_absent(world):
    a, b, c = world.three_generations()
    world.intent("sma", from_ver=c, to_ver=b, phase="needs_retry",
                 fail_reason="verify_failed", minute=10)
    (world.root / "sma").unlink()

    assert _resolve(world).code == "target_required"
    world.link("sma", c)
    assert _resolve(world).code == "target_required"


def test_default_target_is_refused_when_history_and_live_disagree(world):
    a, b, c = world.three_generations()
    world.link("sma", a)

    refusal = _resolve(world)

    assert refusal.code == "target_required"


def test_first_deployment_has_no_rollback_target_and_nothing_changes(world):
    a = world.version("sma", "a")
    world.deploy("sma", a, None, 1)
    before = world.snapshot("sma")

    refusal = _resolve(world)
    outcome = _rollback(world)

    assert refusal.code == "no_rollback_target" and "disable" in refusal.reason
    assert outcome.outcome == "refused" and outcome.refusal_code == "no_rollback_target"
    assert outcome.filesystem_switched is False
    assert world.snapshot("sma") == before


def test_plain_live_directory_is_refused_with_a_retire_hint(world):
    a = world.version("sma", "a")
    plain = world.root / "sma"
    plain.mkdir()

    assert _resolve(world, to=a.name).code == "live_is_plain"
    with pytest.raises(ValueError, match="retire"):
        _rollback(world, to=a.name)


def test_to_prefix_must_match_exactly_one_version_directory(world):
    a, b, c = world.three_generations()
    base = world.root / ".versions" / "sma"
    (base / ("e" * 63 + "0")).mkdir()
    (base / ("e" * 63 + "1")).mkdir()

    assert _resolve(world, to="e" * 10).code == "target_ambiguous"
    assert _resolve(world, to="f" * 10).code == "target_not_found"
    assert _resolve(world, to="XYZ").code == "bad_target"
    assert _resolve(world, to=c.name).code == "already_live"


# ---- target verification (AC-9, IV-8) ----

def _failed(result):
    return isinstance(result, deploy_rollback.VerificationFailure)


def _verify(world, target, *, force=False, current_kind="indicator", name="sma"):
    return deploy_rollback.verify_rollback_target(
        world.conn, world.root, name, target.name, settings=SETTINGS, force=force,
        current_kind=current_kind)


def test_a_verified_target_carries_its_approved_identity(world):
    a, b, c = world.three_generations()

    verified = _verify(world, b)

    assert isinstance(verified, deploy_rollback.VerifiedTarget)
    assert verified.content_hash == world.content(b) and verified.artifact_hash == b.name
    assert verified.to_target == f".versions/sma/{b.name}"


def test_one_byte_change_in_the_target_directory_is_refused_and_nothing_changes(world):
    a, b, c = world.three_generations()
    plugin = b / "plugin.py"
    plugin.write_text(plugin.read_text() + " ")
    before = world.snapshot("sma")

    reasons = _verify(world, b)
    outcome = _rollback(world)

    assert _failed(reasons) and "does not match" in reasons.reasons[0]
    assert outcome.outcome == "refused" and outcome.refusal_code == "target_verification_failed"
    assert world.snapshot("sma") == before


def test_revoked_and_unapproved_versions_are_refused(world):
    a, b, c = world.three_generations()
    approvals_store.append_revocation(world.conn, "sma", world.content(b), now=at(30))
    unapproved = world.version("sma", "u")

    revoked = _verify(world, b)
    never = _verify(world, unapproved)

    assert _failed(revoked) and "no approved decision" in revoked.reasons[0]
    assert _failed(never) and "no approved decision" in never.reasons[0]
    assert _rollback(world).outcome == "refused"


def test_a_missing_target_directory_is_refused(world):
    a, b, c = world.three_generations()
    ghost = world.root / ".versions" / "sma" / ("d" * 64)

    reasons = deploy_rollback.verify_rollback_target(
        world.conn, world.root, "sma", ghost.name, settings=SETTINGS, force=False,
        current_kind="indicator")

    assert _failed(reasons) and "does not exist" in reasons.reasons[0]


def test_a_target_of_another_kind_than_the_deployment_is_refused(world):
    a, b, c = world.three_generations()
    strategy = world.version("sma", "s", kind="strategy")
    world.approve("sma", strategy, 5)

    reasons = _verify(world, strategy)

    assert _failed(reasons) and "kind" in reasons.reasons[0]


def _strategy_world(world):
    ind = world.version("ind", "i1")
    world.deploy("ind", ind, None, 1)
    pin = world.content(ind)
    s1 = world.version("strat", "s1", kind="strategy")
    s2 = world.version("strat", "s2", kind="strategy", indicator="ind", pin=pin)
    world.deploy("strat", s1, None, 2)
    world.deploy("strat", s2, s1, 3)
    return ind, s1, s2


def test_a_target_strategy_with_unresolvable_own_dependencies_is_refused_even_with_force(world):
    ind, s1, s2 = _strategy_world(world)
    # deploy a newer indicator so the pin held by s2 no longer matches
    ind2 = world.version("ind", "i2")
    world.deploy("ind", ind2, ind, 4)
    world.link("strat", s1)

    plain = _verify(world, s2, current_kind="strategy", name="strat")
    forced = _verify(world, s2, current_kind="strategy", force=True, name="strat")
    outcome = _rollback(world, name="strat", to=s2.name, force=True)

    assert _failed(plain) and "pin_mismatch" in plain.reasons[0]
    assert _failed(forced) and "pin_mismatch" in forced.reasons[0]
    assert outcome.outcome == "refused"
    assert os.readlink(world.root / "strat") == f".versions/strat/{s1.name}"


def _dependent_world(world):
    a, b, c = world.three_generations("ind")
    strat = world.version("strat", "s", kind="strategy", indicator="ind", pin=world.content(c))
    world.deploy("strat", strat, None, 5)
    return a, b, c, strat


def test_dependents_that_would_break_block_the_rollback_unless_forced(world):
    a, b, c, strat = _dependent_world(world)
    before = world.snapshot("ind")

    blocked = _rollback(world, name="ind")

    assert blocked.outcome == "dependents_blocked"
    assert blocked.dependents == (("strat", "fast", "pin_mismatch"),)
    assert world.snapshot("ind") == before
    plain = _verify(world, b, name="ind")
    assert _failed(plain) and "dependents_unresolvable" in plain.reasons[0]
    forced = _verify(world, b, name="ind", force=True)
    assert forced.dependents.dependent_names == {"strat"}


def test_force_cascades_dependents_in_the_same_transaction_as_the_intent(world, monkeypatch):
    a, b, c, strat = _dependent_world(world)
    pending = world.signal("strat", world.content(strat))
    claimed = world.signal("strat", world.content(strat), claimed=True)
    calls = []
    real = signals_store.abandon_pending_for_identities

    def spy(conn, identities, **kwargs):
        calls.append((conn.in_transaction, sorted(identities)))
        return real(conn, identities, **kwargs)

    monkeypatch.setattr(signals_store, "abandon_pending_for_identities", spy)
    outcome = _rollback(world, name="ind", force=True)

    assert outcome.outcome == "rolled_back" and outcome.cascade_strategies == ("strat",)
    assert len(calls) == 1 and calls[0][0] is True
    assert ("strat", world.content(strat)) in calls[0][1]
    statuses = dict(world.conn.execute("SELECT id,status FROM signals").fetchall())
    assert statuses == {pending: "abandoned", claimed: "claimed"}
    op_id = deploy_store.list_by_name(world.conn, "ind")[0]["op_id"]
    assert {tuple(r) for r in world.conn.execute(
        "SELECT signal_id,status_at_link FROM plugin_deploy_op_signals WHERE op_id=?",
        (op_id,))} == {(pending, "pending"), (claimed, "claimed")}


def test_a_failing_cascade_leaves_neither_an_intent_nor_a_switch(world, monkeypatch):
    a, b, c, strat = _dependent_world(world)
    monkeypatch.setattr(signals_store, "abandon_pending_for_identities",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("abort")))
    before = world.snapshot("ind")

    with pytest.raises(RuntimeError, match="abort"):
        _rollback(world, name="ind", force=True)

    assert world.snapshot("ind") == before


# ---- rollback execution (AC-10, AC-31) ----

def test_rollback_switches_the_symlink_and_reports_a_pending_restart(world):
    a, b, c = world.three_generations()
    activity = Activity()

    outcome = _rollback(world, activity=activity)

    live = world.root / "sma"
    assert outcome.outcome == "rolled_back"
    assert outcome.filesystem_switched is True
    assert outcome.runtime_activation == "pending_restart"
    assert os.readlink(live) == f".versions/sma/{b.name}"
    assert not [p for p in world.root.iterdir() if ".link-" in p.name]
    assert c.is_dir()
    row = deploy_store.list_by_name(world.conn, "sma")[0]
    assert row["op_kind"] == "rollback_symlink" and row["phase"] == "done"
    assert row["from_kind"] == "symlink" and row["notify_attempted"] == 1
    assert row["subject_artifact_hash"] == c.name and row["target_artifact_hash"] == b.name
    assert row["approval_id"] is not None and row["revoke"] == 0
    assert [e for _, e, _ in activity.events] == ["plugin_deploy_op_done"]


def test_rollback_from_an_absent_live_uses_the_explicit_target(world):
    a, b, c = world.three_generations()
    world.intent("sma", op_kind="disable_symlink", from_ver=c, phase="done", minute=10)
    (world.root / "sma").unlink()

    refused = _rollback(world)
    outcome = _rollback(world, to=b.name[:10])

    assert refused.outcome == "refused" and refused.refusal_code == "target_required"
    assert outcome.outcome == "rolled_back"
    assert os.readlink(world.root / "sma") == f".versions/sma/{b.name}"
    row = deploy_store.list_by_name(world.conn, "sma")[-1]
    assert row["from_kind"] == "absent" and row["subject_artifact_hash"] is None
    assert row["approval_id"] is None


def test_revoke_rejects_the_version_being_removed_and_abandons_its_pending_signals(world):
    a, b, c = world.three_generations()
    pending = world.signal("sma", world.content(c))
    other = world.signal("sma", world.content(b))

    outcome = _rollback(world, revoke=True)

    assert outcome.outcome == "rolled_back"
    assert approvals_store.latest_decision_for_identity(
        world.conn, "sma", world.content(c))["status"] == "rejected"
    assert approvals_store.latest_decision_for_identity(
        world.conn, "sma", world.content(b))["status"] == "approved"
    statuses = dict(world.conn.execute("SELECT id,status FROM signals").fetchall())
    assert statuses == {pending: "abandoned", other: "pending"}
    assert deploy_store.list_by_name(world.conn, "sma")[0]["revoke"] == 1


def test_revoke_is_rejected_without_a_live_version_or_an_approved_subject(world):
    a, b, c = world.three_generations()
    approvals_store.append_revocation(world.conn, "sma", world.content(c), now=at(30))
    before = world.snapshot("sma")
    with pytest.raises(ValueError, match="no approved version"):
        _rollback(world, to=b.name, revoke=True)
    assert world.snapshot("sma") == before

    (world.root / "sma").unlink()
    with pytest.raises(ValueError, match="revoke --version"):
        _rollback(world, to=b.name, revoke=True)


def test_dry_run_changes_nothing(world):
    a, b, c = world.three_generations()
    before = world.snapshot("sma")

    outcome = _rollback(world, dry_run=True)

    assert outcome.outcome == "dry_run" and outcome.to_target == f".versions/sma/{b.name}"
    assert outcome.filesystem_switched is False
    assert world.snapshot("sma") == before


def _fail_rename(monkeypatch):
    monkeypatch.setattr(os, "rename", lambda *a, **k: (_ for _ in ()).throw(OSError("EXDEV")))


def test_a_failing_swap_leaves_live_unchanged_and_the_op_open_for_a_rerun(world, monkeypatch):
    a, b, c = world.three_generations()
    activity = Activity()
    with monkeypatch.context() as patch:
        _fail_rename(patch)
        outcome = _rollback(world, activity=activity)

    assert outcome.outcome == "needs_retry" and outcome.filesystem_switched is False
    assert os.readlink(world.root / "sma") == f".versions/sma/{c.name}"
    op = deploy_store.get_open_by_name(world.conn, "sma")
    assert op["phase"] == "needs_retry" and op["fail_reason"] == "syscall_failed"
    assert [e for _, e, _ in activity.events] == ["plugin_deploy_op_needs_retry"]

    again = _rollback(world, activity=activity)

    assert again.outcome == "rolled_back" and again.op_id == op["op_id"]
    assert os.readlink(world.root / "sma") == f".versions/sma/{b.name}"
    assert len(deploy_store.list_by_name(world.conn, "sma")) == 1
    assert deploy_store.get_by_id(world.conn, op["op_id"])["phase"] == "done"
    assert [e for _, e, _ in activity.events] == [
        "plugin_deploy_op_needs_retry", "plugin_deploy_op_done"]


def _failed_open_op(world, monkeypatch, **kwargs):
    with monkeypatch.context() as patch:
        _fail_rename(patch)
        assert _rollback(world, **kwargs).outcome == "needs_retry"
    return deploy_store.get_open_by_name(world.conn, "sma")


@pytest.mark.parametrize("changed", ["to", "revoke", "force"])
def test_a_rerun_with_a_different_signature_is_rejected_and_changes_nothing(
        world, monkeypatch, changed):
    a, b, c = world.three_generations()
    op = _failed_open_op(world, monkeypatch, to=b.name)
    before = world.snapshot("sma")
    kwargs = {"to": b.name, "revoke": False, "force": False}
    kwargs.update({"to": a.name} if changed == "to" else
                  {"revoke": True} if changed == "revoke" else {"force": True})

    with pytest.raises(deploy_ops.SignatureMismatchError, match=str(op["op_id"])):
        _rollback(world, **kwargs)

    assert world.snapshot("sma") == before


def test_a_rerun_that_drops_an_original_revoke_is_rejected(world, monkeypatch):
    a, b, c = world.three_generations()
    op = _failed_open_op(world, monkeypatch, revoke=True)
    before = world.snapshot("sma")

    with pytest.raises(deploy_ops.SignatureMismatchError):
        _rollback(world, revoke=False)

    assert world.snapshot("sma") == before
    assert _rollback(world, revoke=True).op_id == op["op_id"]


def test_a_refused_precheck_does_not_touch_an_open_op(world, monkeypatch):
    a, b, c = world.three_generations()
    op = _failed_open_op(world, monkeypatch)
    before = world.snapshot("sma")

    outcome = _rollback(world, to="ffff")

    assert outcome.outcome == "refused" and outcome.refusal_code == "target_not_found"
    assert world.snapshot("sma") == before
    reread = deploy_store.get_by_id(world.conn, op["op_id"])
    assert (reread["phase"], reread["fail_reason"]) == ("needs_retry", "syscall_failed")


def test_a_rerun_after_the_swap_but_before_done_completes_without_touching_the_fs(world):
    a, b, c = world.three_generations()
    op_id = world.intent("sma", from_ver=c, to_ver=b)
    world.link("sma", b)
    activity = Activity()

    with pytest.raises(deploy_ops.SignatureMismatchError):
        _rollback(world, to=a.name)
    outcome = _rollback(world, to=b.name, activity=activity)

    assert outcome.outcome == "rolled_back" and outcome.op_id == op_id
    assert os.readlink(world.root / "sma") == f".versions/sma/{b.name}"
    row = deploy_store.get_by_id(world.conn, op_id)
    assert row["phase"] == "done" and row["notify_attempted"] == 1


def test_another_open_op_kind_blocks_a_rollback(world):
    a, b, c = world.three_generations()
    world.intent("sma", op_kind="disable_symlink", from_ver=c)
    before = world.snapshot("sma")

    with pytest.raises(deploy_ops.DeployOpBlockedError):
        _rollback(world)

    assert world.snapshot("sma") == before


# ---- live re-check, call budget, kind of an absent live ----

def _swap_live_after_intent(monkeypatch, world, *, to_version):
    real = deploy_ops.record_intent

    def wrapped(*args, **kwargs):
        op_id = real(*args, **kwargs)
        live = world.root / "sma"
        if live.is_symlink():
            live.unlink()
        live.symlink_to(f".versions/sma/{to_version.name}")
        return op_id

    monkeypatch.setattr(deploy_ops, "record_intent", wrapped)


def test_a_live_symlink_replaced_by_another_canonical_one_after_the_intent_is_foreign(
        world, monkeypatch):
    a, b, c = world.three_generations()
    _swap_live_after_intent(monkeypatch, world, to_version=a)

    outcome = _rollback(world, to=b.name)

    assert outcome.outcome == "foreign" and outcome.filesystem_switched is False
    assert os.readlink(world.root / "sma") == f".versions/sma/{a.name}"
    assert deploy_store.list_by_name(world.conn, "sma")[0]["phase"] == "foreign"
    assert not [p for p in world.root.iterdir() if ".link-" in p.name]


def test_a_live_that_appears_after_an_absent_intent_is_foreign(world, monkeypatch):
    a, b, c = world.three_generations()
    world.intent("sma", op_kind="disable_symlink", from_ver=c, phase="done", minute=10)
    (world.root / "sma").unlink()
    _swap_live_after_intent(monkeypatch, world, to_version=a)

    outcome = _rollback(world, to=b.name)

    assert outcome.outcome == "foreign"
    assert os.readlink(world.root / "sma") == f".versions/sma/{a.name}"


def test_one_rollback_enumerates_the_approved_inventory_at_most_twice(world, monkeypatch):
    from agentic_fx.tools import plugin_loader as tools_plugin_loader

    a, b, c = world.three_generations()
    calls = []
    real = tools_plugin_loader.approved_plugins

    def spy(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(tools_plugin_loader, "approved_plugins", spy)

    outcome = _rollback(world)

    assert outcome.outcome == "rolled_back"
    assert len(calls) <= 2


def test_a_malformed_approval_payload_does_not_hide_a_valid_decision(world):
    a, b, c = world.three_generations()
    from agentic_fx.store import approvals as approvals_store
    junk = approvals_store.create(world.conn, "plugin", {"name": "sma"}, at(40))
    approvals_store.apply_decision(world.conn, junk, "approved", decided_by="human", now=at(40))
    world.conn.execute("UPDATE approval_requests SET payload_json='{bad' WHERE id=?", (junk,))
    world.conn.commit()

    verified = _verify(world, b)

    assert isinstance(verified, deploy_rollback.VerifiedTarget)


def test_kind_of_an_absent_live_comes_from_the_target_of_a_failed_rollback_from_absent(world):
    a, b, c = world.three_generations()
    (world.root / "sma").unlink()
    world.intent("sma", from_ver=None, to_ver=b, phase="needs_retry",
                 fail_reason="verify_failed", minute=10)

    assert deploy_rollback.current_deployed_kind(world.conn, world.root, "sma") == "indicator"


def test_kind_of_an_absent_live_does_not_look_past_the_latest_closed_op(world):
    a, b, c = world.three_generations()
    world.intent("sma", op_kind="disable_symlink", from_ver=c, phase="done", minute=10)
    (world.root / "sma").unlink()
    world.intent("sma", from_ver=None, to_ver=b, phase="foreign", minute=20)

    assert deploy_rollback.current_deployed_kind(world.conn, world.root, "sma") is None
