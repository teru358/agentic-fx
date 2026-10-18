from __future__ import annotations

import os

import pytest

from agentic_fx.plugin import deploy_reconcile, deploy_retire
from agentic_fx.store import plugin_deploy_ops as deploy_store
from tests.plugin._crash import (
    crash_at,
    crash_after_intent_commit,
    crash_after_rename_plain,
    crash_before_rename_plain,
    crash_before_set_phase,
    crashed,
    restart,
)
from tests.plugin.conftest import SETTINGS
from tests.plugin.deploy_world import World, at


NOW = at(182)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _retire(world):
    return deploy_retire.retire_plugin(
        world.conn, world.root, "sma", force=False, now=NOW, settings=SETTINGS)


@pytest.mark.xfail(
    strict=True,
    reason="retire_plugin creates plugins/_retired before recording the durable intent",
)
def test_a_crash_before_plain_retire_intent_recording_leaves_no_durable_effect(world, monkeypatch):
    plain = world.version("sma", "plain")
    os.rename(plain, world.root / "sma")
    before = world.snapshot("sma")
    crash_at(monkeypatch, deploy_retire.deploy_ops, "record_intent")

    crashed(world, _retire, world)

    assert world.snapshot("sma") == before


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

    with pytest.raises(BaseException):
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

    with pytest.raises(BaseException):
        deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)
    restart(world)

    deploy_reconcile.reconcile_deploy_ops(world.conn, world.root, now=NOW, settings=SETTINGS)
    row = deploy_store.list_by_name(world.conn, "sma")[0]
    assert row["phase"] == "done" and not os.path.lexists(world.root / "sma")
