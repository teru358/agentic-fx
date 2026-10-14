from __future__ import annotations

import fcntl
import subprocess
import sys

import pytest

from agentic_fx.ops_setup.setup_lease import (
    SetupLeaseBusy,
    SetupLeaseInvalid,
    acquire_setup_lease,
    begin_handoff,
)
from agentic_fx.store.instance_lock import InstanceAlreadyRunning, acquire_instance_lock
from agentic_fx.store.setup_marker import setup_marker_present


def test_setup_lease_excludes_a_child_process(tmp_path):
    lease = acquire_setup_lease(tmp_path)
    try:
        child = subprocess.run(
            [sys.executable, "-c", "from pathlib import Path; from agentic_fx.ops_setup.setup_lease import acquire_setup_lease, SetupLeaseBusy; import sys;\ntry: acquire_setup_lease(Path(sys.argv[1]))\nexcept SetupLeaseBusy: raise SystemExit(7)\nraise SystemExit(8)", str(tmp_path)],
            capture_output=True, text=True, timeout=30,
        )
        assert child.returncode == 7, child.stderr
    finally:
        lease.close()


def test_lease_holds_setup_lock_then_acquires_instance_lock(tmp_path):
    lease = acquire_setup_lease(tmp_path)
    try:
        instance = lease.acquire_instance_lock()
        try:
            with pytest.raises(InstanceAlreadyRunning):
                acquire_instance_lock(tmp_path / "data")
        finally:
            instance.close()
    finally:
        lease.close()


def test_released_lease_is_inactive_and_cannot_be_used_for_enable(tmp_path, monkeypatch):
    from agentic_fx.ops_setup.interactive import interactive_enable

    lease = acquire_setup_lease(tmp_path)
    try:
        lease.release()
        assert not lease.active
        result = interactive_enable(tmp_path, "web", lease=lease)
        assert (result.rc, result.stage, result.reason_code) == (1, "precheck", "setup_lease_invalid")
    finally:
        lease.close()


def test_lease_context_exit_marks_it_inactive(tmp_path):
    with acquire_setup_lease(tmp_path) as lease:
        assert lease.active
    assert not lease.active


def test_handoff_writes_marker_before_releasing_instance_lock(tmp_path):
    lease = acquire_setup_lease(tmp_path)
    lease.acquire_instance_lock()
    states: list[tuple[str, bool, bool]] = []

    def record(point: str) -> None:
        held = True
        try:
            acquire_instance_lock(tmp_path / "data").close()
        except InstanceAlreadyRunning:
            held = True
        else:
            held = False
        states.append((point, setup_marker_present(tmp_path), held))

    try:
        begin_handoff(lease, b"phase=handoff\n", hook=record)
        assert setup_marker_present(tmp_path)
        acquire_instance_lock(tmp_path / "data").close()
        assert ("after_handoff_marker", True, True) in states
        assert ("before_handoff_instance_lock_release", True, True) in states
        assert ("after_handoff_instance_lock_release", True, False) in states
    finally:
        lease.close()


@pytest.mark.parametrize("point, marker_expected, instance_expected", [
    ("before_handoff_marker", False, True),
    ("after_handoff_marker", True, True),
    ("before_handoff_instance_lock_release", True, True),
    ("after_handoff_instance_lock_release", True, False),
])
def test_handoff_interruptions_leave_a_recoverable_state(
        tmp_path, point, marker_expected, instance_expected):
    lease = acquire_setup_lease(tmp_path)
    lease.acquire_instance_lock()

    def interrupt(name: str) -> None:
        if name == point:
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match=point):
        begin_handoff(lease, b"phase=handoff\n", hook=interrupt)
    assert setup_marker_present(tmp_path) is marker_expected
    if instance_expected:
        with pytest.raises(InstanceAlreadyRunning):
            acquire_instance_lock(tmp_path / "data")
    else:
        acquire_instance_lock(tmp_path / "data").close()
    lease.close()

    retry = acquire_setup_lease(tmp_path)
    try:
        retry.acquire_instance_lock()
        begin_handoff(retry, b"phase=handoff\n")
        assert setup_marker_present(tmp_path)
    finally:
        retry.close()


def test_handoff_requires_the_lease_owned_instance_lock(tmp_path):
    lease = acquire_setup_lease(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="instance lock is not held"):
            begin_handoff(lease, b"phase=handoff\n")
        instance = lease.acquire_instance_lock()
        instance.close()
        with pytest.raises(RuntimeError, match="instance lock is not held"):
            begin_handoff(lease, b"phase=handoff\n")
    finally:
        lease.close()


@pytest.mark.parametrize("kind", ["hook", "flock"])
def test_setup_lease_closes_raw_fd_after_lock_acquire_errors(tmp_path, monkeypatch, kind):
    import agentic_fx.ops_setup.setup_lease as setup_lease

    if kind == "hook":
        hook = lambda _point: (_ for _ in ()).throw(RuntimeError("hook"))
    else:
        def boom(*_args):
            raise OSError("flock")
        monkeypatch.setattr(setup_lease.fcntl, "flock", boom)
        hook = None
    with pytest.raises((RuntimeError, OSError)):
        acquire_setup_lease(tmp_path, hook=hook)
    if kind == "flock":
        monkeypatch.undo()
    retry = acquire_setup_lease(tmp_path)
    retry.close()


def test_after_instance_lock_hook_failure_releases_lock(tmp_path):
    import pytest
    from agentic_fx.ops_setup.setup_lease import acquire_setup_lease
    (tmp_path / "data").mkdir()

    def hook(point):
        if point == "after_instance_lock_acquire":
            raise RuntimeError("injected")

    lease = acquire_setup_lease(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            lease.acquire_instance_lock(hook=hook)
        lock = lease.acquire_instance_lock()
        assert not lock.closed
    finally:
        lease.close()


def test_fstat_failure_closes_setup_lock_fd(tmp_path, monkeypatch):
    import os
    import pytest
    from agentic_fx.ops_setup import setup_lease as module
    from agentic_fx.ops_setup.setup_lease import acquire_setup_lease
    (tmp_path / "data").mkdir()
    opened = []
    real_open = os.open

    def recording_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    real_fstat = os.fstat

    def failing_fstat(fd):
        st = real_fstat(fd)
        if fd == opened[-1] and fd == max(opened):
            raise OSError(5, "injected")
        return st

    monkeypatch.setattr(module.os, "open", recording_open)
    monkeypatch.setattr(module.os, "fstat", failing_fstat)
    with pytest.raises(OSError):
        acquire_setup_lease(tmp_path)
    monkeypatch.undo()
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_raw_unlock_makes_the_lease_inactive_and_enable_refuses(tmp_path):
    from agentic_fx.ops_setup.interactive import interactive_enable

    lease = acquire_setup_lease(tmp_path)
    try:
        assert lease.active
        fcntl.flock(lease._stream.fileno(), fcntl.LOCK_UN)
        assert not lease.active
        result = interactive_enable(tmp_path, "web", lease=lease)
        assert (result.rc, result.stage, result.reason_code) == (1, "precheck", "setup_lease_invalid")
    finally:
        lease.close()


def test_close_releases_even_when_instance_lock_close_raises(tmp_path):
    class BrokenLock:
        closed = False

        def close(self):
            raise OSError("close failed")

    lease = acquire_setup_lease(tmp_path)
    stream = lease._stream
    lease._instance_lock = BrokenLock()
    with pytest.raises(OSError):
        lease.close()
    assert not lease.active
    assert stream.closed
    again = acquire_setup_lease(tmp_path)
    again.close()


def test_released_lease_refuses_instance_lock_and_handoff(tmp_path):
    lease = acquire_setup_lease(tmp_path)
    try:
        instance = lease.acquire_instance_lock()
        lease.release()
        with pytest.raises(SetupLeaseInvalid, match="setup_lease_invalid"):
            begin_handoff(lease, b"phase=handoff\n")
        instance.close()
        with pytest.raises(SetupLeaseInvalid, match="setup_lease_invalid"):
            lease.acquire_instance_lock()
    finally:
        lease.close()


def test_release_instance_lock_keeps_the_setup_lock(tmp_path):
    lease = acquire_setup_lease(tmp_path)
    try:
        lease.acquire_instance_lock()
        assert lease.holds_instance_lock
        lease.release_instance_lock()
        lease.release_instance_lock()
        assert not lease.holds_instance_lock and lease.active
        acquire_instance_lock(tmp_path / "data").close()
        with pytest.raises(SetupLeaseBusy):
            acquire_setup_lease(tmp_path)
    finally:
        lease.close()
