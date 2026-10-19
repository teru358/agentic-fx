"""safe opener の読み取り量・hash 比較・fd 後始末・live 名の検証の境界。"""
from __future__ import annotations

import os
import stat

import pytest

from agentic_fx.ops import change_limits as L
from agentic_fx.ops import safe_open as so

from ._change_fixtures import CONFIG, PLUGIN_PY, TEST_PY, hash_of, make_version, write_files


def _code(callable_):
    with pytest.raises(so.SourceError) as raised:
        callable_()
    return raised.value.code


def _version_with_plugin(root, name, size):
    body = b"#" * (size - 1) + b"\n"
    digest = hash_of(body, CONFIG, TEST_PY)
    write_files(root / ".versions" / name / digest, body, CONFIG, TEST_PY)
    return digest


def _count_chunk_reads(monkeypatch):
    calls = []
    real = os.read

    def spy(fd, n):
        if n == so._CHUNK:
            calls.append(fd)
        return real(fd, n)
    monkeypatch.setattr(so.os, "read", spy)
    return calls


def _lie_about_size(monkeypatch):
    real = os.fstat

    class Reported:
        def __init__(self, info):
            self.st_mode = info.st_mode
            self.st_size = 0

    def fake(fd):
        info = real(fd)
        return Reported(info) if stat.S_ISREG(info.st_mode) else info
    monkeypatch.setattr(so.os, "fstat", fake)


def test_oversized_file_is_refused_from_its_size_before_any_byte_is_read(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    digest = _version_with_plugin(root, "sma", L.MAX_FILE_BYTES + 1)
    reads = _count_chunk_reads(monkeypatch)
    assert _code(lambda: so.read_source(root, so.version_source("sma", digest), digest)) \
        == L.TOO_LARGE
    assert reads == []


def test_file_at_the_limit_is_read_in_full(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    digest = _version_with_plugin(root, "sma", L.MAX_FILE_BYTES)
    reads = _count_chunk_reads(monkeypatch)
    files = so.read_source(root, so.version_source("sma", digest), digest)
    assert len(files.plugin_py) == L.MAX_FILE_BYTES and reads


def test_read_loop_refuses_a_file_that_reports_a_small_size_but_is_over_the_limit(
        tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    digest = _version_with_plugin(root, "sma", L.MAX_FILE_BYTES + 1)
    _lie_about_size(monkeypatch)
    assert _code(lambda: so.read_source(root, so.version_source("sma", digest), digest)) \
        == L.TOO_LARGE


def test_read_loop_accepts_a_file_of_exactly_the_limit_when_the_reported_size_is_wrong(
        tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    digest = _version_with_plugin(root, "sma", L.MAX_FILE_BYTES)
    _lie_about_size(monkeypatch)
    assert len(so.read_source(root, so.version_source("sma", digest), digest).plugin_py) \
        == L.MAX_FILE_BYTES


@pytest.mark.parametrize("position", [0, 31, 63])
def test_hash_that_differs_in_one_hex_digit_is_a_mismatch(tmp_path, position):
    root = tmp_path / "plugins"
    digest = make_version(root, "sma")
    flipped = digest[:position] + ("0" if digest[position] != "0" else "1") + digest[position + 1:]
    assert _code(lambda: so.read_source(root, so.version_source("sma", digest), flipped)) \
        == L.HASH_MISMATCH


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a/../b", "A", "1a", "a b", "a\n", ".", "..",
                                 "a" * 65, None, 5])
def test_inspect_live_refuses_names_that_are_not_plugin_names(tmp_path, bad):
    root = tmp_path / "plugins"
    root.mkdir()
    (root / "b").mkdir()
    assert _code(lambda: so.inspect_live(root, bad)) == L.UNSAFE_SOURCE


def _open_fds():
    return set(os.listdir("/proc/self/fd"))


def test_every_directory_fd_is_closed_after_success_and_after_each_failure(tmp_path):
    root = tmp_path / "plugins"
    digest = make_version(root, "sma")
    before = _open_fds()
    so.read_source(root, so.version_source("sma", digest), digest)
    assert _open_fds() == before
    assert _code(lambda: so.read_source(root, so.version_source("sma", digest), "0" * 64)) \
        == L.HASH_MISMATCH
    assert _open_fds() == before
    assert _code(lambda: so.read_source(root, so.version_source("nope", digest), digest)) \
        == L.UNAVAILABLE
    assert _open_fds() == before
    assert _code(lambda: so.read_source(root, so.version_source("sma", "1" * 64), "1" * 64)) \
        == L.UNAVAILABLE
    assert _open_fds() == before
    assert so.inspect_live(root, "sma").kind == "absent"
    assert _open_fds() == before


def test_a_symlinked_plugins_root_is_refused(tmp_path):
    real = tmp_path / "real"
    digest = make_version(real, "sma")
    link = tmp_path / "plugins"
    os.symlink(real, link)
    assert _code(lambda: so.read_source(link, so.version_source("sma", digest), digest)) \
        == L.UNSAFE_SOURCE
    assert _code(lambda: so.inspect_live(link, "sma")) == L.UNSAFE_SOURCE


def test_a_plugins_root_that_is_a_regular_file_is_refused(tmp_path):
    root = tmp_path / "plugins"
    root.write_bytes(b"")
    assert _code(lambda: so.read_source(root, so.version_source("sma", "0" * 64), "0" * 64)) \
        == L.UNSAFE_SOURCE


def test_no_read_asks_for_more_than_one_byte_past_the_limit_even_when_data_never_ends(
        tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    digest = _version_with_plugin(root, "sma", 1024)
    _lie_about_size(monkeypatch)
    real = os.read
    requested = []
    served = []

    def endless(fd, n):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            requested.append(n)
            served.append(n)
            return b"x" * n
        return real(fd, n)
    monkeypatch.setattr(so.os, "read", endless)
    assert _code(lambda: so.read_source(root, so.version_source("sma", digest), digest)) \
        == L.TOO_LARGE
    assert sum(served) == L.MAX_FILE_BYTES + 1
    assert max(requested) <= so._CHUNK


def test_each_read_is_clamped_to_what_is_left_of_the_limit_plus_one(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    digest = _version_with_plugin(root, "sma", L.MAX_FILE_BYTES)
    _lie_about_size(monkeypatch)
    real = os.read
    seen = []

    def spy(fd, n):
        seen.append(n)
        return real(fd, n)
    monkeypatch.setattr(so.os, "read", spy)
    so.read_source(root, so.version_source("sma", digest), digest)
    total = 0
    for n in seen[:1 + L.MAX_FILE_BYTES // so._CHUNK]:
        assert n <= L.MAX_FILE_BYTES - total + 1
        total += min(n, so._CHUNK)


def _open_fd_count():
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.parametrize("opener", ["dir", "plugins"])
def test_an_opened_directory_that_fstat_reports_as_not_a_directory_is_refused_and_closed(
        tmp_path, monkeypatch, opener):
    (tmp_path / "sub").mkdir()
    monkeypatch.setattr(so.stat, "S_ISDIR", lambda mode: False)
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    before = _open_fd_count()
    try:
        if opener == "dir":
            code = _code(lambda: so._open_dir(parent, "sub"))
        else:
            code = _code(lambda: so._open_plugins_dir(tmp_path))
        assert code == L.UNSAFE_SOURCE
        assert _open_fd_count() == before
    finally:
        os.close(parent)
