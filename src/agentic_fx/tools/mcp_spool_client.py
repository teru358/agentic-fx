"""MCP spool の client 側 (shim が使う publish と応答待ち)。

shim は CLI の子として seccomp と nested Landlock の下で動くので、この
module は標準ライブラリの軽いもの (os、ctypes、time、json) だけを import
する。socket を作らない (部分木では AF_UNIX の socket が EACCES になる)。
POSIX timer も使わない (待ちは sleep の polling)。

名前の流れ: shim が `tmp-<id>` を書いて `req-<id>` に publish し、worker
の dispatcher が `claim-<id>` に claim、応答を `rtmp-<id>` に書いて
`resp-<id>` に publish する。rename は全部同一 directory 内の
`RENAME_NOREPLACE` (cross-directory rename は REFER 無しの Landlock 下で
EXDEV になる)。
"""
from __future__ import annotations

import ctypes
import errno
import json
import os
import time
from pathlib import Path

TMP_PREFIX = "tmp-"
REQ_PREFIX = "req-"
CLAIM_PREFIX = "claim-"
RTMP_PREFIX = "rtmp-"
RESP_PREFIX = "resp-"

# 128 bit の乱数を小文字 hex で表した長さ。
ID_HEX_LEN = 32

_SYS_RENAMEAT2 = 316
_SYS_GETRANDOM = 318
RENAME_NOREPLACE = 1

# 応答待ちの polling 間隔。最初は短く、待ちが長い tool 実行 (backtest 等)
# では上限で落ち着かせる。
_POLL_INITIAL_SEC = 0.001
_POLL_MAX_SEC = 0.02

_libc: ctypes.CDLL | None = None


def get_libc() -> ctypes.CDLL:
    global _libc
    if _libc is None:
        lib = ctypes.CDLL(None, use_errno=True)
        lib.syscall.restype = ctypes.c_long
        _libc = lib
    return _libc


def renameat2_noreplace(dirfd: int, old: str, new: str) -> None:
    """同じ directory fd の中で `old` を `new` に NOREPLACE で rename する。
    失敗は errno つきの `OSError` (EEXIST、ENOENT、EXDEV 等) で返す。"""
    libc = get_libc()
    rc = libc.syscall(
        ctypes.c_long(_SYS_RENAMEAT2), ctypes.c_int(dirfd),
        ctypes.c_char_p(os.fsencode(old)), ctypes.c_int(dirfd),
        ctypes.c_char_p(os.fsencode(new)), ctypes.c_uint(RENAME_NOREPLACE))
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), old, None, new)


def new_request_id() -> str:
    """128 bit の乱数 id。`getrandom(2)` を直接呼ぶ — この Python の
    `os.urandom` は `/dev/urandom` を開くので、Landlock の下で
    `/dev/urandom` が読めない部分木では失敗する。"""
    size = ID_HEX_LEN // 2
    buf = ctypes.create_string_buffer(size)
    got = 0
    libc = get_libc()
    while got < size:
        n = libc.syscall(ctypes.c_long(_SYS_GETRANDOM),
                         ctypes.byref(buf, got), ctypes.c_size_t(size - got),
                         ctypes.c_uint(0))
        if n < 0:
            err = ctypes.get_errno()
            if err == errno.EINTR:
                continue
            raise OSError(err, os.strerror(err))
        got += n
    return buf.raw.hex()


def is_lower_hex(value: str) -> bool:
    return all(c in "0123456789abcdef" for c in value)


def is_request_id(value: str) -> bool:
    return len(value) == ID_HEX_LEN and is_lower_hex(value)


def open_spool_dir(spool_dir: Path | str) -> int:
    return os.open(os.fspath(spool_dir),
                   os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def publish_request(dirfd: int, payload: bytes) -> str:
    """`payload` を `tmp-<id>` に全量書いて fsync し、`req-<id>` に
    NOREPLACE で publish する。返り値は `<id>`。"""
    rid = new_request_id()
    tmp_name = TMP_PREFIX + rid
    fd = os.open(tmp_name,
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                 | os.O_CLOEXEC, 0o600, dir_fd=dirfd)
    try:
        try:
            write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        renameat2_noreplace(dirfd, tmp_name, REQ_PREFIX + rid)
    except BaseException:
        try:
            os.unlink(tmp_name, dir_fd=dirfd)
        except OSError:
            pass
        raise
    return rid


def wait_response(dirfd: int, rid: str,
                  timeout_sec: float | None = None) -> bytes:
    """`resp-<id>` が publish されるまで待って中身を返す。応答は rename で
    一度に現れるので、見えた時点で全量が書かれている。`timeout_sec` を
    過ぎたら `TimeoutError`。"""
    name = RESP_PREFIX + rid
    deadline = None if timeout_sec is None else time.monotonic() + timeout_sec
    delay = _POLL_INITIAL_SEC
    while True:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                         | os.O_CLOEXEC, dir_fd=dirfd)
        except FileNotFoundError:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"no MCP response for {rid}") from None
            time.sleep(delay)
            delay = min(delay * 2, _POLL_MAX_SEC)
            continue
        try:
            chunks = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)


def call(spool_dir: Path | str, request: dict,
         timeout_sec: float | None = None) -> dict:
    """要求 1 件を publish して応答を dict で返す (shim と同じ手順)。"""
    dirfd = open_spool_dir(spool_dir)
    try:
        rid = publish_request(dirfd, (json.dumps(request) + "\n").encode())
        return json.loads(wait_response(dirfd, rid, timeout_sec))
    finally:
        os.close(dirfd)
