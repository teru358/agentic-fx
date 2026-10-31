"""Parent-only indicator fixture cache: discipline, verification and sandbox invisibility."""
from __future__ import annotations

import json
import multiprocessing
import os
import stat
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from agentic_fx.plugin import indicator_fixture_cache as cache
from agentic_fx.plugin.canonical_fixture import aggregate_to, build_canonical_5m, fixture_data_hash

from . import _indicator_cache_proc as proc
from ._worker_isolation_support import cfg_for, copy_plugin, needs_sandbox, run_child

H = "a" * 64
D1 = "b" * 64
D2 = "c" * 64


def key(content=H, data=D1, tf="1h"):
    return cache.CacheKey(content, data, tf)


def entry(shift=0.0):
    idx = pd.date_range("2026-01-05", periods=6, freq="1h", tz="UTC")
    return cache.CacheEntry(
        {"ma": pd.Series([np.nan, 1.0 + shift, 2.0, np.nan, 4.5, 5.0], index=idx),
         "rng": pd.Series(np.linspace(0, 1, 6), index=idx), "last": 3.25, "nan_scalar": float("nan")},
        {"ma": ("proportional", "invariant"), "rng": ("invariant", "neither")})


def path_of(data_dir, k=None):
    return cache.cache_dir(data_dir) / (k or key()).filename


def same(a: cache.CacheEntry, b: cache.CacheEntry):
    assert list(a.outputs) == list(b.outputs)
    assert a.classifications == b.classifications
    for name, value in a.outputs.items():
        other = b.outputs[name]
        if isinstance(value, pd.Series):
            pd.testing.assert_series_equal(value, other, check_names=False, check_freq=False)
        else:
            assert (np.isnan(value) and np.isnan(other)) or value == other


def build(data_dir, k=None, e=None):
    calls = []

    def builder():
        calls.append(1)
        return e or entry()

    got, built = cache.get_or_build(data_dir, k or key(), builder)
    return got, built, len(calls)


def test_round_trip_keeps_series_scalars_nan_index_and_classes(tmp_path):
    got, built, n = build(tmp_path)
    assert built and n == 1
    again = cache.read(tmp_path, key())
    same(entry(), again)
    assert again.outputs["ma"].index.tz is not None and str(again.outputs["ma"].index.tz) == "UTC"


def test_second_get_or_build_does_not_call_the_builder(tmp_path):
    build(tmp_path)
    _, built, n = build(tmp_path)
    assert not built and n == 0


def test_permissions_are_0700_directory_and_0600_files(tmp_path):
    build(tmp_path)
    d = cache.cache_dir(tmp_path)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    files = sorted(os.listdir(d))
    assert key().filename in files
    for name in files:
        st = (d / name).lstat()
        assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600
    assert not [f for f in files if ".tmp-" in f]


def test_a_loose_existing_directory_is_tightened(tmp_path):
    d = cache.cache_dir(tmp_path)
    d.mkdir(parents=True)
    d.chmod(0o777)
    build(tmp_path)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_a_symlinked_cache_directory_is_refused(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (tmp_path / cache.CACHE_DIR_NAME).symlink_to(real)
    with pytest.raises(cache.IndicatorCacheError):
        build(tmp_path)
    assert os.listdir(real) == []


def test_a_symlinked_file_is_rejected_and_replaced_without_touching_the_target(tmp_path):
    build(tmp_path)
    p = path_of(tmp_path)
    target = tmp_path / "target.npz"
    target.write_bytes(p.read_bytes())
    target.chmod(0o600)
    before = target.read_bytes()
    p.unlink()
    p.symlink_to(target)
    assert cache.read(tmp_path, key()) is None
    _, built, n = build(tmp_path, e=entry(shift=7.0))
    assert built and n == 1
    assert not p.is_symlink() and p.lstat().st_nlink == 1
    assert target.read_bytes() == before


def test_a_hardlinked_file_is_rejected_and_replaced(tmp_path):
    build(tmp_path)
    p = path_of(tmp_path)
    other = tmp_path / "other-link"
    os.link(p, other)
    assert cache.read(tmp_path, key()) is None
    _, built, _ = build(tmp_path)
    assert built and p.lstat().st_nlink == 1
    assert other.exists()


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400, 0o666])
def test_a_file_with_the_wrong_mode_is_rejected(tmp_path, mode):
    build(tmp_path)
    path_of(tmp_path).chmod(mode)
    assert cache.read(tmp_path, key()) is None
    _, built, _ = build(tmp_path)
    assert built
    assert stat.S_IMODE(path_of(tmp_path).stat().st_mode) == 0o600


def test_a_fifo_or_directory_in_place_of_the_file_is_not_read(tmp_path):
    d = cache.cache_dir(tmp_path)
    d.mkdir(parents=True)
    os.mkfifo(path_of(tmp_path), 0o600)
    assert cache.read(tmp_path, key()) is None


@pytest.mark.parametrize("damage", ["garbage", "truncate", "empty"])
def test_a_damaged_file_is_rejected_and_rebuilt(tmp_path, damage):
    build(tmp_path)
    p = path_of(tmp_path)
    raw = p.read_bytes()
    p.write_bytes({"garbage": b"not a zip" * 50, "truncate": raw[: len(raw) // 2], "empty": b""}[damage])
    assert cache.read(tmp_path, key()) is None
    got, built, n = build(tmp_path)
    assert built and n == 1
    same(entry(), cache.read(tmp_path, key()))


def test_an_entry_stored_under_another_content_hash_is_rejected(tmp_path):
    build(tmp_path, key(content="1" * 64))
    src = path_of(tmp_path, key(content="1" * 64))
    dst = path_of(tmp_path, key(content="2" * 64))
    dst.write_bytes(src.read_bytes())
    dst.chmod(0o600)
    assert cache.read(tmp_path, key(content="2" * 64)) is None
    assert cache.read(tmp_path, key(content="1" * 64)) is not None


def test_an_entry_stored_under_another_fixture_hash_or_timeframe_is_rejected(tmp_path):
    build(tmp_path)
    for other in (key(data=D2), key(tf="4h")):
        dst = path_of(tmp_path, other)
        dst.write_bytes(path_of(tmp_path).read_bytes())
        dst.chmod(0o600)
        assert cache.read(tmp_path, other) is None


def test_a_different_schema_version_is_rejected(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(cache, "SCHEMA", cache.SCHEMA + 1)
        build(tmp_path)
    assert cache.read(tmp_path, key()) is None


def test_pickled_object_arrays_are_never_loaded(tmp_path):
    d = cache.cache_dir(tmp_path)
    d.mkdir(parents=True)
    p = path_of(tmp_path)
    np.savez(p, meta=np.array(json.dumps({"schema": 1})), index=np.array([object()], dtype=object))
    p.chmod(0o600)
    assert cache.read(tmp_path, key()) is None


def test_the_key_changes_with_the_fixture_data_hash():
    a = aggregate_to(build_canonical_5m(seed=1, days=4), "1h")
    b = aggregate_to(build_canonical_5m(seed=2, days=4), "1h")
    ka = cache.CacheKey(H, fixture_data_hash(a), "1h")
    kb = cache.CacheKey(H, fixture_data_hash(b), "1h")
    assert ka.filename != kb.filename and ka.filename.startswith(f"{H}-")


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "a b", "a.npz", "a" * 129, None, 123, b"abc"])
def test_key_parts_cannot_escape_the_directory(bad):
    for args in ((bad, D1, "1h"), (H, bad, "1h"), (H, D1, bad)):
        with pytest.raises(ValueError):
            cache.CacheKey(*args)


def test_a_failing_builder_leaves_no_file(tmp_path):
    def boom():
        raise RuntimeError("builder failed")

    with pytest.raises(RuntimeError):
        cache.get_or_build(tmp_path, key(), boom)
    names = os.listdir(cache.cache_dir(tmp_path))
    assert key().filename not in names and not [n for n in names if ".tmp-" in n]


def test_a_failed_rename_keeps_the_old_content_and_removes_the_temp_file(tmp_path, monkeypatch):
    build(tmp_path)
    before = path_of(tmp_path).read_bytes()

    def broken_rename(*args, **kwargs):
        raise OSError("rename failed")

    monkeypatch.setattr(os, "rename", broken_rename)
    with pytest.raises(OSError):
        cache.write(tmp_path, key(), entry(shift=9.0))
    assert path_of(tmp_path).read_bytes() == before
    assert not [n for n in os.listdir(cache.cache_dir(tmp_path)) if ".tmp-" in n]


def test_the_new_content_appears_only_through_a_rename_after_fsync(tmp_path, monkeypatch):
    order = []
    real_fsync, real_rename = os.fsync, os.rename
    monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "rename", lambda *a, **k: (order.append("rename"), real_rename(*a, **k))[1])
    cache.write(tmp_path, key(), entry())
    assert order[:2] == ["fsync", "rename"]


def test_concurrent_builders_for_one_key_build_once(tmp_path):
    calls = []
    start = threading.Barrier(2)

    def builder():
        calls.append(1)
        time.sleep(0.3)
        return entry()

    def worker():
        start.wait()
        cache.get_or_build(tmp_path, key(), builder)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(calls) == 1


def test_different_keys_do_not_block_each_other(tmp_path):
    gate = threading.Event()
    out = []

    def slow():
        gate.wait(10)
        return entry()

    t = threading.Thread(target=lambda: cache.get_or_build(tmp_path, key(), slow))
    t.start()
    time.sleep(0.2)
    cache.get_or_build(tmp_path, key(content="9" * 64), entry)
    out.append("other done while first still building")
    gate.set()
    t.join(30)
    assert out


def test_gc_removes_only_other_fixture_generations(tmp_path):
    build(tmp_path, key(data=D1))
    build(tmp_path, key(content="d" * 64, data=D1))
    build(tmp_path, key(data=D2))
    d = cache.cache_dir(tmp_path)
    (d / "notes.txt").write_text("keep me")
    stale_tmp = d / f"{H}-{D1}-1h.tmp-1-ab"
    stale_tmp.write_bytes(b"x")
    removed = cache.gc_stale(tmp_path, D2)
    names = set(os.listdir(d))
    assert key(data=D2).filename in names
    assert f"{H}-{D2}-1h.lock" in names
    assert not [n for n in names if f"-{D1}-" in n]
    assert "notes.txt" in names
    assert set(removed) >= {key(data=D1).filename, f"{H}-{D1}-1h.lock", stale_tmp.name}


def test_cache_directory_is_under_data_and_named_for_the_cache(tmp_path):
    assert cache.cache_dir(tmp_path) == tmp_path / "indicator_fixture_cache"


# --- sandbox invisibility ------------------------------------------------------------

@needs_sandbox
def test_a_sandboxed_worker_cannot_read_or_list_the_cache(tmp_path):
    repo = tmp_path / "repo"
    data = repo / "data"
    build(data)
    canary = path_of(data)
    main = copy_plugin("rsi", tmp_path / "plugins")
    post = (f"out['read'] = []\n"
            f"for target in ({str(canary)!r}, {str(canary.parent)!r}):\n"
            f"    try:\n"
            f"        fd = os.open(target, os.O_RDONLY)\n"
            f"        os.close(fd)\n"
            f"        out['read'].append('opened')\n"
            f"    except OSError as e:\n"
            f"        out['read'].append(e.errno)\n")
    res = run_child(cfg_for(main, post=post, repo_root=str(repo), home=str(tmp_path / "home")))
    assert res.rc == 0, res.stderr
    assert res.out["read"] == [13, 13]


@needs_sandbox
def test_the_sandbox_allowlist_never_covers_the_cache_directory(tmp_path):
    from agentic_fx.core import landlock

    repo = tmp_path / "repo"
    build(repo / "data")
    main = copy_plugin("rsi", tmp_path / "plugins")
    allow = landlock.build_allowlist([main], repo_root=repo, home=tmp_path / "home")
    try:
        target = cache.cache_dir(repo / "data").resolve()
        for rule in allow.rules:
            assert landlock.path_relation(target, Path(rule.path).resolve()) is None
    finally:
        allow.close_all()


# --- key grammar ------------------------------------------------------------------------

def test_key_length_and_character_limits_are_exact():
    assert cache.CacheKey("a" * 128, "b" * 128, "t" * 16).filename.endswith(".npz")
    with pytest.raises(ValueError):
        cache.CacheKey("a" * 129, D1, "1h")
    with pytest.raises(ValueError):
        cache.CacheKey(H, "b" * 129, "1h")
    with pytest.raises(ValueError):
        cache.CacheKey(H, D1, "t" * 17)
    with pytest.raises(ValueError):
        cache.CacheKey(H, D1, "a-b")
    assert cache.CacheKey(H, D1, "m_5").stem.endswith("-m_5")


# --- stand-ins for the os module, to observe or change one call ---------------------------

class _Os:
    """Delegates to ``os`` except for the attributes given."""

    def __init__(self, **over):
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(os, name)


def _with_stat(**changes):
    def fstat(fd):
        st = os.fstat(fd)
        fields = list(st)
        names = ["st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size"]
        for i, n in enumerate(names):
            if n in changes:
                fields[i] = changes[n]
        return os.stat_result(fields)
    return fstat


def _open_cache_dir(data_dir):
    return cache._open_dir(cache.cache_dir(data_dir))


# --- ownership --------------------------------------------------------------------------

def test_a_directory_owned_by_another_user_is_refused(tmp_path, monkeypatch):
    build(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(cache, "os", _Os(geteuid=lambda: os.geteuid() + 1))
        with pytest.raises(cache.IndicatorCacheError, match="another user"):
            cache.read(tmp_path, key())
        with pytest.raises(cache.IndicatorCacheError, match="another user"):
            build(tmp_path)


def test_a_new_directory_is_created_private_from_the_start(tmp_path, monkeypatch):
    made = []
    real = os.mkdir
    with monkeypatch.context() as patch:
        patch.setattr(cache, "os", _Os(mkdir=lambda path, mode=0o777: (made.append(mode), real(path, mode))[1]))
        old = os.umask(0)
        try:
            build(tmp_path)
        finally:
            os.umask(old)
    assert made == [0o700]
    assert stat.S_IMODE(cache.cache_dir(tmp_path).stat().st_mode) == 0o700


def test_a_file_owned_by_another_user_or_not_regular_is_not_read(tmp_path, monkeypatch):
    build(tmp_path)
    with _open_cache_dir(tmp_path) as dir_fd:
        assert cache._read(dir_fd, key()) is not None
        with monkeypatch.context() as patch:
            patch.setattr(cache, "os", _Os(geteuid=lambda: os.geteuid() + 1))
            assert cache._read(dir_fd, key()) is None
        with monkeypatch.context() as patch:
            patch.setattr(cache, "os", _Os(fstat=_with_stat(st_mode=stat.S_IFSOCK | 0o600)))
            assert cache._read(dir_fd, key()) is None
        with monkeypatch.context() as patch:
            patch.setattr(cache, "os", _Os(fstat=_with_stat(st_mode=stat.S_IFDIR | 0o600)))
            assert cache._read(dir_fd, key()) is None


def test_the_lock_file_must_be_a_private_regular_file_of_this_user(tmp_path, monkeypatch):
    build(tmp_path)
    lock = cache.cache_dir(tmp_path) / f"{key().stem}.lock"
    other = tmp_path / "other-link"
    os.link(lock, other)
    with pytest.raises(cache.IndicatorCacheError, match="lock"):
        build(tmp_path)
    other.unlink()
    build(tmp_path)
    lock.unlink()
    os.mkfifo(lock, 0o600)
    with pytest.raises(cache.IndicatorCacheError, match="lock"):
        build(tmp_path)
    lock.unlink()
    build(tmp_path)
    with _open_cache_dir(tmp_path) as dir_fd:
        with monkeypatch.context() as patch:
            patch.setattr(cache, "os", _Os(geteuid=lambda: os.geteuid() + 1))
            with pytest.raises(cache.IndicatorCacheError, match="lock"):
                with cache._key_lock(dir_fd, key().stem):
                    pass


def test_a_loose_lock_file_is_tightened(tmp_path):
    build(tmp_path)
    lock = cache.cache_dir(tmp_path) / f"{key().stem}.lock"
    lock.chmod(0o666)
    build(tmp_path)
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_a_symlinked_lock_file_is_refused_and_its_target_is_left_alone(tmp_path):
    build(tmp_path)
    lock = cache.cache_dir(tmp_path) / f"{key().stem}.lock"
    target = tmp_path / "lock-target"
    target.write_bytes(b"")
    target.chmod(0o644)
    lock.unlink()
    lock.symlink_to(target)
    with pytest.raises(OSError):
        build(tmp_path)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644 and lock.is_symlink()


# --- temp file discipline ----------------------------------------------------------------

def _fixed_tmp_name(k):
    return f"{k.stem}.tmp-{os.getpid()}-{'01' * 4}"


def test_a_temp_name_that_already_exists_is_never_reused(tmp_path, monkeypatch):
    build(tmp_path)
    d = cache.cache_dir(tmp_path)
    planted = d / _fixed_tmp_name(key())
    planted.write_bytes(b"keep")
    before = path_of(tmp_path).read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(cache, "os", _Os(urandom=lambda n: b"\x01" * n))
        with pytest.raises(FileExistsError):
            cache.write(tmp_path, key(), entry(shift=3.0))
    assert planted.read_bytes() == b"keep" and path_of(tmp_path).read_bytes() == before


def test_a_symlink_in_place_of_the_temp_name_is_not_written_through(tmp_path, monkeypatch):
    build(tmp_path)
    d = cache.cache_dir(tmp_path)
    target = tmp_path / "elsewhere"
    target.write_bytes(b"untouched")
    (d / _fixed_tmp_name(key())).symlink_to(target)
    with monkeypatch.context() as patch:
        patch.setattr(cache, "os", _Os(urandom=lambda n: b"\x01" * n))
        with pytest.raises(OSError):
            cache.write(tmp_path, key(), entry(shift=3.0))
    assert target.read_bytes() == b"untouched"


def test_files_are_0600_even_under_a_permissive_umask(tmp_path):
    old = os.umask(0)
    try:
        build(tmp_path)
    finally:
        os.umask(old)
    for name in os.listdir(cache.cache_dir(tmp_path)):
        assert stat.S_IMODE((cache.cache_dir(tmp_path) / name).lstat().st_mode) == 0o600


def test_the_directory_is_synced_after_the_rename_and_the_file_before_it(tmp_path, monkeypatch):
    events = []
    real_fsync, real_rename = os.fsync, os.rename

    def fsync(fd):
        events.append(("fsync", stat.S_ISDIR(os.fstat(fd).st_mode)))
        real_fsync(fd)

    def rename(*a, **k):
        events.append(("rename",))
        real_rename(*a, **k)

    with monkeypatch.context() as patch:
        patch.setattr(cache, "os", _Os(fsync=fsync, rename=rename))
        cache.write(tmp_path, key(), entry())
    assert events == [("fsync", False), ("rename",), ("fsync", True)]


def test_a_short_write_is_continued_until_the_whole_entry_is_stored(tmp_path, monkeypatch):
    real_write = os.write
    with monkeypatch.context() as patch:
        patch.setattr(cache, "os", _Os(write=lambda fd, data: real_write(fd, bytes(data[:7]))))
        cache.write(tmp_path, key(), entry())
    same(entry(), cache.read(tmp_path, key()))


# --- encoding limits ----------------------------------------------------------------------

def test_series_must_have_a_datetime_index_and_share_it(tmp_path):
    idx = pd.date_range("2026-01-05", periods=3, freq="1h", tz="UTC")
    later = pd.date_range("2026-02-05", periods=3, freq="1h", tz="UTC")
    for outputs in ({"a": pd.Series([1.0, 2.0, 3.0])},
                    {"a": pd.Series([1.0, 2.0, 3.0], index=idx), "b": pd.Series([1.0, 2.0, 3.0], index=later)}):
        with pytest.raises(cache.IndicatorCacheError):
            cache.write(tmp_path, key(), cache.CacheEntry(outputs))
    assert not [n for n in os.listdir(cache.cache_dir(tmp_path)) if n.endswith(".npz") or ".tmp-" in n]


def test_an_entry_over_the_size_limit_is_not_written(tmp_path, monkeypatch):
    size = len(cache._encode(key(), entry()))
    with monkeypatch.context() as patch:
        patch.setattr(cache, "MAX_FILE_BYTES", size)
        cache.write(tmp_path, key(), entry())
        same(entry(), cache.read(tmp_path, key()))
    with monkeypatch.context() as patch:
        patch.setattr(cache, "MAX_FILE_BYTES", size - 1)
        with pytest.raises(cache.IndicatorCacheError, match="size"):
            cache.write(tmp_path, key(content="7" * 64), entry())
        calls = []
        with pytest.raises(cache.IndicatorCacheError):
            cache.get_or_build(tmp_path, key(content="8" * 64), lambda: (calls.append(1), entry())[1])
        assert calls == [1]
    names = os.listdir(cache.cache_dir(tmp_path))
    assert not [n for n in names if n.endswith(".npz") and not n.startswith(H)]
    assert not [n for n in names if ".tmp-" in n]


def test_a_stored_file_over_the_limit_is_refused_without_being_read(tmp_path, monkeypatch):
    build(tmp_path)
    size = path_of(tmp_path).stat().st_size
    opened = []
    with monkeypatch.context() as patch:
        patch.setattr(cache, "MAX_FILE_BYTES", size - 1)
        patch.setattr(cache, "os", _Os(fdopen=lambda *a, **k: opened.append(1)))
        assert cache.read(tmp_path, key()) is None
    assert opened == []
    with monkeypatch.context() as patch:
        patch.setattr(cache, "MAX_FILE_BYTES", size)
        assert cache.read(tmp_path, key()) is not None


def test_a_file_that_grew_after_it_was_measured_is_refused_by_its_length(tmp_path, monkeypatch):
    build(tmp_path)
    size = path_of(tmp_path).stat().st_size
    with monkeypatch.context() as patch:
        patch.setattr(cache, "MAX_FILE_BYTES", size - 1)
        patch.setattr(cache, "os", _Os(fstat=_with_stat(st_size=1)))
        assert cache.read(tmp_path, key()) is None


# --- schema checks on a validly zipped entry ------------------------------------------------

def forge(tmp_path, edit, *, drop=()):
    build(tmp_path)
    p = path_of(tmp_path)
    with np.load(p) as z:
        arrays = {n: z[n] for n in z.files}
    meta = json.loads(str(arrays.pop("meta")))
    meta = edit(meta)
    for name in drop:
        arrays.pop(name)
    np.savez(p, meta=np.array(json.dumps(meta)), **arrays)
    p.chmod(0o600)


def _col(meta, i=0):
    return meta["columns"][i]


def _set(i, **kw):
    def edit(meta):
        _col(meta, i).update(kw)
        return meta
    return edit


def _dup(meta):
    meta["columns"][1]["name"] = meta["columns"][0]["name"]
    return meta


def _del(i, field):
    def edit(meta):
        del _col(meta, i)[field]
        return meta
    return edit


BAD_ENTRIES = {
    "meta_null": lambda m: None,
    "meta_list": lambda m: [],
    "meta_string": lambda m: "x",
    "columns_null": lambda m: {**m, "columns": None},
    "columns_dict": lambda m: {**m, "columns": {}},
    "column_null": lambda m: {**m, "columns": [None] + m["columns"][1:]},
    "column_list": lambda m: {**m, "columns": [[]] + m["columns"][1:]},
    "duplicate_name": _dup,
    "unknown_kind": _set(0, kind="matrix"),
    "unknown_kind_on_a_scalar": _set(2, kind="matrix"),
    "kind_missing": _del(0, "kind"),
    "name_not_str": _set(0, name=5),
    "name_missing": _del(0, "name"),
    "price_null": _set(0, price=None),
    "price_not_str": _set(0, price=3),
    "volume_missing": _del(0, "volume"),
    "array_missing_from_file": _set(0, array="s99"),
    "array_not_str": _set(0, array=1),
    "slot_out_of_range": _set(2, slot=99),
    "slot_negative": _set(2, slot=-1),
    "slot_bool": _set(2, slot=True),
    "slot_not_int": _set(2, slot="0"),
    "unit_unknown": lambda m: {**m, "unit": "hour"},
    "unit_missing": lambda m: {k: v for k, v in m.items() if k != "unit"},
    "unit_list": lambda m: {**m, "unit": ["ns"]},
}


@pytest.mark.parametrize("name", sorted(BAD_ENTRIES))
def test_a_validly_zipped_entry_with_a_bad_schema_is_a_miss_and_is_rebuilt(tmp_path, name):
    forge(tmp_path, BAD_ENTRIES[name])
    assert cache.read(tmp_path, key()) is None
    _, built, n = build(tmp_path)
    assert built and n == 1
    same(entry(), cache.read(tmp_path, key()))


def test_a_series_whose_length_differs_from_the_index_is_a_miss(tmp_path):
    build(tmp_path)
    p = path_of(tmp_path)
    with np.load(p) as z:
        arrays = {n: z[n] for n in z.files}
    arrays["s0"] = arrays["s0"][:-1]
    np.savez(p, **arrays)
    p.chmod(0o600)
    assert cache.read(tmp_path, key()) is None


def test_a_two_dimensional_series_array_is_a_miss(tmp_path):
    build(tmp_path)
    p = path_of(tmp_path)
    with np.load(p) as z:
        arrays = {n: z[n] for n in z.files}
    arrays["s0"] = np.stack([arrays["s0"], arrays["s0"]], axis=1)
    np.savez(p, **arrays)
    p.chmod(0o600)
    assert cache.read(tmp_path, key()) is None


def test_the_good_forged_baseline_still_reads(tmp_path):
    forge(tmp_path, lambda m: m)
    same(entry(), cache.read(tmp_path, key()))


# --- locks across processes ------------------------------------------------------------------

def _start_holder(tmp_path, *, content=H, data=D1, tf="1h", plant_tmp=False):
    ctx = multiprocessing.get_context("spawn")
    started, release = tmp_path / "started", tmp_path / "release"
    child = ctx.Process(target=proc.hold_key, args=(str(tmp_path), content, data, tf,
                                                    str(started), str(release), plant_tmp))
    child.start()
    deadline = time.monotonic() + 60
    while not started.exists():
        assert child.is_alive() and time.monotonic() < deadline, "holder did not start"
        time.sleep(0.02)
    return child, release


def _finish(child, release):
    release.write_text("go")
    child.join(60)
    if child.is_alive():
        child.terminate()
        child.join(10)
    assert child.exitcode == 0


def _in_thread(fn):
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            out["error"] = exc
        out["done"] = time.monotonic()

    t = threading.Thread(target=run)
    t.start()
    return t, out


def test_a_builder_in_another_process_is_waited_for_and_its_entry_reused(tmp_path):
    child, release = _start_holder(tmp_path)
    try:
        calls = []
        t, out = _in_thread(lambda: cache.get_or_build(
            tmp_path, key(), lambda: (calls.append(1), entry())[1]))
        time.sleep(0.6)
        assert t.is_alive() and calls == []
    finally:
        _finish(child, release)
    t.join(30)
    assert not t.is_alive() and "error" not in out
    assert calls == [] and out["value"][1] is False


def test_write_alone_takes_the_key_lock_held_by_another_process(tmp_path):
    child, release = _start_holder(tmp_path)
    try:
        t, out = _in_thread(lambda: cache.write(tmp_path, key(), entry(shift=5.0)))
        time.sleep(0.6)
        assert t.is_alive()
    finally:
        _finish(child, release)
    t.join(30)
    assert not t.is_alive() and "error" not in out
    same(entry(shift=5.0), cache.read(tmp_path, key()))


def test_gc_leaves_a_key_that_another_process_is_building_and_collects_the_rest(tmp_path):
    idle = key(content="9" * 64, data=D1)
    build(tmp_path, idle)
    child, release = _start_holder(tmp_path, plant_tmp=True)
    d = cache.cache_dir(tmp_path)
    try:
        removed = cache.gc_stale(tmp_path, D2)
        names = set(os.listdir(d))
        assert f"{H}-{D1}-1h.lock" in names and f"{H}-{D1}-1h.tmp-1-ab" in names
        assert not [r for r in removed if r.startswith(H)]
        assert idle.filename in removed and idle.filename not in names
    finally:
        _finish(child, release)
    assert path_of(tmp_path).exists()
    same(proc.small_entry(), cache.read(tmp_path, key()))
    assert path_of(tmp_path).name in cache.gc_stale(tmp_path, D2)
    assert not [n for n in os.listdir(d) if f"-{D1}-" in n]


def test_a_waiter_whose_lock_file_was_collected_takes_the_lock_on_the_current_file(tmp_path):
    build(tmp_path)
    lock_path = cache.cache_dir(tmp_path) / f"{key().stem}.lock"
    inside = threading.Event()
    seen = {}

    def waiter():
        with _open_cache_dir(tmp_path) as dir_fd, cache._key_lock(dir_fd, key().stem):
            seen["path_exists"] = lock_path.exists()
            inside.set()

    with _open_cache_dir(tmp_path) as dir_fd:
        with cache._key_lock(dir_fd, key().stem):
            t = threading.Thread(target=waiter)
            t.start()
            time.sleep(0.4)
            assert not inside.is_set()
            lock_path.unlink()
    t.join(30)
    assert inside.is_set() and seen["path_exists"] is True


# --- the agent's file tools never reach the cache ----------------------------------------------

def test_staging_file_tools_cannot_read_the_cache_even_through_symlinks(tmp_path):
    from agentic_fx.tools.improve_staging_tools import build_improve_staging_tooldefs

    data = tmp_path / "data"
    build(data)
    secret = path_of(data)
    staging, source = tmp_path / "staging", tmp_path / "source"
    staging.mkdir()
    source.mkdir()
    (staging / "evil").symlink_to(cache.cache_dir(data), target_is_directory=True)
    (staging / "evil2").mkdir()
    (staging / "evil2" / "plugin.py").symlink_to(secret)
    (source / "evil3").symlink_to(cache.cache_dir(data), target_is_directory=True)
    tools = {t.name: t for t in build_improve_staging_tooldefs(staging_dir=staging,
                                                               source_snapshot_dir=source)}
    results = [
        tools["read_staging_file"].func(name="evil", rel="plugin.py"),
        tools["read_staging_file"].func(name="evil2", rel="plugin.py"),
        tools["read_staging_file"].func(name="../data", rel="plugin.py"),
        tools["read_staging_file"].func(name="evil2", rel=f"../../data/indicator_fixture_cache/{key().filename}"),
        tools["read_plugin_source"].func(name="evil3"),
        tools["read_example_plugin"].func(name="evil3"),
        tools["list_staging"].func(),
    ]
    blob = json.dumps(results, default=str)
    assert all("content" not in r for r in results[:4])
    assert results[-1] == {"candidates": [{"name": "evil2", "files": []}]}
    assert key().filename not in blob and "proportional" not in blob and "schema" not in blob


def _probe_tests_skip_reason(monkeypatch, host, inherited):
    import importlib.util

    from agentic_fx.plugin import sandbox
    from . import test_indicator_probe as probe_tests

    spec = importlib.util.spec_from_file_location("probe_tests_copy", probe_tests.__file__)
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as patch:
        patch.setattr(sandbox, "host_preflight", lambda: (host, 3))
        patch.setattr(sandbox, "inherited_filter_preflight", lambda: inherited)
        spec.loader.exec_module(module)
    return module._reason


def test_the_real_sandbox_tests_skip_on_the_host_preflight_and_on_an_inherited_filter(monkeypatch):
    assert _probe_tests_skip_reason(monkeypatch, "arch_unsupported", None) == "arch_unsupported"
    assert _probe_tests_skip_reason(monkeypatch, None, "inherited_seccomp_filter") == "inherited_seccomp_filter"
    assert _probe_tests_skip_reason(monkeypatch, None, None) is None
