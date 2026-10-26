"""親専有 report の読み (dirfd + O_NOFOLLOW + 上限)。"""
from __future__ import annotations

import os

import pytest

from agentic_fx.ops import safe_open
from agentic_fx.ops.change_limits import TOO_LARGE, UNAVAILABLE, UNSAFE_SOURCE
from agentic_fx.ops.safe_open import SourceError, read_improve_report

MAX = 64 * 1024
NAME = "improve-2026-10-25-2043.md"


@pytest.fixture
def reports(tmp_path):
    root = tmp_path / "data" / "improve_reports"
    root.mkdir(parents=True)
    return root


def _code(reports, mission_id, value):
    with pytest.raises(SourceError) as raised:
        read_improve_report(reports, mission_id, value)
    return raised.value.code


def test_reads_the_named_report(reports):
    (reports / NAME).write_text("hello 日本語\n", encoding="utf-8")
    assert read_improve_report(reports, 2043, str(reports / NAME)) == "hello 日本語\n".encode()
    assert read_improve_report(reports, 2043, NAME) == "hello 日本語\n".encode()


def test_missing_file_is_unavailable(reports):
    assert _code(reports, 2043, str(reports / NAME)) == UNAVAILABLE


def test_symlink_final_component_is_refused_even_with_good_content(reports, tmp_path):
    target = tmp_path / "elsewhere.md"
    target.write_text("# Improve Mission 2043 — gate failed\n")
    os.symlink(target, reports / NAME)
    assert _code(reports, 2043, str(reports / NAME)) == UNSAFE_SOURCE


def test_reports_directory_symlink_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / NAME).write_text("x")
    link = tmp_path / "data_link"
    os.symlink(real, link)
    assert _code(link, 2043, NAME) == UNSAFE_SOURCE


def test_directory_in_place_of_report_is_refused(reports):
    (reports / NAME).mkdir()
    assert _code(reports, 2043, str(reports / NAME)) == UNSAFE_SOURCE


def test_fifo_is_refused_without_blocking(reports):
    os.mkfifo(reports / NAME)
    assert _code(reports, 2043, str(reports / NAME)) == UNSAFE_SOURCE


@pytest.mark.parametrize("value", [
    "improve-2026-10-25-2044.md",
    "improve-2026-10-25-12043.md",
    "improve-2026-10-25-2043.txt",
    "improve-2026-10-25-2043.md.bak",
    "improve-2026-10-25-2043.md/..",
    "../improve-2026-10-25-2043.md",
    "improve-26-10-25-2043.md",
    "x/improve-2026-10-25-2043.md",
    "improve-2026-10-25-2043.md\n",
    "improve-2026-10-25-2043.md\x00",
    "",
    "/",
    "/improve-2026-10-25-2043.md",
])
def test_basename_must_be_the_canonical_name_of_the_same_mission(reports, value):
    (reports / NAME).write_text("x")
    (reports / "improve-2026-10-25-2044.md").write_text("other mission")
    assert _code(reports, 2043, value) == UNSAFE_SOURCE


def test_another_missions_report_is_not_readable(reports):
    (reports / "improve-2026-10-25-2044.md").write_text("other mission")
    assert _code(reports, 2043, str(reports / "improve-2026-10-25-2044.md")) == UNSAFE_SOURCE


def test_path_outside_the_reports_directory_is_refused(reports, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / NAME).write_text("x")
    (reports / NAME).write_text("x")
    assert _code(reports, 2043, str(outside / NAME)) == UNSAFE_SOURCE
    assert _code(reports, 2043, str(reports / "sub" / NAME)) == UNSAFE_SOURCE


@pytest.mark.parametrize("mission_id", [True, 0, -1, "2043", None, 10 ** 18])
def test_mission_id_must_be_a_positive_bounded_int(reports, mission_id):
    (reports / NAME).write_text("x")
    assert _code(reports, mission_id, str(reports / NAME)) == UNSAFE_SOURCE


def test_non_string_path_value_is_refused(reports):
    assert _code(reports, 2043, None) == UNSAFE_SOURCE


def test_exactly_64_kib_passes_and_one_more_byte_is_refused(reports):
    (reports / NAME).write_bytes(b"a" * MAX)
    assert len(read_improve_report(reports, 2043, NAME)) == MAX
    (reports / NAME).write_bytes(b"a" * (MAX + 1))
    assert _code(reports, 2043, NAME) == TOO_LARGE


def test_a_file_that_grows_while_being_read_is_cut_off(reports, monkeypatch):
    (reports / NAME).write_bytes(b"a" * 10)
    real_fstat = os.fstat

    class _Small:
        def __init__(self, info):
            self._info = info

        def __getattr__(self, name):
            return 5 if name == "st_size" else getattr(self._info, name)

    monkeypatch.setattr(safe_open.os, "fstat", lambda fd: _Small(real_fstat(fd)))
    (reports / NAME).write_bytes(b"a" * (MAX + 10))
    assert _code(reports, 2043, NAME) == TOO_LARGE


def test_invalid_utf8_is_refused(reports):
    (reports / NAME).write_bytes(b"ok \xff\xfe")
    assert _code(reports, 2043, NAME) == UNSAFE_SOURCE


def test_error_carries_no_path(reports):
    (reports / NAME).mkdir()
    with pytest.raises(SourceError) as raised:
        read_improve_report(reports, 2043, str(reports / NAME))
    assert str(reports) not in str(raised.value) and str(reports) not in repr(raised.value)


def test_directory_is_opened_as_a_dirfd_with_nofollow(reports, monkeypatch):
    (reports / NAME).write_text("x")
    calls = []
    real_open = os.open

    def spy(path, flags, *args, **kwargs):
        calls.append((os.fspath(path), flags, kwargs.get("dir_fd")))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(safe_open.os, "open", spy)
    read_improve_report(reports, 2043, str(reports / NAME))
    assert calls[0][0] == os.fspath(reports)
    assert calls[0][1] & os.O_DIRECTORY and calls[0][1] & os.O_NOFOLLOW
    assert calls[1][0] == NAME and calls[1][2] is not None
    assert calls[1][1] & os.O_NOFOLLOW and calls[1][1] & os.O_NONBLOCK
