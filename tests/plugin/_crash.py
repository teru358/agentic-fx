"""Crash seams shared by durable deployment-operation tests."""
from __future__ import annotations

import errno as errno_module
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from agentic_fx.plugin import deploy_disable, deploy_ops, deploy_reconcile, deploy_rollback
from agentic_fx.store import db as db_store
from agentic_fx.store import plugin_deploy_ops as deploy_store


class CrashPoint(BaseException):
    """A process death that must not be caught by ordinary error handling."""


def crash_at(monkeypatch, obj, attr: str, *, when: str = "before", nth: int = 1,
             match: Callable[..., bool] | None = None) -> None:
    """Raise once around the selected call, optionally after its side effect."""
    if when not in {"before", "after"}:
        raise ValueError("when must be 'before' or 'after'")
    original = getattr(obj, attr)
    calls = 0
    fired = False

    def wrapped(*args, **kwargs):
        nonlocal calls, fired
        selected = match is None or match(*args, **kwargs)
        if selected:
            calls += 1
        should_crash = selected and calls == nth and not fired
        if should_crash and when == "before":
            fired = True
            monkeypatch.setattr(obj, attr, original)
            raise CrashPoint(attr)
        result = original(*args, **kwargs)
        if should_crash and when == "after":
            fired = True
            monkeypatch.setattr(obj, attr, original)
            raise CrashPoint(attr)
        return result

    monkeypatch.setattr(obj, attr, wrapped)


def crash_after_intent_commit(monkeypatch) -> None:
    crash_at(monkeypatch, deploy_ops, "record_intent", when="after")


def crash_before_set_phase(monkeypatch, phase: str) -> None:
    crash_at(monkeypatch, deploy_store, "set_phase", match=lambda _conn, _id, value, **_: value == phase)


def crash_after_set_phase(monkeypatch, phase: str) -> None:
    crash_at(monkeypatch, deploy_store, "set_phase", when="after",
             match=lambda _conn, _id, value, **_: value == phase)


def crash_before_notify(monkeypatch) -> None:
    crash_at(monkeypatch, deploy_ops, "try_notify")


def crash_after_activity(monkeypatch) -> None:
    crash_at(monkeypatch, deploy_store, "mark_notify_attempted")


def crash_in_activity_write(monkeypatch, activity) -> None:
    crash_at(monkeypatch, activity, "write")


def crash_before_swap(monkeypatch) -> None:
    crash_at(monkeypatch, deploy_rollback, "swap_to_target")


def crash_before_rename_plain(monkeypatch) -> None:
    crash_at(monkeypatch, deploy_reconcile, "rename_plain_dir")


def crash_after_rename_plain(monkeypatch) -> None:
    crash_at(monkeypatch, deploy_reconcile, "rename_plain_dir", when="after")


def crash_inside_record_intent(monkeypatch, stage: str) -> None:
    seams = {
        "after_revocation": (deploy_ops.approvals_store, "append_revocation"),
        "after_insert": (deploy_store, "insert_intent"),
        "after_hook": (deploy_ops.signals_store, "abandon_pending_for_name"),
        "after_cascade": (deploy_ops.signals_store, "abandon_pending_for_identities"),
    }
    if stage not in seams:
        raise ValueError(f"unknown record-intent stage: {stage}")
    obj, attr = seams[stage]
    crash_at(monkeypatch, obj, attr, when="after")


def fail_syscall(monkeypatch, name: str, *, errno_: int = errno_module.EACCES,
                 path_pred: Callable[[Path], bool], times: int | None = None,
                 crash: bool = False, when: str = "before"):
    """Fail or crash selected ``os`` calls without touching pytest or SQLite paths."""
    original = getattr(os, name)
    remaining = times
    active = True

    def wrapped(path, *args, **kwargs):
        nonlocal remaining
        selected = active and path_pred(Path(path)) and (remaining is None or remaining > 0)
        if selected and when == "before":
            if remaining is not None:
                remaining -= 1
            if crash:
                raise CrashPoint(name)
            raise OSError(errno_, os.strerror(errno_), str(path))
        result = original(path, *args, **kwargs)
        if selected and when == "after":
            if remaining is not None:
                remaining -= 1
            if crash:
                raise CrashPoint(name)
            raise OSError(errno_, os.strerror(errno_), str(path))
        return result

    monkeypatch.setattr(os, name, wrapped)

    def heal() -> None:
        nonlocal active
        active = False

    return heal


def restart(world) -> None:
    world.conn.close()
    world.conn = db_store.connect(world.db_path)
    db_store.init_db(world.conn)


def crashed(world, fn, *args, **kwargs):
    with pytest.raises(CrashPoint):
        fn(*args, **kwargs)
    restart(world)


def assert_state(world, op_id: int, *, phase: str, fail_reason: str | None = None,
                 notify: bool | None = None, live=None, retired: bool | None = None) -> None:
    row = deploy_store.get_by_id(world.conn, op_id)
    assert row is not None
    assert (row["phase"], row["fail_reason"]) == (phase, fail_reason)
    if notify is not None:
        assert bool(row["notify_attempted"]) is notify
    live_path = world.root / row["name"]
    if live is None:
        assert not os.path.lexists(live_path)
    elif live == ("dir",):
        assert live_path.is_dir() and not live_path.is_symlink()
    else:
        assert live_path.is_symlink() and os.readlink(live_path) == live[1]
    if retired is not None:
        assert (world.root / row["retired_path"]).exists() is retired


def db_effects(world) -> tuple:
    """Every table a deployment operation can touch, rows compared whole."""
    def rows(sql: str) -> list[tuple]:
        return [tuple(r) for r in world.conn.execute(sql)]

    return (
        rows("SELECT * FROM plugin_deploy_ops ORDER BY op_id"),
        rows("SELECT id,status,requeue_count FROM signals ORDER BY id"),
        rows("SELECT id,status FROM approval_requests ORDER BY id"),
        rows("SELECT * FROM plugin_deploy_op_signals ORDER BY op_id,signal_id"),
    )


def op_count(world, name: str = "sma") -> int:
    return len(deploy_store.list_by_name(world.conn, name))


def reconcile(world, settings, *, now, activity=None):
    return deploy_reconcile.reconcile_deploy_ops(
        world.conn, world.root, now=now, settings=settings, activity=activity)


class Scene:
    """Signals and approvals around a disable of indicator ``sma``."""

    def __init__(self, world, *, revoke: bool, cascade: bool) -> None:
        self.revoke = revoke
        self.cascade = cascade
        self.a, self.b, self.c = world.three_generations()
        self.content = world.content(self.c)
        self.signals = [world.signal("sma", self.content),
                        world.signal("sma", self.content, claimed=True)]
        if cascade:
            strategy = world.version("strat", "strategy", kind="strategy", indicator="sma")
            world.deploy("strat", strategy, None, 4)
            strategy_content = world.content(strategy)
            self.signals += [world.signal("strat", strategy_content),
                             world.signal("strat", strategy_content, claimed=True)]

    @property
    def force(self) -> bool:
        return self.cascade


SCENES = [(False, False), (True, False), (False, True), (True, True)]
SCENE_IDS = ["plain", "revoke", "cascade", "revoke-and-cascade"]
