"""safe opener (root と component の契約、symlink・非通常ファイル・サイズ・hash の拒否)。"""
from __future__ import annotations

import logging
import os
import shutil

import pytest

from agentic_fx.ops import change_limits as L
from agentic_fx.ops import safe_open as so

from ._change_fixtures import (CONFIG, PLUGIN_PY, TEST_PY, hash_of, make_archive,
                               make_human, make_staging, make_version, write_files)


def _code(callable_):
    with pytest.raises(so.SourceError) as raised:
        callable_()
    return raised.value.code


def test_ac24_reads_each_root_with_its_component_contract(tmp_path):
    root = tmp_path / "plugins"
    digest = make_version(root, "sma")
    files = so.read_source(root, so.version_source("sma", digest), digest)
    assert files.plugin_py.decode() == PLUGIN_PY and files.test_plugin.decode() == TEST_PY
    staged = make_staging(root, 7, "sma")
    assert so.read_source(root, so.staging_source(7, "sma"), staged).config_yaml.decode() == CONFIG
    archived = make_archive(root, 7)
    assert so.read_source(root, so.archive_source(7, archived), archived)
    human = make_human(root, "sma")
    assert so.read_source(root, so.human_source("sma"), human)


@pytest.mark.parametrize("bad_name", ["", "Sma", "../x", "a/b", "1a", "a" * 65, "sma\n", "a b"])
def test_ac24_disallowed_names_are_unsafe(tmp_path, bad_name):
    assert _code(lambda: so.version_source(bad_name, "a" * 64)) == L.UNSAFE_SOURCE
    assert _code(lambda: so.human_source(bad_name)) == L.UNSAFE_SOURCE
    assert _code(lambda: so.staging_source(1, bad_name)) == L.UNSAFE_SOURCE


@pytest.mark.parametrize("bad_hash", ["", "a" * 63, "a" * 65, "A" * 64, "../" + "a" * 61,
                                      ".tmp-" + "a" * 59, "a" * 64 + "\n", None, 5])
def test_ac24_disallowed_hashes_are_unsafe(tmp_path, bad_hash):
    assert _code(lambda: so.version_source("sma", bad_hash)) == L.UNSAFE_SOURCE
    assert _code(lambda: so.archive_source(1, bad_hash)) == L.UNSAFE_SOURCE
    root = tmp_path / "plugins"
    digest = make_version(root, "sma")
    assert _code(lambda: so.read_source(root, so.version_source("sma", digest), bad_hash)) \
        == L.UNSAFE_SOURCE


@pytest.mark.parametrize("bad_mission", [0, -1, True, "1", None, 1.0, 10 ** 18])
def test_ac24_disallowed_mission_ids_are_unsafe(bad_mission):
    assert _code(lambda: so.staging_source(bad_mission, "sma")) == L.UNSAFE_SOURCE
    assert _code(lambda: so.archive_source(bad_mission, "a" * 64)) == L.UNSAFE_SOURCE


def test_ac24_tmp_prefixed_archive_dir_is_not_read(tmp_path):
    root = tmp_path / "plugins"
    write_files(root / "_archive" / "3" / (".tmp-" + "a" * 59))
    assert _code(lambda: so.archive_source(3, ".tmp-" + "a" * 59)) == L.UNSAFE_SOURCE


def test_ac24_root_that_is_not_a_directory_or_is_a_symlink_is_unsafe(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    (root / ".versions").write_text("not a dir")
    digest = hash_of(PLUGIN_PY, CONFIG, TEST_PY)
    assert _code(lambda: so.read_source(root, so.version_source("sma", digest), digest)) \
        == L.UNSAFE_SOURCE
    (root / ".versions").unlink()
    other = tmp_path / "elsewhere"
    make_version(other, "sma")
    os.symlink(other / ".versions", root / ".versions")
    assert _code(lambda: so.read_source(root, so.version_source("sma", digest), digest)) \
        == L.UNSAFE_SOURCE


def test_ac24_intermediate_symlink_rejected(tmp_path):
    root = tmp_path / "plugins"
    outside = tmp_path / "outside"
    digest = make_version(outside, "sma")
    (root / ".versions").mkdir(parents=True)
    os.symlink(outside / ".versions" / "sma", root / ".versions" / "sma")
    assert (root / ".versions" / "sma" / digest / "plugin.py").exists()
    assert _code(lambda: so.read_source(root, so.version_source("sma", digest), digest)) \
        == L.UNSAFE_SOURCE


def test_ac24_final_directory_symlink_rejected(tmp_path):
    root = tmp_path / "plugins"
    outside = tmp_path / "outside"
    digest = write_files(outside / "real")
    (root / ".versions" / "sma").mkdir(parents=True)
    os.symlink(outside / "real", root / ".versions" / "sma" / digest)
    assert _code(lambda: so.read_source(root, so.version_source("sma", digest), digest)) \
        == L.UNSAFE_SOURCE


def test_ac24_file_symlink_and_non_regular_file_rejected(tmp_path):
    root = tmp_path / "plugins"
    digest = make_version(root, "sma")
    leaf = root / ".versions" / "sma" / digest
    target = tmp_path / "secret.py"
    target.write_text(PLUGIN_PY)
    (leaf / "plugin.py").unlink()
    os.symlink(target, leaf / "plugin.py")
    assert _code(lambda: so.read_source(root, so.version_source("sma", digest), digest)) \
        == L.UNSAFE_SOURCE
    (leaf / "plugin.py").unlink()
    os.mkfifo(leaf / "plugin.py")
    assert _code(lambda: so.read_source(root, so.version_source("sma", digest), digest)) \
        == L.UNSAFE_SOURCE
    (leaf / "plugin.py").unlink()
    (leaf / "plugin.py").mkdir()
    assert _code(lambda: so.read_source(root, so.version_source("sma", digest), digest)) \
        == L.UNSAFE_SOURCE


def test_ac6_missing_sources_are_unavailable_not_unsafe(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    digest = "a" * 64
    for source in (so.version_source("sma", digest), so.staging_source(1, "sma"),
                   so.archive_source(1, digest), so.human_source("sma")):
        assert _code(lambda: so.read_source(root, source, digest)) == L.UNAVAILABLE
    make_version(root, "sma")
    assert _code(lambda: so.read_source(root, so.version_source("sma", digest), digest)) \
        == L.UNAVAILABLE


@pytest.mark.parametrize("delta,expected", [(-1, None), (0, None), (1, L.TOO_LARGE)])
def test_ac24_file_size_boundary(tmp_path, delta, expected):
    root = tmp_path / "plugins"
    big = "#" * (L.MAX_FILE_BYTES + delta - 1) + "\n"
    assert len(big.encode()) == L.MAX_FILE_BYTES + delta
    digest = make_version(root, "sma", plugin_py=big)
    source = so.version_source("sma", digest)
    if expected is None:
        assert so.read_source(root, source, digest).plugin_py == big.encode()
    else:
        assert _code(lambda: so.read_source(root, source, digest)) == expected


def test_ac24_hash_mismatch_returns_no_bytes(tmp_path):
    root = tmp_path / "plugins"
    digest = make_version(root, "sma")
    leaf = root / ".versions" / "sma" / digest
    os.chmod(leaf / "plugin.py", 0o600)
    (leaf / "plugin.py").write_text(PLUGIN_PY + "# tampered\n")
    with pytest.raises(so.SourceError) as raised:
        so.read_source(root, so.version_source("sma", digest), digest)
    assert raised.value.code == L.HASH_MISMATCH
    assert "tampered" not in repr(raised.value) and str(root) not in str(raised.value)


def test_ac24_errors_and_logs_never_contain_resolved_paths(tmp_path, caplog):
    root = tmp_path / "plugins"
    root.mkdir()
    (root / ".versions").write_text("x")
    digest = "b" * 64
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(so.SourceError) as raised:
            so.read_source(root, so.version_source("sma", digest), digest)
    text = repr(raised.value) + str(raised.value) + caplog.text
    assert str(tmp_path) not in text and "plugins" not in text
    assert raised.value.__cause__ is None and raised.value.__suppress_context__ is True


@pytest.mark.parametrize("target", [
    "../.versions/sma/" + "a" * 64,
    ".versions/other/" + "a" * 64,
    ".versions/sma/" + "a" * 63,
    ".versions/sma/" + "a" * 65,
    ".versions/sma/" + "A" * 64,
    "/abs/.versions/sma/" + "a" * 64,
    ".versions/sma/../sma/" + "a" * 64,
    ".versions//sma/" + "a" * 64,
    ".versions/sma/" + "a" * 64 + "/",
    ".versions/sma/" + "a" * 64 + "\n",
    "plugins/.versions/sma/" + "a" * 64,
    None,
])
def test_ac24_baseline_target_must_match_exactly(target):
    assert so.baseline_hash_from_target("sma", target) is None


def test_ac24_baseline_target_exact_match_extracts_the_hash():
    assert so.baseline_hash_from_target("sma", ".versions/sma/" + "c" * 64) == "c" * 64


def test_ac24_inspect_live_kinds(tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    assert so.inspect_live(root, "sma").kind == "absent"
    (root / "sma").mkdir()
    assert so.inspect_live(root, "sma").kind == "dir"
    shutil.rmtree(root / "sma")
    os.symlink(".versions/sma/" + "a" * 64, root / "sma")
    live = so.inspect_live(root, "sma")
    assert live.kind == "symlink" and live.target == ".versions/sma/" + "a" * 64
    (root / "sma").unlink()
    (root / "sma").write_text("x")
    assert so.inspect_live(root, "sma").kind == "other"
