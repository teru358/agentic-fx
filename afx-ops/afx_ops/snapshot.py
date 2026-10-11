"""state dir の fd だけで JSON snapshot を交換する。"""
from __future__ import annotations

import json
import os
import secrets
import stat
from typing import Any

REASON_SNAPSHOT_CORRUPT = "snapshot_corrupt"
_CLEANED_TEMP_DIRS: set[tuple[int, int, str]] = set()


class SnapshotError(Exception):
    """snapshot が正本として読めない。"""

    def __init__(self) -> None:
        super().__init__(REASON_SNAPSHOT_CORRUPT)
        self.reason = REASON_SNAPSHOT_CORRUPT


def _checked_name(name: str) -> str:
    if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\0" in name:
        raise ValueError("invalid snapshot name")
    return name


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError("short snapshot write")
        view = view[written:]


def _remove_stale_temps(dir_fd: int, name: str) -> None:
    prefix = f".{name}.tmp-"
    try:
        names = os.listdir(dir_fd)
        for entry in names:
            if entry.startswith(prefix):
                os.unlink(entry, dir_fd=dir_fd)
    except OSError:
        raise SnapshotError() from None


def _remove_stale_temps_once(dir_fd: int, name: str) -> None:
    try:
        st = os.fstat(dir_fd)
    except OSError:
        raise SnapshotError() from None
    identity = (st.st_dev, st.st_ino, name)
    if identity not in _CLEANED_TEMP_DIRS:
        _remove_stale_temps(dir_fd, name)
        _CLEANED_TEMP_DIRS.add(identity)


def write_snapshot(dir_fd: int, name: str, obj: Any) -> None:
    """単一 process の単一 writer だけが、``obj`` を原子的に置換する。

    複数 writer を許さない契約なので、ここへロックや競合調停を追加してはならない。
    """
    name = _checked_name(name)
    try:
        encoded = json.dumps(obj, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError):
        raise SnapshotError() from None
    _remove_stale_temps_once(dir_fd, name)
    temp = f".{name}.tmp-{secrets.token_hex(16)}"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=dir_fd)
    except OSError:
        raise SnapshotError() from None
    committed = False
    try:
        try:
            _write_all(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
        committed = True
    except OSError:
        raise SnapshotError() from None
    finally:
        if not committed:
            try:
                os.unlink(temp, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def read_snapshot(dir_fd: int, name: str, schema_version: int) -> dict[str, Any] | None:
    """正本 snapshot だけを読み、schema version が一致した object を返す。"""
    name = _checked_name(name)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise SnapshotError() from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SnapshotError()
        chunks = []
        while chunk := os.read(fd, 4096):
            chunks.append(chunk)
    except SnapshotError:
        raise
    except OSError:
        raise SnapshotError() from None
    finally:
        os.close(fd)
    try:
        obj = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SnapshotError() from None
    if (type(obj) is not dict or type(obj.get("version")) is not type(schema_version)
            or obj["version"] != schema_version):
        raise SnapshotError()
    return obj
