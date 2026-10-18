"""core lock と plugin name lock の順序を検証する。"""
from __future__ import annotations

import contextlib
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from agentic_fx.core.contracts import FixedClock
from agentic_fx.plugin import switch
from agentic_fx.runners.base import MissionResult
from agentic_fx.service import _scheduler_tick_once
from agentic_fx.store import signals

from tests.loops.test_trade_loop import NOW, _loop
from tests.core._lock_helpers import TrackedRLock
from tests.core.test_scheduler import Env


def test_scheduler_runs_deferred_maintenance_after_core_lock_release():
    """hook を lock 内で実行する変異は event 順序を壊す。"""
    lock = TrackedRLock()
    events = []

    def hook():
        events.append(("hook", lock.owned_by_current_thread()))

    class Scheduler:
        def tick(self, now):
            events.append(("tick", lock.owned_by_current_thread(), now))
            return [hook]

    app = SimpleNamespace(
        clock=FixedClock(NOW), ingest=None, outage=None, core_lock=lock,
        executor=SimpleNamespace(
            defer_notifications=lambda: contextlib.nullcontext([])),
        scheduler=Scheduler(),
    )

    _scheduler_tick_once(app)

    assert events == [("tick", True, NOW), ("hook", False)]


def test_deferred_name_lock_is_not_acquired_while_scheduler_owns_core_lock(tmp_path):
    """The scheduler's deferred maintenance may acquire a name lock only after release."""
    lock = TrackedRLock()
    root = tmp_path / "plugins"
    root.mkdir()
    violations = []

    def hook():
        with switch._plugin_locks(root, {"strategy"}):
            pass

    class Scheduler:
        def tick(self, _now):
            return [hook]

    app = SimpleNamespace(
        clock=FixedClock(NOW), ingest=None, outage=None, core_lock=lock,
        executor=SimpleNamespace(
            defer_notifications=lambda: contextlib.nullcontext([])),
        scheduler=Scheduler(),
    )
    switch.set_plugin_lock_owner_guard(
        lambda: violations.append(lock.owned_by_current_thread()))
    try:
        _scheduler_tick_once(app)
    finally:
        switch.set_plugin_lock_owner_guard(None)

    assert violations == [False]


def test_open_takes_name_lock_before_core_lock(tmp_path, monkeypatch):
    """OPEN の name lock を core lock の後ろへ移す変異を guard で検出する。"""
    intent = {
        "action": "open", "pair": "USDJPY", "direction": "long",
        "entry_type": "market", "horizon": "day", "stop_loss": 147.0,
        "take_profit": 149.0, "reasoning": "test",
    }
    conn, loop, _, _ = _loop(tmp_path, [MissionResult("completed", intent, [])])
    signals.add(
        conn, plugin="strategy", content_hash="a" * 64, pair="USDJPY",
        timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
        payload={"direction": "long"}, now=NOW)
    loop._plugins_root = tmp_path / "plugins"
    tracked = TrackedRLock()
    loop._core_lock = tracked
    monkeypatch.setattr(
        "agentic_fx.loops.trade_loop.signal_source_active", lambda *_args: True)

    violations = []
    hold_seconds = []
    real_locks = switch._plugin_locks

    @contextlib.contextmanager
    def timed_locks(*args, **kwargs):
        started = time.monotonic()
        with real_locks(*args, **kwargs):
            yield
        hold_seconds.append(time.monotonic() - started)

    switch.set_plugin_lock_owner_guard(
        lambda: violations.append(tracked.owned_by_current_thread()))
    monkeypatch.setattr(switch, "_plugin_locks", timed_locks)
    try:
        out = loop.run_once("signal")
    finally:
        switch.set_plugin_lock_owner_guard(None)

    assert out is not None
    assert violations == [False]
    assert len(hold_seconds) == 1
    assert hold_seconds[0] < 1.0


def test_scheduler_protection_precedes_open_core_and_deferred_name_lock(tmp_path, monkeypatch):
    """OPEN waits behind protection, then the deferred hook waits behind OPEN."""
    scheduler_root = tmp_path / "scheduler"
    loop_root = tmp_path / "loop"
    scheduler_root.mkdir()
    loop_root.mkdir()
    env = Env(scheduler_root)
    conn, loop, _, _ = _loop(loop_root, [MissionResult("completed", {
        "action": "open", "pair": "USDJPY", "direction": "long",
        "entry_type": "market", "horizon": "day", "stop_loss": 147.0,
        "take_profit": 149.0, "reasoning": "test",
    }, [])])
    signals.add(
        conn, plugin="strategy", content_hash="a" * 64, pair="USDJPY",
        timeframe="1h", bar_ts=NOW.isoformat(), kind="strategy",
        payload={"direction": "long"}, now=NOW)
    loop._plugins_root = loop_root / "plugins"
    loop._plugins_root.mkdir()
    monkeypatch.setattr(
        "agentic_fx.loops.trade_loop.signal_source_active", lambda *_args: True)

    events = []
    open_name_entered = threading.Event()
    scheduler_entered = threading.Event()
    sltp_entered = threading.Event()
    allow_scheduler_release = threading.Event()
    open_core_entered = threading.Event()

    class RecordingRLock(TrackedRLock):
        def __enter__(self):
            super().__enter__()
            events.append("core-enter")
            return self

        def __exit__(self, *args):
            events.append("core-release")
            return super().__exit__(*args)

    core_lock = RecordingRLock()
    loop._core_lock = core_lock
    real_locks = switch._plugin_locks

    @contextlib.contextmanager
    def ordered_locks(root, names, **kwargs):
        names = set(names)
        with real_locks(root, names, **kwargs):
            if names == {"strategy"}:
                events.append("open-name")
                open_name_entered.set()
                assert scheduler_entered.wait(5)
            else:
                assert open_core_entered.wait(5)
                events.append("hook-name")
            yield

    monkeypatch.setattr(switch, "_plugin_locks", ordered_locks)
    lock_owners = []
    switch.set_plugin_lock_owner_guard(
        lambda: lock_owners.append(core_lock.owned_by_current_thread()))

    def exits(_now, _filled):
        events.append("sltp")
        sltp_entered.set()
        assert allow_scheduler_release.wait(5)

    env.sched._process_exits = exits

    def delayed_hook(_now):
        with switch._plugin_locks(loop._plugins_root, {"maintenance"}):
            assert not core_lock.owned_by_current_thread()

    env.sched.on_signal_maintenance = delayed_hook
    app = SimpleNamespace(
        clock=FixedClock(NOW), ingest=None, outage=None, core_lock=core_lock,
        executor=env.executor, scheduler=env.sched,
    )

    original_record = loop.executor.record_and_validate_intent

    def record(*args, **kwargs):
        events.append("open-core")
        open_core_entered.set()
        return original_record(*args, **kwargs)

    loop.executor.record_and_validate_intent = record
    open_thread = threading.Thread(target=lambda: loop.run_once("signal"))
    open_thread.start()
    assert open_name_entered.wait(5)

    scheduler_thread = threading.Thread(target=lambda: _scheduler_tick_once(app))
    try:
        scheduler_thread.start()
        assert sltp_entered.wait(5)
        scheduler_entered.set()
        allow_scheduler_release.set()
        scheduler_thread.join(5)
        open_thread.join(5)
    finally:
        switch.set_plugin_lock_owner_guard(None)
    assert not scheduler_thread.is_alive()
    assert not open_thread.is_alive()
    assert events.index("open-name") < events.index("sltp")
    assert events.index("sltp") < events.index("open-core") < events.index("hook-name")
    assert lock_owners == [False, False]
