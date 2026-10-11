"""fd ベースの JSON snapshot は rename 後の一つだけを正本にする。"""
from __future__ import annotations

import json
import os

import pytest

from afx_ops import snapshot
from afx_ops.snapshot import SnapshotError, read_snapshot, write_snapshot


def _open_dir(path):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def test_writes_and_reads_a_snapshot(tmp_path):
    dir_fd = _open_dir(tmp_path)
    try:
        write_snapshot(dir_fd, "state.json", {"version": 1, "cursor": 7})
        assert read_snapshot(dir_fd, "state.json", 1) == {"version": 1, "cursor": 7}
    finally:
        os.close(dir_fd)


def test_keeps_the_previous_snapshot_when_a_temp_file_is_left_after_a_crash(tmp_path):
    dir_fd = _open_dir(tmp_path)
    try:
        write_snapshot(dir_fd, "state.json", {"version": 1, "cursor": 7})
        (tmp_path / ".state.json.tmp-crashed").write_text('{"version": 1', encoding="ascii")
        assert read_snapshot(dir_fd, "state.json", 1) == {"version": 1, "cursor": 7}
    finally:
        os.close(dir_fd)


def test_rejects_invalid_json_without_removing_it(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{", encoding="ascii")
    dir_fd = _open_dir(tmp_path)
    try:
        with pytest.raises(SnapshotError) as info:
            read_snapshot(dir_fd, "state.json", 1)
    finally:
        os.close(dir_fd)
    assert info.value.reason == "snapshot_corrupt"
    assert path.read_text(encoding="ascii") == "{"


def test_rejects_a_different_schema_version(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 2}), encoding="ascii")
    dir_fd = _open_dir(tmp_path)
    try:
        with pytest.raises(SnapshotError) as info:
            read_snapshot(dir_fd, "state.json", 1)
    finally:
        os.close(dir_fd)
    assert info.value.reason == "snapshot_corrupt"


def test_a_write_removes_stale_temp_files_for_the_same_snapshot(tmp_path):
    stale = tmp_path / ".state.json.tmp-crashed"
    stale.write_text("partial", encoding="ascii")
    dir_fd = _open_dir(tmp_path)
    try:
        write_snapshot(dir_fd, "state.json", {"version": 1})
    finally:
        os.close(dir_fd)
    assert not stale.exists()


def test_a_snapshot_directory_is_scanned_only_on_its_first_write(monkeypatch, tmp_path):
    dir_fd = _open_dir(tmp_path)
    calls = []
    original_listdir = os.listdir

    def count_listdir(fd):
        calls.append(fd)
        return original_listdir(fd)

    monkeypatch.setattr("afx_ops.snapshot.os.listdir", count_listdir)
    try:
        write_snapshot(dir_fd, "state.json", {"version": 1, "cursor": 1})
        write_snapshot(dir_fd, "state.json", {"version": 1, "cursor": 2})
    finally:
        os.close(dir_fd)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["write", "file_fsync", "replace"])
def test_write_failure_removes_its_temp_file(monkeypatch, tmp_path, failure):
    dir_fd = _open_dir(tmp_path)
    if failure == "write":
        monkeypatch.setattr("afx_ops.snapshot._write_all",
                            lambda *_args: (_ for _ in ()).throw(OSError("full")))
    elif failure == "file_fsync":
        monkeypatch.setattr("afx_ops.snapshot.os.fsync",
                            lambda *_args: (_ for _ in ()).throw(OSError("full")))
    else:
        monkeypatch.setattr("afx_ops.snapshot.os.replace",
                            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("full")))
    try:
        with pytest.raises(SnapshotError):
            write_snapshot(dir_fd, "state.json", {"version": 1})
    finally:
        os.close(dir_fd)
    assert list(tmp_path.glob(".state.json.tmp-*")) == []


def test_never_follows_a_snapshot_symlink(tmp_path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"version": 1}), encoding="ascii")
    (tmp_path / "state.json").symlink_to(target)
    dir_fd = _open_dir(tmp_path)
    try:
        with pytest.raises(SnapshotError) as info:
            read_snapshot(dir_fd, "state.json", 1)
    finally:
        os.close(dir_fd)
    assert info.value.reason == "snapshot_corrupt"


@pytest.mark.parametrize("name", ["", "..", "state/name", "bad\0name"])
def test_rejects_invalid_snapshot_names_as_caller_errors(tmp_path, name):
    dir_fd = _open_dir(tmp_path)
    try:
        with pytest.raises(ValueError):
            write_snapshot(dir_fd, name, {"version": 1})
        with pytest.raises(ValueError):
            read_snapshot(dir_fd, name, 1)
    finally:
        os.close(dir_fd)


def test_never_writes_into_an_existing_temp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "_remove_stale_temps_once", lambda *args: None)
    monkeypatch.setattr(snapshot.secrets, "token_hex", lambda nbytes: "0" * (2 * nbytes))
    temp = tmp_path / (".state.json.tmp-" + "0" * 32)
    temp.write_text("keep", encoding="ascii")
    dir_fd = _open_dir(tmp_path)
    try:
        with pytest.raises(SnapshotError):
            write_snapshot(dir_fd, "state.json", {"version": 1, "cursor": 7})
    finally:
        os.close(dir_fd)
    assert temp.read_text(encoding="ascii") == "keep"
    assert not (tmp_path / "state.json").exists()


def test_the_temp_file_is_synced_before_the_rename_and_the_directory_after(tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    dir_fd = _open_dir(tmp_path)

    def fsync(fd):
        events.append("dir" if fd == dir_fd else "file")
        real_fsync(fd)

    def replace(*args, **kwargs):
        events.append("replace")
        real_replace(*args, **kwargs)

    monkeypatch.setattr(snapshot.os, "fsync", fsync)
    monkeypatch.setattr(snapshot.os, "replace", replace)
    try:
        write_snapshot(dir_fd, "state.json", {"version": 1, "cursor": 7})
    finally:
        os.close(dir_fd)
    assert events == ["file", "replace", "dir"]
