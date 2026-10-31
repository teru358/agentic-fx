"""Parent-only cache of indicator outputs on the canonical fixture.

Files live under a 0700 directory as 0600 regular files (one inode, never a
symlink), are written through a temp file and an atomic rename, and are read back
only after the schema and the key have been verified.  The directory is under
``data/`` and is never added to the sandbox allowlist.
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import re
import stat
import time
import zipfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pandas as pd

SCHEMA = 1
CACHE_DIR_NAME = "indicator_fixture_cache"
MAX_FILE_BYTES = 64 * 1024 * 1024
_SUFFIX = ".npz"
_LOCK_SUFFIX = ".lock"
_TMP_MARK = ".tmp-"
DEFAULT_LOCK_TIMEOUT_SEC = 120.0
STALE_TMP_AGE_SEC = 3600.0
_LOCK_POLL_SEC = 0.05
# A sha256 hex digest for each hash part and up to 16 characters of timeframe keep
# every file name (temp name included) well under the 255-byte limit.
_HASH_RE = re.compile(r"[0-9A-Za-z]{1,64}")
_TF_RE = re.compile(r"[0-9A-Za-z_]{1,16}")
_NAME_RE = re.compile(r"(?P<content>[0-9A-Za-z]{1,64})-(?P<data>[0-9A-Za-z]{1,64})-(?P<tf>[0-9A-Za-z_]{1,16})")


class IndicatorCacheError(Exception):
    """The cache directory itself is unsafe or a value cannot be stored."""


class IndicatorCacheLockTimeout(IndicatorCacheError):
    """The key lock stayed held by another process past the limit (environmental,
    worth retrying later)."""


@dataclass(frozen=True)
class CacheKey:
    content_hash: str
    fixture_data_hash: str
    timeframe: str

    def __post_init__(self) -> None:
        for value, pattern in ((self.content_hash, _HASH_RE), (self.fixture_data_hash, _HASH_RE),
                               (self.timeframe, _TF_RE)):
            if not isinstance(value, str) or not pattern.fullmatch(value):
                raise ValueError("cache key part has unexpected characters")

    @property
    def stem(self) -> str:
        return f"{self.content_hash}-{self.fixture_data_hash}-{self.timeframe}"

    @property
    def filename(self) -> str:
        return self.stem + _SUFFIX


@dataclass
class CacheEntry:
    """Series are indexed by one shared index; scalars are floats (NaN allowed)."""

    outputs: dict[str, "pd.Series | float"]
    classifications: dict[str, tuple[str, str]] = field(default_factory=dict)


def cache_dir(data_dir: Path) -> Path:
    return Path(data_dir) / CACHE_DIR_NAME


@contextmanager
def _open_dir(directory: Path) -> Iterator[int]:
    """Create the directory if absent, then open it without following links and
    tighten it to 0700 when it belongs to this user."""
    directory = Path(directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise IndicatorCacheError(f"cache directory is not a plain directory: {exc}") from exc
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid():
            raise IndicatorCacheError("cache directory is owned by another user")
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
        yield fd
    finally:
        os.close(fd)


def _acquire_lock(dir_fd: int, stem: str, *, timeout_sec: float | None) -> int | None:
    """Return the fd holding the key lock.  With ``timeout_sec`` None a held lock
    gives None at once; otherwise the lock is retried until the limit and
    ``IndicatorCacheLockTimeout`` is raised.  The lock file may be unlinked by a
    collector while this waits, so the path is re-checked after the lock is won and
    the open is retried when it no longer names the locked inode."""
    name = stem + _LOCK_SUFFIX
    deadline = None if timeout_sec is None else time.monotonic() + timeout_sec
    while True:
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=dir_fd)
        locked = False
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
                raise IndicatorCacheError("cache lock file is not a private regular file")
            if stat.S_IMODE(st.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                pass
            if locked:
                try:
                    current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                if current is not None and (current.st_ino, current.st_dev) == (st.st_ino, st.st_dev):
                    return fd
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        if locked:
            continue
        if deadline is None:
            return None
        if time.monotonic() >= deadline:
            raise IndicatorCacheLockTimeout(f"cache key lock is still held after {timeout_sec} s")
        time.sleep(_LOCK_POLL_SEC)


@contextmanager
def _key_lock(dir_fd: int, stem: str,
              lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC) -> Iterator[None]:
    fd = _acquire_lock(dir_fd, stem, timeout_sec=lock_timeout_sec)
    assert fd is not None
    try:
        yield
    finally:
        os.close(fd)


def _encode(key: CacheKey, entry: CacheEntry) -> bytes:
    index: pd.DatetimeIndex | None = None
    columns: list[dict[str, Any]] = []
    arrays: dict[str, np.ndarray] = {}
    scalars: list[float] = []
    for name, value in entry.outputs.items():
        price, volume = entry.classifications.get(name, ("", ""))
        if isinstance(value, pd.Series):
            if not isinstance(value.index, pd.DatetimeIndex):
                raise IndicatorCacheError("series index must be a DatetimeIndex")
            if value.index.tz is None:
                raise IndicatorCacheError("series index must be timezone-aware")
            if index is None:
                index = value.index
            elif not index.equals(value.index):
                raise IndicatorCacheError("series outputs must share one index")
            arrays[f"s{len(columns)}"] = value.to_numpy(dtype="<f8")
            columns.append({"name": name, "kind": "series", "price": price, "volume": volume,
                            "array": f"s{len(columns)}"})
        else:
            columns.append({"name": name, "kind": "scalar", "price": price, "volume": volume,
                            "slot": len(scalars)})
            scalars.append(float(value))
    meta = {"schema": SCHEMA, "content_hash": key.content_hash,
            "fixture_data_hash": key.fixture_data_hash, "timeframe": key.timeframe,
            "columns": columns, "unit": index.unit if index is not None else "ns"}
    buf = io.BytesIO()
    np.savez(buf, meta=np.array(json.dumps(meta)),
             index=(index.tz_convert("UTC").as_unit("ns").asi8.astype("<i8")
                    if index is not None else np.empty(0, dtype="<i8")),
             scalars=np.asarray(scalars, dtype="<f8"), **arrays)
    return buf.getvalue()


_KINDS = ("series", "scalar")


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("cache entry field is not a string")
    return value


def _decode(key: CacheKey, raw: bytes) -> CacheEntry:
    with np.load(io.BytesIO(raw), allow_pickle=False) as data:
        meta = json.loads(str(data["meta"]))
        if not isinstance(meta, dict):
            raise ValueError("cache entry meta is not an object")
        if (meta.get("schema") != SCHEMA or meta.get("content_hash") != key.content_hash
                or meta.get("fixture_data_hash") != key.fixture_data_hash
                or meta.get("timeframe") != key.timeframe):
            raise ValueError("cache entry does not match its key")
        unit = meta.get("unit")
        if unit not in ("s", "ms", "us", "ns"):
            raise ValueError("unknown index unit")
        columns = meta.get("columns")
        if not isinstance(columns, list):
            raise ValueError("cache entry columns are not a list")
        index = pd.DatetimeIndex(pd.to_datetime(data["index"], unit="ns", utc=True)).as_unit(unit)
        scalars = data["scalars"]
        outputs: dict[str, Any] = {}
        classes: dict[str, tuple[str, str]] = {}
        for col in columns:
            if not isinstance(col, dict):
                raise ValueError("cache entry column is not an object")
            name = _text(col.get("name"))
            if name in outputs:
                raise ValueError("duplicate column name")
            kind = col.get("kind")
            if kind not in _KINDS:
                raise ValueError("unknown column kind")
            price, volume = _text(col.get("price")), _text(col.get("volume"))
            if kind == "series":
                array = _text(col.get("array"))
                if array not in data.files:
                    raise ValueError("column array is missing")
                values = data[array]
                if values.ndim != 1 or len(values) != len(index):
                    raise ValueError("series length differs from the index")
                outputs[name] = pd.Series(values, index=index, dtype="float64")
            else:
                slot = col.get("slot")
                if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < len(scalars):
                    raise ValueError("scalar slot is out of range")
                outputs[name] = float(scalars[slot])
            if price or volume:
                classes[name] = (price, volume)
    return CacheEntry(outputs, classes)


def _read(dir_fd: int, key: CacheKey) -> CacheEntry | None:
    """Return the verified entry, or None when absent or rejected."""
    try:
        fd = os.open(key.filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=dir_fd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid()
                or stat.S_IMODE(st.st_mode) != 0o600 or st.st_size > MAX_FILE_BYTES):
            return None
        with os.fdopen(os.dup(fd), "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_FILE_BYTES:
        return None
    try:
        return _decode(key, raw)
    except (ValueError, KeyError, OSError, EOFError, zipfile.BadZipFile, TypeError, IndexError):
        return None


def _write(dir_fd: int, key: CacheKey, entry: CacheEntry) -> bytes:
    """Store the entry and return the bytes that were stored."""
    payload = _encode(key, entry)
    if len(payload) > MAX_FILE_BYTES:
        raise IndicatorCacheError("encoded cache entry exceeds the size limit")
    tmp = f"{key.stem}{_TMP_MARK}{os.getpid()}-{os.urandom(4).hex()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=dir_fd)
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, key.filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    os.fsync(dir_fd)
    return payload


def read(data_dir: Path, key: CacheKey) -> CacheEntry | None:
    with _open_dir(cache_dir(data_dir)) as dir_fd:
        return _read(dir_fd, key)


def write(data_dir: Path, key: CacheKey, entry: CacheEntry, *,
          lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC) -> None:
    with _open_dir(cache_dir(data_dir)) as dir_fd, _key_lock(dir_fd, key.stem, lock_timeout_sec):
        _write(dir_fd, key, entry)


def get_or_build(data_dir: Path, key: CacheKey, builder: Callable[[], CacheEntry], *,
                 lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC) -> tuple[CacheEntry, bool]:
    """Return ``(entry, built)``.  The per-key lock covers check, build and write so
    two missions never build the same entry twice; a rejected file is rebuilt.  A
    freshly built entry is returned in the form a later hit would have (the stored
    bytes decoded), so both paths hand out the same thing."""
    with _open_dir(cache_dir(data_dir)) as dir_fd, _key_lock(dir_fd, key.stem, lock_timeout_sec):
        hit = _read(dir_fd, key)
        if hit is not None:
            return hit, False
        payload = _write(dir_fd, key, builder())
        return _decode(key, payload), True


class GcResult(list):
    """The removed file names; ``skipped`` lists stems that could not be collected."""

    def __init__(self, removed: list[str], skipped: list[str]) -> None:
        super().__init__(removed)
        self.skipped = skipped


def _split_name(name: str) -> tuple[str, str] | None:
    """(stem, kind) with kind in entry / lock / tmp, or None for a foreign file."""
    kind = "tmp"
    stem = name
    if _TMP_MARK in name:
        stem = name.split(_TMP_MARK, 1)[0]
    elif name.endswith(_SUFFIX):
        stem, kind = name[: -len(_SUFFIX)], "entry"
    elif name.endswith(_LOCK_SUFFIX):
        stem, kind = name[: -len(_LOCK_SUFFIX)], "lock"
    else:
        return None
    return (stem, kind) if _NAME_RE.fullmatch(stem) else None


def _unlink(dir_fd: int, name: str, removed: list[str]) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    removed.append(name)


def _collect_stem(dir_fd: int, stem: str, names: list[str], stale_generation: bool,
                  removed: list[str]) -> None:
    fd = _acquire_lock(dir_fd, stem, timeout_sec=None)
    if fd is None:
        return          # in use; left for the next collection
    try:
        lock_name = stem + _LOCK_SUFFIX
        if stale_generation:
            for name in sorted(set(names) | {lock_name}):
                _unlink(dir_fd, name, removed)
            return
        now = time.time()
        for name in names:
            if _TMP_MARK not in name:
                continue
            try:
                age = now - os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mtime
            except FileNotFoundError:
                continue
            if age > STALE_TMP_AGE_SEC:
                _unlink(dir_fd, name, removed)
        prefix = stem + _TMP_MARK
        if any(n.startswith(prefix) for n in os.listdir(dir_fd)):
            return
        try:
            os.stat(stem + _SUFFIX, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            _unlink(dir_fd, lock_name, removed)     # an orphan lock: no entry and no temp file
    finally:
        os.close(fd)


def gc_stale(data_dir: Path, keep_fixture_data_hash: str) -> GcResult:
    """Remove entries (and their lock / temp files) of other fixture generations; in
    the current generation remove temp files older than an hour and locks that guard
    nothing.

    A key whose lock is held is in use and is left for the next collection; a key
    that cannot be collected is counted in ``skipped`` and the rest carry on."""
    stems: dict[str, list[str]] = {}
    removed: list[str] = []
    skipped: list[str] = []
    with _open_dir(cache_dir(data_dir)) as dir_fd:
        for name in os.listdir(dir_fd):
            parsed = _split_name(name)
            if parsed is not None:
                stems.setdefault(parsed[0], []).append(name)
        for stem, names in sorted(stems.items()):
            match = _NAME_RE.fullmatch(stem)
            stale_generation = match["data"] != keep_fixture_data_hash
            try:
                _collect_stem(dir_fd, stem, names, stale_generation, removed)
            except (OSError, IndicatorCacheError):
                skipped.append(stem)
    return GcResult(sorted(removed), sorted(skipped))
