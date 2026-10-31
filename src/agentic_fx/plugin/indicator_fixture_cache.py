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
_HASH_RE = re.compile(r"[0-9A-Za-z]{1,128}")
_TF_RE = re.compile(r"[0-9A-Za-z_]{1,16}")
_NAME_RE = re.compile(r"(?P<content>[0-9A-Za-z]{1,128})-(?P<data>[0-9A-Za-z]{1,128})-(?P<tf>[0-9A-Za-z_]{1,16})")


class IndicatorCacheError(Exception):
    """The cache directory itself is unsafe or a value cannot be stored."""


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


@contextmanager
def _key_lock(dir_fd: int, stem: str) -> Iterator[None]:
    fd = os.open(stem + _LOCK_SUFFIX, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid():
            raise IndicatorCacheError("cache lock file is not a private regular file")
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
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


def _decode(key: CacheKey, raw: bytes) -> CacheEntry:
    with np.load(io.BytesIO(raw), allow_pickle=False) as data:
        meta = json.loads(str(data["meta"]))
        if (meta.get("schema") != SCHEMA or meta.get("content_hash") != key.content_hash
                or meta.get("fixture_data_hash") != key.fixture_data_hash
                or meta.get("timeframe") != key.timeframe):
            raise ValueError("cache entry does not match its key")
        unit = meta["unit"]
        if unit not in ("s", "ms", "us", "ns"):
            raise ValueError("unknown index unit")
        index = pd.DatetimeIndex(pd.to_datetime(data["index"], unit="ns", utc=True)).as_unit(unit)
        scalars = data["scalars"]
        outputs: dict[str, Any] = {}
        classes: dict[str, tuple[str, str]] = {}
        for col in meta["columns"]:
            name = str(col["name"])
            if col["kind"] == "series":
                values = data[col["array"]]
                if len(values) != len(index):
                    raise ValueError("series length differs from the index")
                outputs[name] = pd.Series(values, index=index, dtype="float64")
            else:
                outputs[name] = float(scalars[int(col["slot"])])
            if col["price"] or col["volume"]:
                classes[name] = (str(col["price"]), str(col["volume"]))
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


def _write(dir_fd: int, key: CacheKey, entry: CacheEntry) -> None:
    payload = _encode(key, entry)
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


def read(data_dir: Path, key: CacheKey) -> CacheEntry | None:
    with _open_dir(cache_dir(data_dir)) as dir_fd:
        return _read(dir_fd, key)


def write(data_dir: Path, key: CacheKey, entry: CacheEntry) -> None:
    with _open_dir(cache_dir(data_dir)) as dir_fd, _key_lock(dir_fd, key.stem):
        _write(dir_fd, key, entry)


def get_or_build(data_dir: Path, key: CacheKey,
                 builder: Callable[[], CacheEntry]) -> tuple[CacheEntry, bool]:
    """Return ``(entry, built)``.  The per-key lock covers check, build and write so
    two missions never build the same entry twice; a rejected file is rebuilt."""
    with _open_dir(cache_dir(data_dir)) as dir_fd, _key_lock(dir_fd, key.stem):
        hit = _read(dir_fd, key)
        if hit is not None:
            return hit, False
        entry = builder()
        _write(dir_fd, key, entry)
        return entry, True


def gc_stale(data_dir: Path, keep_fixture_data_hash: str) -> list[str]:
    """Remove entries (and their lock / temp files) of other fixture generations."""
    removed: list[str] = []
    with _open_dir(cache_dir(data_dir)) as dir_fd:
        for name in os.listdir(dir_fd):
            stem = name
            for suffix in (_SUFFIX, _LOCK_SUFFIX):
                if stem.endswith(suffix):
                    stem = stem[: -len(suffix)]
                    break
            stem = stem.split(_TMP_MARK, 1)[0]
            match = _NAME_RE.fullmatch(stem)
            if match is None or match["data"] == keep_fixture_data_hash:
                continue
            try:
                os.unlink(name, dir_fd=dir_fd)
            except FileNotFoundError:
                continue
            removed.append(name)
    return sorted(removed)
