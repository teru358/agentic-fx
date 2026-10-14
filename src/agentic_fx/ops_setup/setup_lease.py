"""Setup lock lease and the lock handoff used by first-run orchestration."""
from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import IO

from agentic_fx.store.instance_lock import acquire_instance_lock
from agentic_fx.store.setup_marker import write_setup_marker

Hook = Callable[[str], None]


class SetupLeaseBusy(RuntimeError):
    """Another setup operation owns ``data/ops/setup.lock``."""


class SetupLeaseInvalid(RuntimeError):
    """The lease no longer owns its setup lock (reason ``setup_lease_invalid``)."""

    reason_code = "setup_lease_invalid"

    def __init__(self) -> None:
        super().__init__(self.reason_code)


class SetupLeaseUnsafe(RuntimeError):
    """The setup lock path cannot be used safely."""


class SetupLease:
    """An owned setup lease.

    Keep this object open for the whole setup operation.  Call
    :meth:`acquire_instance_lock` only while it is held, and pass this same
    lease to the later enable operation instead of acquiring setup.lock again.
    """

    def __init__(self, root: Path, stream: IO[bytes]) -> None:
        self.root = Path(root).resolve()
        self._stream = stream
        self._instance_lock: IO | None = None
        self._active = True

    @property
    def active(self) -> bool:
        """Whether this lease still owns its setup lock, measured against the OS.

        A second descriptor on the same lock file tries a non-blocking
        exclusive flock.  While this lease holds the lock that attempt must
        fail; if it succeeds the lock has been lost and the lease is invalid.

        The probe only detects that the lease has been lost; it never
        re-acquires the lock.  When the probe does manage to take the lock,
        the original exclusivity is already gone, and the probe releases its
        own lock immediately, so another process may acquire setup.lock right
        after.  A caller that gets False must therefore stop, treating the
        lease as invalid, and must not continue the protected work.
        """
        if not self._active or self._stream.closed:
            return False
        try:
            probe = os.open(self.root / "data" / "ops" / "setup.lock",
                            os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            except OSError:
                return False
            try:
                fcntl.flock(probe, fcntl.LOCK_UN)
            except OSError:
                pass
            return False
        finally:
            os.close(probe)

    def release(self) -> None:
        """Release the setup lock while leaving the stream available for closing."""
        if self._active:
            try:
                if not self._stream.closed:
                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            finally:
                self._active = False

    def acquire_instance_lock(self, *, hook: Hook | None = None) -> IO:
        """Acquire instance.lock while retaining this setup lease."""
        if not self.active:
            raise SetupLeaseInvalid()
        if self._instance_lock is not None and not self._instance_lock.closed:
            raise RuntimeError("instance lock is already held")
        if hook is not None:
            hook("before_instance_lock_acquire")
        lock = acquire_instance_lock(self.root / "data")
        self._instance_lock = lock
        if hook is not None:
            try:
                hook("after_instance_lock_acquire")
            except BaseException:
                lock.close()
                self._instance_lock = None
                raise
        return lock

    @property
    def holds_instance_lock(self) -> bool:
        return self._instance_lock is not None and not self._instance_lock.closed

    def release_instance_lock(self) -> None:
        """Release only instance.lock; the setup lock stays held. Idempotent."""
        if self._instance_lock is not None and not self._instance_lock.closed:
            self._instance_lock.close()

    def close(self) -> None:
        try:
            if self._instance_lock is not None and not self._instance_lock.closed:
                self._instance_lock.close()
        finally:
            try:
                self.release()
            finally:
                if not self._stream.closed:
                    self._stream.close()

    def __enter__(self) -> SetupLease:
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()


def acquire_setup_lock(root: Path, *, after_flock: Callable[[], None] | None = None) -> IO[bytes]:
    """Acquire ``data/ops/setup.lock`` without blocking."""
    root = Path(root).resolve()
    root_fd = os.open(root, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    parent_fd = root_fd
    try:
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.getuid():
            raise SetupLeaseUnsafe()
        for name in ("data", "ops"):
            created = False
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                pass
            directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
            directory_stat = os.fstat(directory_fd)
            if (not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != os.getuid()
                    or stat.S_IMODE(directory_stat.st_mode) & 0o002):
                os.close(directory_fd)
                raise SetupLeaseUnsafe()
            if created:
                os.chmod(name, 0o700, dir_fd=parent_fd, follow_symlinks=False)
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = directory_fd
        fd = os.open("setup.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
    finally:
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)
    try:
        st = os.fstat(fd)
    except BaseException:
        os.close(fd)
        raise
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o002:
        os.close(fd)
        raise SetupLeaseUnsafe()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(fd)
        raise SetupLeaseBusy() from error
    except BaseException:
        os.close(fd)
        raise
    try:
        if after_flock is not None:
            after_flock()
        return os.fdopen(fd, "a+b")
    except BaseException:
        os.close(fd)
        raise


def acquire_setup_lease(root: Path, *, hook: Hook | None = None) -> SetupLease:
    """Acquire ``data/ops/setup.lock`` and return its owned lease."""
    root = Path(root).resolve()
    if hook is not None:
        hook("before_setup_lock_acquire")
    stream = acquire_setup_lock(
        root,
        after_flock=(lambda: hook("after_setup_lock_acquire")) if hook is not None else None,
    )
    return SetupLease(root, stream)


def begin_handoff(lease: SetupLease, marker: bytes, *, hook: Hook | None = None) -> None:
    """Install the handoff marker before releasing instance.lock.

    ``lease`` remains held after this call; the caller gives it to the enable
    operation, which obtains instance.lock itself.  The hook names expose the
    crash boundaries without changing their order.
    """
    if not lease.active:
        raise SetupLeaseInvalid()
    if lease._instance_lock is None or lease._instance_lock.closed:
        raise RuntimeError("instance lock is not held")
    if hook is not None:
        hook("before_handoff_marker")
    write_setup_marker(lease.root, marker, hook=hook)
    if hook is not None:
        hook("after_handoff_marker")
        hook("before_handoff_instance_lock_release")
    lease._instance_lock.close()
    if hook is not None:
        hook("after_handoff_instance_lock_release")
