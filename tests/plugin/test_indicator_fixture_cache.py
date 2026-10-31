"""Parent-only indicator fixture cache: discipline, verification and sandbox invisibility."""
from __future__ import annotations

import json
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


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "a b", "a.npz"])
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
