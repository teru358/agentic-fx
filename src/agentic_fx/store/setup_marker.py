"""Durable marker used while initial setup is committing or handing off locks."""
from __future__ import annotations

import errno
import os
import tempfile
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SETUP_IN_PROGRESS = "setup_in_progress"
EXIT_SETUP_IN_PROGRESS = 76
Hook = Callable[[str], None]


class SetupInProgress(Exception):
    """Initial setup has not reached a state from which the service may start."""


def setup_marker_path(root: Path) -> Path:
    return Path(root) / "data" / "state" / SETUP_IN_PROGRESS


def setup_marker_present(root: Path) -> bool:
    """Return whether the marker exists, treating unreadable metadata as present."""
    try:
        os.lstat(setup_marker_path(root))
    except OSError as error:
        return error.errno != errno.ENOENT
    return True


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_marker_parent(marker: Path) -> None:
    parent = marker.parent.parent.parent
    for name in ("data", "state"):
        child = parent / name
        try:
            child.mkdir()
        except FileExistsError:
            pass
        else:
            _fsync_directory(child)
            _fsync_directory(parent)
        parent = child


def write_setup_marker(root: Path, content: bytes, *, hook: Hook | None = None) -> Path:
    """Atomically install a durable marker; ``hook`` observes each write boundary."""
    marker = setup_marker_path(root)
    _ensure_marker_parent(marker)
    if hook is not None:
        hook("before_setup_marker_write")
    fd, temporary = tempfile.mkstemp(prefix=f".{SETUP_IN_PROGRESS}.", dir=marker.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if hook is not None:
            hook("before_setup_marker_replace")
        os.replace(temporary, marker)
        if hook is not None:
            hook("after_setup_marker_replace")
        _fsync_directory(marker.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    if hook is not None:
        hook("after_setup_marker_write")
    return marker


def remove_setup_marker(root: Path, *, hook: Hook | None = None) -> None:
    """Remove the marker durably; an already absent marker is a completed removal."""
    marker = setup_marker_path(root)
    if hook is not None:
        hook("before_setup_marker_remove")
    try:
        os.unlink(marker)
    except FileNotFoundError:
        # 前回の除去が unlink の後・dir fsync の前で止まった可能性があるので、親 dir の fsync は省かない
        if marker.parent.is_dir():
            _fsync_directory(marker.parent)
        return
    if hook is not None:
        hook("after_setup_marker_unlink")
    _fsync_directory(marker.parent)
    if hook is not None:
        hook("after_setup_marker_remove")


@dataclass(frozen=True)
class SetupMarker:
    """Parsed marker content. Fields that are absent or malformed are ``None``."""
    phase: str | None
    started_at: str | None
    settings_sha256: str | None


_MAX_MARKER_BYTES = 4096


def format_setup_marker(phase: str, started_at: str, settings_sha256: str | None = None) -> bytes:
    """The one content layout written for a marker (``key=value`` lines)."""
    lines = [f"phase={phase}", f"started_at={started_at}"]
    if settings_sha256 is not None:
        lines.append(f"settings_sha256={settings_sha256}")
    return ("\n".join(lines) + "\n").encode("ascii")


def read_setup_marker(root: Path) -> SetupMarker | None:
    """Read the marker content; ``None`` when absent.

    A marker that exists but cannot be read or parsed yields a marker with no
    fields, so callers never mistake an unreadable marker for a resumable one.
    The service-side start guard does not use this: it decides on existence only.
    """
    path = setup_marker_path(root)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        return SetupMarker(None, None, None)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return SetupMarker(None, None, None)
        raw = os.read(fd, _MAX_MARKER_BYTES + 1)
    except OSError:
        return SetupMarker(None, None, None)
    finally:
        os.close(fd)
    if len(raw) > _MAX_MARKER_BYTES:
        return SetupMarker(None, None, None)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return SetupMarker(None, None, None)
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"phase", "started_at", "settings_sha256"} and key not in fields:
            fields[key] = value
    phase = fields.get("phase")
    if phase not in {"commit", "handoff"}:
        phase = None
    sha = fields.get("settings_sha256")
    if sha is not None and (len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)):
        sha = None
    return SetupMarker(phase, fields.get("started_at") or None, sha)
