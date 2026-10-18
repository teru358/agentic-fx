from __future__ import annotations

import sqlite3

import pytest

from agentic_fx.plugin import deploy_ops
from agentic_fx.plugin.source_state import source_state
from agentic_fx.store import plugin_deploy_ops as deploy_store
from tests.plugin.deploy_world import World, at


NOW = at(183)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.mark.parametrize(
    ("phase", "reason", "blocking"),
    [
        ("intent", None, True),
        ("needs_retry", "syscall_failed", True),
        ("needs_retry", "verify_failed", False),
        ("done", None, False),
    ],
)
def test_open_consumers_share_the_deploy_operation_predicate(world, phase, reason, blocking):
    _a, _b, c = world.three_generations()
    op_id = world.intent(
        "sma", op_kind="rollback_symlink" if reason == "verify_failed" else "disable_symlink",
        from_ver=c, to_ver=_b if reason == "verify_failed" else None,
        phase=phase, fail_reason=reason,
    )
    row = deploy_store.get_by_id(world.conn, op_id)

    assert deploy_store.is_open(row) is blocking
    assert (deploy_store.get_open_by_name(world.conn, "sma") is not None) is blocking
    assert source_state(
        world.conn, world.root, name="sma", content_hash=world.content(c),
        artifact_hash=c.name, indicators=[],
    ) == ("open_deploy_op" if blocking else None)
    if blocking:
        with pytest.raises(deploy_ops.DeployOpBlockedError):
            deploy_ops.require_no_open_deploy_ops(world.conn, {"sma"}, operation="approval")
        with pytest.raises(sqlite3.IntegrityError):
            world.intent("sma", op_kind="disable_symlink", from_ver=c)
    else:
        deploy_ops.require_no_open_deploy_ops(world.conn, {"sma"}, operation="approval")
