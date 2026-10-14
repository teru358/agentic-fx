from __future__ import annotations

import os

import pytest

from agentic_fx.store.setup_marker import (
    remove_setup_marker,
    setup_marker_path,
    setup_marker_present,
    write_setup_marker,
)


def test_setup_marker_is_durable_and_removed(tmp_path, monkeypatch):
    calls: list[str] = []
    real_fsync = os.fsync

    def remember(fd: int) -> None:
        calls.append(os.readlink(f"/proc/self/fd/{fd}"))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", remember)
    write_setup_marker(tmp_path, b"phase=commit\n")

    assert setup_marker_path(tmp_path) == tmp_path / "data" / "state" / "setup_in_progress"
    assert setup_marker_present(tmp_path)
    assert setup_marker_path(tmp_path).read_bytes() == b"phase=commit\n"
    assert str(tmp_path) in calls
    assert str(tmp_path / "data") in calls
    assert str(tmp_path / "data" / "state") in calls

    remove_setup_marker(tmp_path)
    assert not setup_marker_present(tmp_path)


def test_setup_marker_stat_error_fails_closed(tmp_path, monkeypatch):
    import agentic_fx.store.setup_marker as marker

    monkeypatch.setattr(marker.os, "lstat", lambda _path: (_ for _ in ()).throw(PermissionError()))
    assert setup_marker_present(tmp_path)


@pytest.mark.parametrize("point", [
    "before_setup_marker_write", "before_setup_marker_replace",
    "after_setup_marker_replace", "after_setup_marker_write",
])
def test_marker_write_hook_interruption_can_be_retried(tmp_path, point):
    def interrupt(name: str) -> None:
        if name == point:
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match=point):
        write_setup_marker(tmp_path, b"phase=commit\n", hook=interrupt)

    write_setup_marker(tmp_path, b"phase=commit\n")
    remove_setup_marker(tmp_path)
    assert not setup_marker_present(tmp_path)


@pytest.mark.parametrize("point", [
    "before_setup_marker_remove", "after_setup_marker_unlink", "after_setup_marker_remove",
])
def test_marker_remove_hook_interruption_can_be_retried(tmp_path, point):
    write_setup_marker(tmp_path, b"phase=commit\n")

    def interrupt(name: str) -> None:
        if name == point:
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match=point):
        remove_setup_marker(tmp_path, hook=interrupt)

    remove_setup_marker(tmp_path)
    assert not setup_marker_present(tmp_path)


def test_remove_retry_after_unlink_crash_fsyncs_parent(tmp_path, monkeypatch):
    import os
    import pytest
    from agentic_fx.store import setup_marker as module
    module.write_setup_marker(tmp_path, b"x")
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(module.os, "fsync", lambda fd: (synced.append(os.fstat(fd).st_ino), real_fsync(fd)))

    def crash(point):
        if point == "after_setup_marker_unlink":
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError):
        module.remove_setup_marker(tmp_path, hook=crash)
    synced.clear()
    module.remove_setup_marker(tmp_path)
    parent_ino = os.stat(module.setup_marker_path(tmp_path).parent).st_ino
    assert parent_ino in synced
    assert not module.setup_marker_present(tmp_path)


def test_read_setup_marker_parses_commit_and_handoff_content(tmp_path):
    from agentic_fx.store.setup_marker import format_setup_marker, read_setup_marker

    assert read_setup_marker(tmp_path) is None
    sha = "ab" * 32
    write_setup_marker(tmp_path, format_setup_marker("commit", "2026-10-13T00:00:00+00:00", sha))
    marker = read_setup_marker(tmp_path)
    assert (marker.phase, marker.started_at, marker.settings_sha256) == (
        "commit", "2026-10-13T00:00:00+00:00", sha)
    write_setup_marker(tmp_path, format_setup_marker("handoff", "2026-10-13T00:00:01+00:00"))
    marker = read_setup_marker(tmp_path)
    assert (marker.phase, marker.settings_sha256) == ("handoff", None)


@pytest.mark.parametrize("content", [
    b"", b"\xff\xfe", b"phase=other\n", b"phase=commit\nsettings_sha256=zz\n",
    b"x" * 5000, b"phase=commit\nphase=handoff\n"])
def test_read_setup_marker_never_trusts_malformed_content(tmp_path, content):
    from agentic_fx.store.setup_marker import read_setup_marker

    write_setup_marker(tmp_path, content)
    marker = read_setup_marker(tmp_path)
    assert marker is not None and marker.settings_sha256 is None
    if content.startswith(b"phase=commit\n"):
        assert marker.phase == "commit"
    else:
        assert marker.phase is None


def test_read_setup_marker_treats_a_symlink_as_unreadable_but_present(tmp_path):
    from agentic_fx.store.setup_marker import read_setup_marker, setup_marker_path

    target = tmp_path / "elsewhere"
    target.write_bytes(b"phase=commit\n")
    path = setup_marker_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    marker = read_setup_marker(tmp_path)
    assert marker is not None and marker.phase is None
    assert setup_marker_present(tmp_path)
