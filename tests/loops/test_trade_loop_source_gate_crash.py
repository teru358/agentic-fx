"""Claimed-signal OPEN against deploy operations that complete, die or race around it."""
from __future__ import annotations

import os
import threading
from datetime import timedelta

import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops import trade_loop
from agentic_fx.plugin import deploy_disable, deploy_retire, deploy_rollback, switch
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import missions, signals
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store.db import connect
from tests.loops.test_trade_loop import NOW, SETTINGS, _loop
from tests.loops.test_trade_loop_source_gate import OPEN, _add, _last_intent
from tests.plugin._conc_support import lock_is_free
from tests.plugin._crash import CrashPoint, crash_at
from tests.plugin.deploy_world import World


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


OPEN_LIMIT = {
    "action": "open", "pair": "USDJPY", "direction": "long", "entry_type": "limit",
    "horizon": "day", "limit_price": 148.20, "expires_in": "4h", "stop_loss": 147.80,
    "take_profit": 149.00, "reasoning": "test",
}


def _two_generations(world, name="strat"):
    first = world.version(name, "s1", kind="strategy")
    second = world.version(name, "s2", kind="strategy")
    world.deploy(name, first, None, 1)
    world.deploy(name, second, first, 2)
    return first, second


def _loop_on(world, tmp_path, results):
    conn, loop, runner, _ = _loop(tmp_path, results, conn=world.conn)
    loop._plugins_root = world.root
    return conn, loop


def _claimable_signal(conn, content):
    return signals.add(
        conn, plugin="strat", content_hash=content, pair="USDJPY", timeframe="1h",
        bar_ts=NOW.isoformat(), kind="strategy", payload={"direction": "long"}, now=NOW)


def _before_the_source_lock(monkeypatch, action):
    real = trade_loop.acquire_name_locks

    def hooked(*args, **kwargs):
        action()
        return real(*args, **kwargs)

    monkeypatch.setattr(trade_loop, "acquire_name_locks", hooked)


def _disable(world):
    return deploy_disable.disable_plugin(
        world.conn, world.root, "strat", revoke=False, force=False, now=NOW,
        settings=SETTINGS)


def _rollback(world, target):
    return lambda: deploy_rollback.rollback_plugin(
        world.conn, world.root, "strat", to=target.name, revoke=False, force=False,
        now=NOW, settings=SETTINGS)


def _retire(world):
    return deploy_retire.retire_plugin(
        world.conn, world.root, "strat", force=False, now=NOW, settings=SETTINGS)


def _status(conn, signal_id):
    return dict(conn.execute(
        "SELECT status,requeue_count FROM signals WHERE id=?", (signal_id,)).fetchone())


# ---- a deploy operation completes between the claim and the source lock ----

@pytest.mark.parametrize("kind", ["disable", "rollback", "retire"])
def test_an_operation_completed_before_the_source_lock_rejects_the_claimed_open(
        world, tmp_path, monkeypatch, kind):
    if kind == "retire":
        os.rename(world.version("strat", "plain", kind="strategy"), world.root / "strat")
        content = world.content(world.root / "strat")
        run = lambda: _retire(world)  # noqa: E731
    else:
        first, second = _two_generations(world)
        content = world.content(second)
        run = (lambda: _disable(world)) if kind == "disable" else _rollback(world, first)
    conn, loop = _loop_on(world, tmp_path, [MissionResult("completed", OPEN, [])])
    sid = _claimable_signal(conn, content)
    done = []
    _before_the_source_lock(monkeypatch, lambda: done.append(run()))

    out = loop.run_once("signal")

    assert len(done) == 1
    assert out == {"result": "rejected", "order_id": None,
                   "reasons": ["signal_source_inactive"]}
    assert _status(conn, sid) == {"status": "abandoned", "requeue_count": 0}
    assert dict(_last_intent(conn)) == {
        "gate_result": "rejected", "reject_reason": "signal_source_inactive",
        "reject_category": "signal"}
    op = deploy_store.list_by_name(conn, "strat")[-1]
    assert op["phase"] == "done"
    links = [tuple(r) for r in conn.execute(
        "SELECT signal_id,status_at_link FROM plugin_deploy_op_signals WHERE op_id=?",
        (op["op_id"],))]
    assert links in ([(sid, "claimed")], [])


# ---- a deploy operation starts while the order is being placed ----

def test_a_disable_started_during_the_order_waits_for_the_source_lock_and_runs_after_it(
        world, tmp_path, monkeypatch):
    _first, second = _two_generations(world)
    conn, loop = _loop_on(world, tmp_path, [MissionResult("completed", OPEN_LIMIT, [])])
    sid = _claimable_signal(conn, world.content(second))
    events: list[str] = []
    started = threading.Event()
    box: dict = {}

    def run_disable():
        other = connect(world.db_path)
        try:
            started.set()
            box["outcome"] = deploy_disable.disable_plugin(
                other, world.root, "strat", revoke=False, force=False, now=NOW,
                settings=SETTINGS, deadline=None)
            events.append("disable_done")
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            other.close()

    worker = threading.Thread(target=run_disable)
    real = loop.executor.open_from_snapshot

    def open_with_a_competing_disable(*args, **kwargs):
        worker.start()
        assert started.wait(30)
        box["lock_free_during_order"] = lock_is_free(world.root, "strat")
        box["intents_during_order"] = len(deploy_store.list_by_name(world.conn, "strat"))
        result = real(*args, **kwargs)
        events.append("order_placed")
        return result

    loop.executor.open_from_snapshot = open_with_a_competing_disable
    try:
        out = loop.run_once("signal")
    finally:
        worker.join(timeout=60)

    assert not worker.is_alive() and "error" not in box
    assert box["lock_free_during_order"] is False and box["intents_during_order"] == 0
    assert out["result"] == "pending" and out["order_id"] is not None, out
    assert events == ["order_placed", "disable_done"]
    assert _status(conn, sid)["status"] == "consumed"
    assert box["outcome"].outcome == "disabled"
    assert not os.path.lexists(world.root / "strat")


# ---- the source lock cannot be taken ----

@pytest.mark.parametrize(("preset", "limit", "expected"), [
    (0, 2, {"status": "pending", "requeue_count": 1}),
    (1, 2, {"status": "pending", "requeue_count": 2}),
    (2, 2, {"status": "abandoned", "requeue_count": 2}),
    (0, 0, {"status": "abandoned", "requeue_count": 0}),
])
def test_a_busy_source_lock_requeues_a_claim_until_the_requeue_limit(
        tmp_path, monkeypatch, preset, limit, expected):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult("completed", OPEN, [])])
    sid = _add(conn)
    conn.execute("UPDATE signals SET requeue_count=? WHERE id=?", (preset, sid))
    conn.commit()
    loop._plugins_root = tmp_path / "plugins"
    loop.settings = SETTINGS.model_copy(deep=True)
    loop.settings.plugin.signal_requeue_max = limit

    from contextlib import contextmanager

    @contextmanager
    def busy(*_args, **_kwargs):
        raise switch.PluginBusyError("held")
        yield

    monkeypatch.setattr(switch, "_plugin_locks", busy)
    out = loop.run_once("signal")

    assert out["reasons"] == ["signal_source_lock_timeout"]
    assert _status(conn, sid) == expected


# ---- snapshot staleness around the consume ----

def _advance_after_gather(loop, moment, *, executor_only=False):
    real_gather = loop.executor.gather_open_snapshot

    def gather_then_advance(*args, **kwargs):
        snapshot = real_gather(*args, **kwargs)
        loop.executor.clock = FixedClock(moment)
        if not executor_only:
            loop.clock = FixedClock(moment)
        return snapshot

    loop.executor.gather_open_snapshot = gather_then_advance


@pytest.mark.parametrize(("preset", "limit", "expected"), [
    (0, 2, {"status": "pending", "requeue_count": 1}),
    (2, 2, {"status": "abandoned", "requeue_count": 2}),
    (0, 0, {"status": "abandoned", "requeue_count": 0}),
])
def test_a_stale_snapshot_requeues_a_fresh_signal_unless_the_requeue_limit_is_reached(
        tmp_path, preset, limit, expected):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult("completed", OPEN, [])])
    sid = _add(conn)
    conn.execute("UPDATE signals SET requeue_count=? WHERE id=?", (preset, sid))
    conn.commit()
    loop.settings = SETTINGS.model_copy(deep=True)
    loop.settings.plugin.signal_requeue_max = limit
    _advance_after_gather(loop, NOW + timedelta(seconds=31))

    out = loop.run_once("signal")

    assert "execution snapshot is stale" in out["reasons"][0]
    assert _status(conn, sid) == expected
    assert dict(_last_intent(conn))["reject_category"] == "execution"


def test_a_snapshot_that_turns_stale_after_the_gate_leaves_the_signal_consumed(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult("completed", OPEN, [])])
    sid = _add(conn)
    _advance_after_gather(loop, NOW + timedelta(seconds=31), executor_only=True)

    out = loop.run_once("signal")

    assert out["result"] == "rejected"
    assert "execution snapshot is stale" in out["reasons"][0]
    assert _status(conn, sid) == {"status": "consumed", "requeue_count": 0}
    assert dict(_last_intent(conn))["reject_category"] == "execution"


def test_a_signal_that_turns_stale_with_a_fresh_snapshot_is_abandoned_without_a_requeue(
        tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult("completed", OPEN, [])])
    sid = _add(conn)
    real_gather = loop.executor.gather_open_snapshot

    def gather_at_the_late_clock(*args, **kwargs):
        late = NOW + timedelta(hours=3)
        loop.clock = FixedClock(late)
        loop.executor.clock = FixedClock(late)
        return real_gather(*args, **kwargs)

    loop.executor.gather_open_snapshot = gather_at_the_late_clock

    out = loop.run_once("signal")

    assert out["reasons"] == ["signal stale"]
    assert _status(conn, sid) == {"status": "abandoned", "requeue_count": 0}
    assert dict(_last_intent(conn))["reject_category"] == "signal"


def test_a_signal_and_a_snapshot_that_are_both_stale_abandon_the_signal_without_a_requeue(
        tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult("completed", OPEN, [])])
    sid = _add(conn)
    _advance_after_gather(loop, NOW + timedelta(hours=3))

    out = loop.run_once("signal")

    assert out["reasons"] == ["signal stale"]
    assert _status(conn, sid) == {"status": "abandoned", "requeue_count": 0}
    assert dict(_last_intent(conn))["reject_category"] == "signal"


# ---- startup recovery and lease reclaim of claims ----

def _link_to_an_operation(world, name, content):
    """Disable ``name`` so every pending or claimed signal of ``content`` is linked."""
    deploy_disable.disable_plugin(
        world.conn, world.root, name, revoke=False, force=False, now=NOW, settings=SETTINGS)


def _claimed(world, plugin, content, mission_id, *, count=0, bar_minute=0):
    sid = signals.add(
        world.conn, plugin=plugin, content_hash=content, pair="USDJPY", timeframe="1h",
        bar_ts=(NOW + timedelta(minutes=bar_minute)).isoformat(), kind="strategy",
        payload={}, now=NOW)
    world.conn.execute(
        "UPDATE signals SET status='claimed', claimed_by_mission_id=?, claimed_at=?, "
        "requeue_count=? WHERE id=?", (mission_id, NOW.isoformat(), count, sid))
    world.conn.commit()
    return sid


def test_startup_recovery_requeues_unlinked_claims_and_abandons_linked_ones(world):
    _a, _b, c = world.three_generations("sma")
    mission_id = missions.start(world.conn, "trade", "local", "m", NOW)
    linked = _claimed(world, "sma", world.content(c), mission_id)
    below = _claimed(world, "other", "x" * 64, mission_id, count=1, bar_minute=1)
    at_limit = _claimed(world, "other", "y" * 64, mission_id, count=2, bar_minute=2)
    _link_to_an_operation(world, "sma", world.content(c))
    assert world.conn.execute("SELECT status FROM signals WHERE id=?", (linked,)).fetchone()[0] == "claimed"

    result = missions.recover_interrupted(world.conn, now=NOW, max_requeue=2)

    assert world.conn.execute(
        "SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()[0] == "interrupted"
    assert _status(world.conn, linked) == {"status": "abandoned", "requeue_count": 0}
    assert _status(world.conn, below) == {"status": "pending", "requeue_count": 2}
    assert _status(world.conn, at_limit) == {"status": "abandoned", "requeue_count": 2}
    assert (result["signals_requeued"], result["signals_abandoned"]) == (1, 2)


def test_a_linked_claim_recovered_at_startup_is_never_claimed_again(world):
    _a, _b, c = world.three_generations("sma")
    mission_id = missions.start(world.conn, "trade", "local", "m", NOW)
    linked = _claimed(world, "sma", world.content(c), mission_id)
    _link_to_an_operation(world, "sma", world.content(c))

    missions.recover_interrupted(world.conn, now=NOW, max_requeue=2)

    assert signals.pending_exists(world.conn, now=NOW, freshness_bars=None) is False
    assert signals.claim_oldest(
        world.conn, mission_id=mission_id + 1, now=NOW, freshness_bars=None) is None
    assert _status(world.conn, linked)["status"] == "abandoned"


def test_a_crash_after_the_consume_keeps_the_signal_consumed_at_the_next_start(
        tmp_path, monkeypatch):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult("completed", OPEN, [])])
    sid = _add(conn)
    crash_at(monkeypatch, loop.executor, "open_from_snapshot")

    with pytest.raises(CrashPoint):
        loop.run_once("signal")
    mission_id = conn.execute("SELECT id FROM missions").fetchone()[0]
    # a real death skips the cleanup that ran above
    conn.execute("UPDATE missions SET status='running', finished_at=NULL WHERE id=?",
                 (mission_id,))
    conn.commit()
    conn.close()
    conn = connect(tmp_path / "t.db")
    links = "SELECT * FROM plugin_deploy_op_signals ORDER BY op_id, signal_id"
    links_before = [tuple(r) for r in conn.execute(links)]

    result = missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    assert result["missions_recovered"] == 1
    assert conn.execute(
        "SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()[0] == "interrupted"
    assert [tuple(r) for r in conn.execute(links)] == links_before == []
    assert _status(conn, sid) == {"status": "consumed", "requeue_count": 0}
    assert signals.pending_exists(conn, now=NOW, freshness_bars=None) is False


@pytest.mark.parametrize(("elapsed", "reclaimed"), [
    (timedelta(minutes=15), True),
    (timedelta(minutes=15) - timedelta(seconds=1), False),
])
def test_lease_reclaim_without_a_mission_abandons_linked_claims_at_the_lease_boundary(
        world, elapsed, reclaimed):
    _a, _b, c = world.three_generations("sma")
    linked = _claimed(world, "sma", world.content(c), None)
    unlinked = _claimed(world, "other", "x" * 64, None, bar_minute=1)
    _link_to_an_operation(world, "sma", world.content(c))

    first = missions.start(world.conn, "trade", "local", "m", NOW)
    second = missions.start(world.conn, "trade", "local", "m", NOW)
    signals.reclaim_expired(world.conn, now=NOW + elapsed, lease_min=15, max_requeue=2)

    if reclaimed:
        assert _status(world.conn, linked) == {"status": "abandoned", "requeue_count": 0}
        assert _status(world.conn, unlinked) == {"status": "pending", "requeue_count": 1}
        assert signals.claim_oldest(
            world.conn, mission_id=first, now=NOW + elapsed, freshness_bars=None)["id"] == unlinked
        assert signals.claim_oldest(
            world.conn, mission_id=second, now=NOW + elapsed, freshness_bars=None) is None
    else:
        assert _status(world.conn, linked)["status"] == "claimed"
        assert _status(world.conn, unlinked)["status"] == "claimed"
