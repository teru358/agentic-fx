from __future__ import annotations

import os

import pytest

from agentic_fx.plugin import deploy_reconcile, deploy_retire
from agentic_fx.plugin import deploy_ops
from agentic_fx.store import plugin_deploy_ops as deploy_store
from tests.plugin.conftest import SETTINGS
from tests.plugin.deploy_world import Activity, World, at


NOW = at(120)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_plain_retire_moves_directory_records_relative_destination_and_notifies(world):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")
    signal_id = world.signal("sma", "untrusted-content")
    claimed_id = world.signal("sma", "untrusted-content", claimed=True)
    activity = Activity()

    result = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS,
        activity=activity)

    assert result.outcome == "done"
    assert not os.path.lexists(world.root / "sma")
    row = deploy_store.get_by_id(world.conn, result.op_id)
    assert row["op_kind"] == "retire_plain"
    assert row["from_kind"] == "plain" and row["to_kind"] == "retired"
    assert row["subject_content_hash"] == world.content(world.root / row["retired_path"])
    assert row["subject_artifact_hash"] is None
    assert row["retired_path"] == result.retired_path
    assert str(result.op_id) in row["retired_path"]
    assert (world.root / row["retired_path"] / "plugin.py").is_file()
    assert world.conn.execute("SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()[0] == "abandoned"
    assert world.conn.execute(
        "SELECT status_at_link FROM plugin_deploy_op_signals WHERE op_id=? AND signal_id=?",
        (result.op_id, signal_id)).fetchone()[0] == "pending"
    assert world.conn.execute("SELECT status FROM signals WHERE id=?", (claimed_id,)).fetchone()[0] == "claimed"
    assert world.conn.execute(
        "SELECT status_at_link FROM plugin_deploy_op_signals WHERE op_id=? AND signal_id=?",
        (result.op_id, claimed_id)).fetchone()[0] == "claimed"
    assert [event for _, event, _ in activity.events] == ["plugin_retired"]


def test_plain_retire_blocks_dependents_without_force_and_cascades_with_force(world):
    indicator = world.version("sma", "plain")
    indicator_content = world.content(indicator)
    os.rename(indicator, world.root / "sma")
    strategy = world.version("strat", "strategy", kind="strategy", indicator="sma",
                             pin="a" * 64)
    assert "a" * 64 != indicator_content
    world.deploy("strat", strategy, None, 1)
    signal_id = world.signal("sma", "unknown")

    blocked = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)

    assert blocked.outcome == "dependents_blocked" and (world.root / "sma").is_dir()
    done = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=True, now=NOW, settings=SETTINGS)
    assert done.outcome == "done" and done.cascade_strategies == ("strat",)
    assert world.conn.execute("SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()[0] == "abandoned"


def test_plain_retire_records_null_identity_when_discovery_fails_and_completes(world):
    plain = world.version("sma", "broken")
    os.rename(plain, world.root / "sma")
    (world.root / "sma" / "config.yaml").write_text("kind: [not valid\n")

    result = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)

    row = deploy_store.get_by_id(world.conn, result.op_id)
    assert result.outcome == "done" and row["subject_content_hash"] is None
    assert (world.root / row["retired_path"]).is_dir()


def test_plain_retire_rejects_cross_device_before_recording(world, monkeypatch):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")
    real_stat = deploy_retire.os.stat

    def different_device(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if str(path).endswith("/_retired"):
            class Different:
                st_dev = result.st_dev + 1
            return Different()
        return result

    monkeypatch.setattr(deploy_retire.os, "stat", different_device)
    with pytest.raises(OSError, match="same filesystem"):
        deploy_retire.retire_plugin(
            world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)
    assert world.conn.execute("SELECT count(*) FROM plugin_deploy_ops").fetchone()[0] == 0


def test_plain_retire_rejects_existing_destination_before_recording(world):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")
    destination = world.root / "_retired" / "sma-20261016T050000Z-1"
    destination.mkdir(parents=True)

    with pytest.raises(FileExistsError, match="already exists"):
        deploy_retire.retire_plugin(
            world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)
    assert world.conn.execute("SELECT count(*) FROM plugin_deploy_ops").fetchone()[0] == 0


def test_plain_retire_rename_failure_reopens_with_same_signature_and_completes(world, monkeypatch):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")

    def fail_rename(*args):
        raise OSError("injected")

    real_rename = deploy_retire.deploy_reconcile.rename_plain_dir
    monkeypatch.setattr(deploy_retire.deploy_reconcile, "rename_plain_dir", fail_rename)
    failed = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)
    row = deploy_store.get_by_id(world.conn, failed.op_id)
    assert failed.outcome == "needs_retry" and (row["phase"], row["fail_reason"]) == (
        "needs_retry", "syscall_failed")
    with pytest.raises(deploy_ops.SignatureMismatchError):
        deploy_retire.retire_plugin(
            world.conn, world.root, "sma", force=True, now=NOW, settings=SETTINGS)
    with pytest.raises(deploy_ops.SignatureMismatchError):
        deploy_retire.retire_plugin(
            world.conn, world.root, "sma", force=True, now=NOW, settings=SETTINGS,
            dry_run=True)

    monkeypatch.setattr(deploy_retire.deploy_reconcile, "rename_plain_dir", real_rename)
    done = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)
    assert done.outcome == "done" and done.op_id == failed.op_id


def test_plain_retry_refuses_a_destination_collision_without_closing_the_open_op(world, monkeypatch):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")
    real_rename = deploy_retire.deploy_reconcile.rename_plain_dir
    monkeypatch.setattr(
        deploy_retire.deploy_reconcile, "rename_plain_dir",
        lambda *args: (_ for _ in ()).throw(OSError("injected")),
    )
    failed = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)
    monkeypatch.setattr(deploy_retire.deploy_reconcile, "rename_plain_dir", real_rename)
    row = deploy_store.get_by_id(world.conn, failed.op_id)
    (world.root / row["retired_path"]).mkdir(parents=True)

    result = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)

    reread = deploy_store.get_by_id(world.conn, failed.op_id)
    assert result.outcome == "refused"
    assert str(failed.op_id) in result.reason and row["retired_path"] in result.reason
    assert (reread["phase"], reread["fail_reason"]) == ("needs_retry", "syscall_failed")
    assert (world.root / "sma").is_dir()


def test_plain_retry_dry_run_refuses_a_destination_collision_like_a_real_run(world, monkeypatch):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")
    real_rename = deploy_retire.deploy_reconcile.rename_plain_dir
    monkeypatch.setattr(
        deploy_retire.deploy_reconcile, "rename_plain_dir",
        lambda *args: (_ for _ in ()).throw(OSError("injected")),
    )
    failed = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)
    monkeypatch.setattr(deploy_retire.deploy_reconcile, "rename_plain_dir", real_rename)
    row = deploy_store.get_by_id(world.conn, failed.op_id)
    (world.root / row["retired_path"]).mkdir(parents=True)

    result = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS, dry_run=True)

    reread = deploy_store.get_by_id(world.conn, failed.op_id)
    assert result.outcome == "refused" and row["retired_path"] in result.reason
    assert (reread["phase"], reread["fail_reason"]) == ("needs_retry", "syscall_failed")


def test_plain_retry_closes_foreign_when_the_directory_was_replaced(world, monkeypatch):
    original = world.version("sma", "original")
    replacement = world.version("sma", "replacement")
    os.rename(original, world.root / "sma")
    real_rename = deploy_retire.deploy_reconcile.rename_plain_dir
    monkeypatch.setattr(deploy_retire.deploy_reconcile, "rename_plain_dir",
                        lambda *args: (_ for _ in ()).throw(OSError("injected")))
    failed = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)
    monkeypatch.setattr(deploy_retire.deploy_reconcile, "rename_plain_dir", real_rename)
    os.rename(world.root / "sma", world.root / "old-sma")
    os.rename(replacement, world.root / "sma")

    result = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)

    row = deploy_store.get_by_id(world.conn, failed.op_id)
    assert result.outcome == "foreign" and row["phase"] == "foreign"
    assert (world.root / "sma").is_dir()
    assert not (world.root / row["retired_path"]).exists()


def test_plain_retire_force_abandons_pending_signals_of_dependent_strategies(world):
    indicator = world.version("sma", "plain")
    os.rename(indicator, world.root / "sma")
    strategy = world.version("strat", "strategy", kind="strategy", indicator="sma",
                             pin="a" * 64)
    world.deploy("strat", strategy, None, 1)
    strategy_content = world.content(strategy)
    strategy_signal = world.signal("strat", strategy_content)

    done = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=True, now=NOW, settings=SETTINGS)

    assert done.outcome == "done" and done.cascade_strategies == ("strat",)
    assert world.conn.execute(
        "SELECT status FROM signals WHERE id=?", (strategy_signal,)).fetchone()[0] == "abandoned"
    assert world.conn.execute(
        "SELECT status_at_link FROM plugin_deploy_op_signals WHERE op_id=? AND signal_id=?",
        (done.op_id, strategy_signal)).fetchone()[0] == "pending"


def test_already_retired_dry_run_does_not_recover_notifications(world):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")
    activity = Activity()
    result = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS,
        activity=activity)
    world.conn.execute("UPDATE plugin_deploy_ops SET notify_attempted=0")
    world.conn.commit()
    activity.events.clear()
    before = world.snapshot("sma")

    dry = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS,
        activity=activity, dry_run=True)

    assert dry.outcome == "already_retired" and dry.notified_op_ids == ()
    assert activity.events == [] and world.snapshot("sma") == before
    assert result.op_id is not None


def test_plain_retire_dry_run_changes_neither_database_nor_filesystem(world):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")
    before = world.snapshot("sma")

    result = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS,
        dry_run=True)

    assert result.outcome == "dry_run" and world.snapshot("sma") == before


def test_symlink_retire_uses_disable_operation_and_activity(world):
    version = world.version("sma", "version")
    world.deploy("sma", version, None, 1)
    activity = Activity()

    result = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS,
        activity=activity)

    row = deploy_store.get_by_id(world.conn, result.op_id)
    assert result.outcome == "disabled" and row["op_kind"] == "disable_symlink"
    assert [event for _, event, _ in activity.events] == ["plugin_disabled"]


def test_symlink_retire_takes_over_matching_disable_intent(world):
    version = world.version("sma", "version")
    world.deploy("sma", version, None, 1)
    op_id = world.intent("sma", op_kind="disable_symlink", from_ver=version)

    result = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)

    assert result.op_id == op_id and result.outcome == "disabled"


def test_plain_retry_closes_foreign_when_live_becomes_symlink(world, monkeypatch):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")
    real_rename = deploy_retire.deploy_reconcile.rename_plain_dir
    monkeypatch.setattr(deploy_retire.deploy_reconcile, "rename_plain_dir",
                        lambda *args: (_ for _ in ()).throw(OSError("injected")))
    failed = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)
    monkeypatch.setattr(deploy_retire.deploy_reconcile, "rename_plain_dir", real_rename)
    os.rename(world.root / "sma", world.root / "replaced")
    (world.root / "sma").symlink_to("replaced")

    result = deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)

    assert result.outcome == "foreign"
    assert deploy_store.get_by_id(world.conn, failed.op_id)["phase"] == "foreign"


def test_reconcile_completes_plain_retire_intent_with_relative_path(world):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")
    retired = "_retired/sma-crash-1"
    (world.root / "_retired").mkdir()
    op_id = world.intent("sma", op_kind="retire_plain", retired_path=retired)

    results = deploy_reconcile.reconcile_deploy_ops(
        world.conn, world.root, now=NOW, settings=SETTINGS)

    assert deploy_store.get_by_id(world.conn, op_id)["phase"] == "done"
    assert (world.root / retired / "plugin.py").is_file()
    assert results[0].outcome == "done"
