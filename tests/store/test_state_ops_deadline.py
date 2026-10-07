from __future__ import annotations

import fcntl
import os
import threading
import time

import pytest

from agentic_fx.store.state import StateLockCancelled, StateLockTimeout, StateStore


def _hold_lock(path, ready, release):
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        ready.set()
        release.wait()
    finally:
        os.close(fd)


def test_ac45_state_update_honors_monotonic_deadline(tmp_path):
    path = tmp_path / "app_state.json"
    ready, release = threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_lock, args=(path, ready, release))
    holder.start()
    assert ready.wait(1)
    try:
        with pytest.raises(StateLockTimeout):
            StateStore(path).update(initialized=True, deadline=time.monotonic() + 0.05)
    finally:
        release.set()
        holder.join(1)


def test_ac45_state_update_exits_when_stop_event_is_set(tmp_path):
    path = tmp_path / "app_state.json"
    ready, release, stop = threading.Event(), threading.Event(), threading.Event()
    holder = threading.Thread(target=_hold_lock, args=(path, ready, release))
    holder.start()
    assert ready.wait(1)
    stop.set()
    try:
        with pytest.raises(StateLockCancelled):
            StateStore(path).update(initialized=True, deadline=time.monotonic() + 1,
                                    stop_event=stop)
    finally:
        release.set()
        holder.join(1)


def test_ac45_state_update_waiting_on_another_thread_honors_deadline_and_stop(tmp_path):
    store = StateStore(tmp_path / "app_state.json")
    inside, release = threading.Event(), threading.Event()

    def hold():
        with store._exclusive():
            inside.set()
            release.wait(3)

    holder = threading.Thread(target=hold)
    holder.start()
    assert inside.wait(1)
    try:
        started = time.monotonic()
        with pytest.raises(StateLockTimeout):
            store.update(initialized=True, deadline=time.monotonic() + 0.05)
        assert time.monotonic() - started < 1.0
        stop = threading.Event()
        stop.set()
        with pytest.raises(StateLockCancelled):
            store.update(initialized=True, stop_event=stop)
    finally:
        release.set()
        holder.join(5)
