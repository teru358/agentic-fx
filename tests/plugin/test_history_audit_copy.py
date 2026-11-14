"""保存版のコピー元の検査と安全コピー (`copy_exact_version`)。

元の 3 ファイルは dirfd を固定して `stat(follow_symlinks=False)` で全部検査し、通常ファイル・
リンク数 1・サイズ上限を満たすものだけを、検査した fd から読む。どれか 1 つでも不適合なら、
他のファイルを開く前にコピー前で拒否する。"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agentic_fx.plugin import history_audit as audit
from agentic_fx.plugin import loader
from agentic_fx.plugin.gate_pytest import CandidateSnapshotError
from agentic_fx.plugin.loader import REQUIRED_FILES, _MAX_FILE_BYTES
from tests.plugin.audit_world import never_block

NAME = "sma"
FILES = {"plugin.py": b"def evaluate(df, indicators, signals, params):\n    return {}\n",
         "config.yaml": b"kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nexit_mode: levels\n",
         "test_plugin.py": b"def test_plugin():\n    pass\n"}


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "plugins"
    staging = root / ".versions" / NAME / "tmp"
    staging.mkdir(parents=True)
    for name, data in FILES.items():
        (staging / name).write_bytes(data)
    digest = loader.artifact_hash_bytes(*(FILES[n] for n in REQUIRED_FILES))
    version = staging.with_name(digest)
    staging.rename(version)
    content = loader.content_hash(version)
    dest = tmp_path / "work" / "copy"
    dest.parent.mkdir()
    return root, version, content, dest


def _copy(store, **kwargs):
    root, version, content, dest = store
    audit.copy_exact_version(root, NAME, version.name, dest, content_hash=content, **kwargs)


def _code(store, **kwargs) -> str:
    with pytest.raises(audit.VersionCopyError) as raised:
        _copy(store, **kwargs)
    return raised.value.code


@pytest.fixture(autouse=True)
def _never_block():
    with never_block():
        yield


def test_a_clean_version_is_copied_byte_for_byte_with_both_hashes_checked(store):
    root, version, content, dest = store
    _copy(store)
    for name, data in FILES.items():
        copied = dest / name
        assert copied.read_bytes() == data
        st = copied.lstat()
        assert stat.S_ISREG(st.st_mode) and st.st_nlink == 1
        assert st.st_ino != (version / name).lstat().st_ino      # not a hardlink
    assert loader.content_hash(dest) == content
    assert sorted(p.name for p in dest.iterdir()) == sorted(FILES)


def test_the_copy_runs_the_existing_snapshot_check_and_the_hash_comparison_on_the_destination(
        store, monkeypatch):
    root, version, content, dest = store
    seen = {}
    real_check, real_hashes = audit.check_candidate_snapshot, audit.hashes_of
    monkeypatch.setattr(audit, "check_candidate_snapshot",
                        lambda d: (seen.setdefault("check", d), real_check(d))[1])
    monkeypatch.setattr(audit, "hashes_of",
                        lambda d: (seen.setdefault("hashes", d), real_hashes(d))[1])
    _copy(store)
    assert seen == {"check": dest, "hashes": dest}


def test_a_corrupt_destination_is_refused_and_removed(store, monkeypatch):
    root, version, content, dest = store
    monkeypatch.setattr(audit, "hashes_of", lambda d: ("x" * 64, "y" * 64))
    assert _code(store) == "hash_mismatch"
    assert not dest.exists()


def test_a_destination_that_fails_the_snapshot_check_is_refused_and_removed(store, monkeypatch):
    def broken(d):
        raise CandidateSnapshotError("bad")

    monkeypatch.setattr(audit, "check_candidate_snapshot", broken)
    assert _code(store) == "invalid_snapshot" and not store[3].exists()


def test_a_recorded_hash_that_differs_from_the_stored_bytes_is_refused(store):
    root, version, content, dest = store
    with pytest.raises(audit.VersionCopyError) as raised:
        audit.copy_exact_version(root, NAME, version.name, dest, content_hash="d" * 64)
    assert raised.value.code == "hash_mismatch" and not dest.exists()


def test_an_existing_destination_is_never_reused_or_removed(store):
    root, version, content, dest = store
    dest.mkdir()
    (dest / "keep.txt").write_text("mine")
    assert _code(store) == "resource_unavailable"
    assert (dest / "keep.txt").read_text() == "mine"


# ---- 元の検査 ---------------------------------------------------------------------

def _symlink(version: Path, name: str, tmp_path: Path):
    target = tmp_path / f"real-{name}"
    target.write_bytes(FILES[name])                   # 正しい bytes を指す symlink
    (version / name).unlink()
    os.symlink(target, version / name)


def _hardlink(version: Path, name: str, tmp_path: Path):
    other = tmp_path / f"other-{name}"
    other.write_bytes(FILES[name])
    (version / name).unlink()
    os.link(other, version / name)                    # 同じ bytes、リンク数 2


def _fifo(version: Path, name: str, tmp_path: Path):
    (version / name).unlink()
    os.mkfifo(version / name)


def _oversize(version: Path, name: str, tmp_path: Path):
    (version / name).write_bytes(b"#" * (_MAX_FILE_BYTES + 1))


def _missing(version: Path, name: str, tmp_path: Path):
    (version / name).unlink()


BREAKERS = [("symlink", _symlink, "invalid_snapshot"), ("hardlink", _hardlink, "invalid_snapshot"),
            ("fifo", _fifo, "invalid_snapshot"), ("oversize", _oversize, "invalid_snapshot"),
            ("missing", _missing, "artifact_missing")]


@pytest.mark.parametrize("name", REQUIRED_FILES)
@pytest.mark.parametrize("label,breaker,code", BREAKERS, ids=[b[0] for b in BREAKERS])
def test_each_required_file_is_inspected_and_a_bad_one_is_refused_before_the_copy(
        store, tmp_path, name, label, breaker, code):
    root, version, content, dest = store
    breaker(version, name, tmp_path)
    assert _code(store) == code
    assert not dest.exists()


@pytest.mark.parametrize("label,breaker,code", BREAKERS, ids=[b[0] for b in BREAKERS])
def test_no_file_is_opened_when_the_last_one_is_bad(store, tmp_path, monkeypatch, label,
                                                    breaker, code):
    """検査は全 3 ファイルを先に終える。最後の 1 つが不適合なら、他のファイルも開かない。"""
    root, version, content, dest = store
    breaker(version, "test_plugin.py", tmp_path)
    opened = []
    real = os.open

    def spy(path, flags, *args, **kwargs):
        if kwargs.get("dir_fd") is not None and str(path) in REQUIRED_FILES:
            opened.append(str(path))
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    assert _code(store) == code
    assert opened == []


def test_an_unexpected_entry_in_the_stored_version_is_refused(store):
    root, version, content, dest = store
    (version / "extra.py").write_text("x = 1\n")
    assert _code(store) == "invalid_snapshot"
    (version / "extra.py").unlink()
    (version / "__pycache__").mkdir()
    (version / "plugin.cpython-312.pyc").write_bytes(b"\0")
    _copy(store)
    assert sorted(p.name for p in dest.iterdir()) == sorted(FILES)       # only the 3 are copied


def test_a_version_directory_that_is_a_symlink_or_missing_is_refused(store, tmp_path):
    root, version, content, dest = store
    moved = tmp_path / "moved"
    version.rename(moved)
    os.symlink(moved, version)
    assert _code(store) == "invalid_snapshot"
    version.unlink()
    assert _code(store) == "artifact_missing"


def test_a_versions_directory_that_is_a_symlink_is_refused(store, tmp_path):
    root, version, content, dest = store
    moved = tmp_path / "versions-moved"
    (root / ".versions").rename(moved)
    os.symlink(moved, root / ".versions")
    assert _code(store) == "invalid_snapshot"


@pytest.mark.parametrize("name,artifact", [("Bad", "a" * 64), (NAME, "short"),
                                           ("../escape", "a" * 64)])
def test_a_non_canonical_locator_is_refused(store, name, artifact):
    root, _version, content, dest = store
    with pytest.raises(audit.VersionCopyError) as raised:
        audit.copy_exact_version(root, name, artifact, dest, content_hash=content)
    assert raised.value.code == "invalid_snapshot"


# ---- 検査後の差替え ----------------------------------------------------------------

def _swap_to_fifo(version):
    (version / "plugin.py").unlink()
    os.mkfifo(version / "plugin.py")


def _swap_to_other_inode(version):
    (version / "plugin.py").unlink()
    (version / "plugin.py").write_bytes(FILES["plugin.py"])       # same bytes, new inode


def _grow_in_place(version):
    with open(version / "plugin.py", "ab") as handle:
        handle.write(b"# more\n")


def _swap_to_symlink(version):
    target = version.parent / "real.py"
    target.write_bytes(FILES["plugin.py"])
    (version / "plugin.py").unlink()
    os.symlink(target, version / "plugin.py")


def _swap_to_hardlinked(version):
    os.link(version / "config.yaml", version.parent / "alias")


@pytest.mark.parametrize("swap", [_swap_to_fifo, _swap_to_other_inode, _grow_in_place,
                                  _swap_to_symlink, _swap_to_hardlinked])
def test_a_swap_between_the_inspection_and_the_read_is_detected(store, swap):
    root, version, content, dest = store
    assert _code(store, between_checks_and_reads=lambda: swap(version)) == (
        "changed_during_evaluation")
    assert not dest.exists()


def test_only_the_inspected_descriptor_is_read(store, monkeypatch):
    """検査後にパスを通常 open し直す方式を使わない: 読取りは `O_NOFOLLOW | O_NONBLOCK` の fd だけ。"""
    root, version, content, dest = store
    flags_seen = []
    real = os.open

    def spy(path, flags, *args, **kwargs):
        if kwargs.get("dir_fd") is not None and str(path) in REQUIRED_FILES:
            flags_seen.append(flags)
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    real_read = Path.read_bytes

    def guarded(self):
        if version in self.parents:
            raise AssertionError("the source must not be re-read by path")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    _copy(store)
    source_opens = flags_seen[:3]
    assert len(source_opens) == 3
    for flags in source_opens:
        assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
