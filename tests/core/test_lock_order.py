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


class TrackedRLock:
    """テスト専用: 現スレッドが core lock owner かを検出する。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._owner = None
        self._depth = 0

    def __enter__(self):
        self._lock.acquire()
        owner = threading.get_ident()
        if self._owner == owner:
            self._depth += 1
        else:
            self._owner, self._depth = owner, 1
        return self

    def __exit__(self, *_args):
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
        self._lock.release()

    def owned_by_current_thread(self):
        return self._owner == threading.get_ident()


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
