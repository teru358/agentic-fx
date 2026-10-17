"""Signal producer against real separate processes: name-lock windows,
deadline expiry and the interleavings with ``disable_plugin``."""
from __future__ import annotations

import shutil
from datetime import timedelta

import pytest

from agentic_fx.plugin import signal_producer as sp
from agentic_fx.plugin.signal_producer import SignalProducer
from agentic_fx.store import signals as signals_store
from tests.plugin._conc_support import (
    FIRST_TICK, NEXT_TICK, Activity, ConcWorld, DisableChild, Holder, Sandbox,
    lock_is_free, probe_other_process, run_disable)
from tests.plugin.test_signal_producer import H, SETTINGS, SOURCE, _open_result


@pytest.fixture
def expired_deadline(monkeypatch):
    """The producer's deadline clock reads 0.0, so its deadline is already in
    the past of the real monotonic clock the lock loop compares against: a lock
    that is busy on the first non-blocking try times out without waiting."""
    monkeypatch.setattr(sp, "monotonic", lambda: 0.0)


@pytest.fixture
def cleanup():
    items = []
    yield items
    for item in items:
        item.close()


def _cursor(producer, world):
    meta = world.meta()
    return producer._cursor.get((meta.name, meta.content_hash, "USDJPY"))


# --- evaluation does not hold the name lock; the insert window does ----------

def test_name_lock_is_free_during_evaluation_and_held_during_insert(
        tmp_path, monkeypatch):
    world = ConcWorld(tmp_path, indicator="a")
    names = ["a", "s"]
    seen = []

    sandbox = Sandbox(_open_result(), on_call=lambda: seen.append(
        ("evaluate", probe_other_process(world.root, names))))

    real_state = sp.source_state

    def revalidate(*args, **kwargs):
        seen.append(("revalidate", probe_other_process(world.root, names)))
        return real_state(*args, **kwargs)

    real_add = signals_store.add

    def insert(*args, **kwargs):
        seen.append(("insert", probe_other_process(world.root, names)))
        return real_add(*args, **kwargs)

    monkeypatch.setattr(sp, "source_state", revalidate)
    monkeypatch.setattr(signals_store, "add", insert)

    inserted = world.evaluate(SignalProducer(), FIRST_TICK, sandbox, Activity())

    assert inserted == 1
    assert seen == [
        ("evaluate", {"a": True, "s": True}),
        ("revalidate", {"a": False, "s": False}),
        ("insert", {"a": False, "s": False}),
    ]
    assert probe_other_process(world.root, names) == {"a": True, "s": True}


# --- deadline expiry ----------------------------------------------------------

def test_lock_deadline_skips_insert_keeps_cursor_and_notifies_once_per_change(
        tmp_path, monkeypatch, expired_deadline, cleanup):
    world = ConcWorld(tmp_path, indicator="a")
    producer, activity = SignalProducer(), Activity()
    sandbox = Sandbox(_open_result())

    holder = Holder(world.root, tmp_path / "h1", ["s"])
    cleanup.append(holder)
    assert not lock_is_free(world.root, "s")

    assert world.evaluate(producer, FIRST_TICK, sandbox, activity) == 0
    cursor_after_first = _cursor(producer, world)
    assert world.total() == 0
    assert activity.names() == ["plugin_skipped_lock_timeout"]
    # the first lock of the sorted pair was taken and must not leak
    assert lock_is_free(world.root, "a")

    assert world.evaluate(producer, FIRST_TICK, sandbox, activity) == 0
    assert _cursor(producer, world) == cursor_after_first
    assert cursor_after_first is None or cursor_after_first < H
    assert len(sandbox.calls) == 2
    assert [c["df"].index[-1] for c in sandbox.calls] == [H, H]
    assert activity.names() == ["plugin_skipped_lock_timeout"]
    assert world.total() == 0

    holder.release()
    assert world.evaluate(producer, FIRST_TICK, sandbox, activity) == 1
    assert _cursor(producer, world) == H
    assert len(sandbox.calls) == 3
    assert activity.names() == ["plugin_skipped_lock_timeout"]

    second = Holder(world.root, tmp_path / "h2", ["s"])
    cleanup.append(second)
    assert world.evaluate(producer, NEXT_TICK, sandbox, activity) == 0
    assert _cursor(producer, world) == H
    assert world.total() == 1
    assert activity.names() == ["plugin_skipped_lock_timeout"] * 2
    assert [c["df"].index[-1] for c in sandbox.calls][-1] == H + timedelta(hours=1)
    second.release()


# --- producer x disable interleavings ----------------------------------------

def _hook_pre_lock(monkeypatch, action, events):
    real = sp.acquire_name_locks

    def pre_lock(root, names, **kwargs):
        events.append("pre_lock")
        action()
        return real(root, names, **kwargs)

    monkeypatch.setattr(sp, "acquire_name_locks", pre_lock)


def test_disable_completed_between_evaluation_and_lock_prevents_insert(
        tmp_path, monkeypatch):
    world = ConcWorld(tmp_path)
    events, activity, producer = [], Activity(), SignalProducer()
    results = []

    def disable_to_completion():
        results.append(run_disable(
            world.db_path, world.root, "s", force=False, prefix=tmp_path / "d1"))
        events.append("disable_done")

    _hook_pre_lock(monkeypatch, disable_to_completion, events)
    sandbox = Sandbox(_open_result(), on_call=lambda: events.append("evaluate"))

    assert world.evaluate(producer, FIRST_TICK, sandbox, activity) == 0

    assert events == ["evaluate", "pre_lock", "disable_done"]
    assert results == ["disabled:"]
    assert not (world.root / "s").exists()
    assert world.pending() == 0 and world.total() == 0
    assert activity.events == [
        ("plugin_skipped_not_live", "name=s reason=strategy_not_live")]
    cursor = _cursor(producer, world)
    assert cursor is None or cursor < H


def test_insert_completed_before_disable_takes_its_locks_is_abandoned_by_cascade(
        tmp_path, cleanup):
    world = ConcWorld(tmp_path)
    producer = SignalProducer()
    disable = DisableChild(world.db_path, world.root, "s", pause="before_locks",
                           force=False, prefix=tmp_path / "d2")
    cleanup.append(disable)
    disable.wait_reached()
    assert lock_is_free(world.root, "s")

    sandbox = Sandbox(_open_result())
    assert world.evaluate(producer, FIRST_TICK, sandbox, Activity()) == 1
    assert world.pending() == 1

    disable.go()
    assert disable.finish() == "disabled:"

    assert world.pending() == 0
    assert world.signal_rows() == [("s", "abandoned")]
    linked = world.conn.execute(
        "SELECT status_at_link FROM plugin_deploy_op_signals").fetchall()
    assert [r["status_at_link"] for r in linked] == ["pending"]
    assert not (world.root / "s").exists()


def test_insert_is_refused_while_disable_holds_its_locks_before_recording_intent(
        tmp_path, monkeypatch, expired_deadline, cleanup):
    world = ConcWorld(tmp_path)
    producer, activity = SignalProducer(), Activity()
    disable = DisableChild(world.db_path, world.root, "s", pause="record_intent",
                           force=False, prefix=tmp_path / "d3")
    cleanup.append(disable)
    disable.wait_reached()
    assert not lock_is_free(world.root, "s")

    sandbox = Sandbox(_open_result())
    assert world.evaluate(producer, FIRST_TICK, sandbox, activity) == 0
    assert world.total() == 0
    assert activity.names() == ["plugin_skipped_lock_timeout"]
    cursor = _cursor(producer, world)
    assert cursor is None or cursor < H

    disable.go()
    assert disable.finish() == "disabled:"
    assert world.pending() == 0 and world.total() == 0

    assert world.evaluate(producer, FIRST_TICK, sandbox, activity) == 0
    assert world.total() == 0
    assert len(sandbox.calls) == 1
    assert activity.names() == [
        "plugin_skipped_lock_timeout", "plugin_skipped_not_live"]


# --- dependent indicator interleavings ---------------------------------------

def test_forced_disable_of_indicator_during_strategy_evaluation_drops_the_insert(
        tmp_path, monkeypatch):
    world = ConcWorld(tmp_path, indicator="a")
    events, activity, producer = [], Activity(), SignalProducer()
    results = []

    def disable_indicator():
        results.append(run_disable(
            world.db_path, world.root, "a", force=True, prefix=tmp_path / "d4"))
        events.append("indicator_disabled")

    _hook_pre_lock(monkeypatch, disable_indicator, events)
    sandbox = Sandbox(_open_result(), on_call=lambda: events.append("evaluate"))

    assert world.evaluate(producer, FIRST_TICK, sandbox, activity) == 0

    assert events == ["evaluate", "pre_lock", "indicator_disabled"]
    assert results == ["disabled:s"]
    assert (world.root / "s").is_symlink() and not (world.root / "a").exists()
    assert world.total() == 0
    assert activity.events == [
        ("plugin_skipped_not_live", "name=s reason=indicator_not_live")]
    cursor = _cursor(producer, world)
    assert cursor is None or cursor < H


def test_strategy_on_a_plain_indicator_inserts_while_it_is_live_without_hash_check(
        tmp_path):
    world = ConcWorld(tmp_path)
    plain = world.root / "a"
    plain.mkdir()
    (plain / "plugin.py").write_text("def compute(df, params):\n    return {'value': 1.0}\n")
    (plain / "config.yaml").write_text("kind: indicator\noutputs: [value]\n")
    world.indicator_name = "a"
    producer, activity = SignalProducer(), Activity()
    sandbox = Sandbox(_open_result())

    # the resolved hash differs on purpose: a plain indicator is not hash-checked
    resolved = world.resolved(content_hash="f" * 64)
    meta = world.meta()

    def run(now):
        return producer.evaluate_due_plugins(
            world.conn, plugins=[meta], now=now, source=SOURCE,
            sandbox_run=sandbox, settings=SETTINGS,
            resolved_by_identity=resolved, activity=activity,
            plugins_root=world.root)

    assert run(FIRST_TICK) == 1
    assert world.signal_rows() == [("s", "pending")]
    assert activity.events == []

    shutil.rmtree(plain)
    assert run(NEXT_TICK) == 0
    assert world.total() == 1
    assert activity.events == [
        ("plugin_skipped_not_live", "name=s reason=indicator_not_live")]
