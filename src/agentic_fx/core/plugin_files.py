"""plugin の `plugin.py` と `config.yaml` を上限つきで読む共有部品。

親 (`loader.content_hash`) と worker の source-only loader が同じ上限で読むための出所。
上限を超えるファイルは全体を読まずに `PluginFileTooLarge` にする (巨大ファイルを
置かれても、hash の計算でメモリと時間がファイル量に比例して増えないように)。

このモジュールは import 時に numpy / pandas を読まない (隔離前の worker からも使える)。
"""
from __future__ import annotations

import hashlib
import os
import stat as _stat
from pathlib import Path

#: `plugin.py` / `config.yaml` の読取上限 (bytes)
MAX_PLUGIN_FILE_BYTES = 1_048_576

#: content_hash の対象になる 2 ファイル
PLUGIN_FILE_NAMES = ("plugin.py", "config.yaml")

_READ_CHUNK = 1 << 16


def write_all(fd: int, data: bytes) -> None:
    """`data` を全部書き切る (部分書き込みを繰り返す)。親子の protocol・診断で共用する。"""
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def read_fd_up_to(fd: int, limit: int) -> bytes | None:
    """開いている `fd` を最大 `limit + 1` bytes だけ読む。上限を超えたら `None`
    (呼び出し元が各自の `too_large` 失敗を起こす)。上限は読んだ bytes 数で判定する
    (fstat の size は読取中に伸び得る)。`os.read` の失敗は呼び出し元へ伝播する。"""
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        b = os.read(fd, min(_READ_CHUNK, limit + 1 - total))
        if not b:
            break
        chunks.append(b)
        total += len(b)
    if total > limit:
        return None
    return b"".join(chunks)


def content_hash_bytes(plugin_py: bytes, config_yaml: bytes) -> str:
    """content_hash の定義 (不変)。式はここ 1 箇所だけに置き、版ストアはここから import する。"""
    return hashlib.sha256(
        b"plugin.py\0" + plugin_py + b"\0config.yaml\0" + config_yaml
    ).hexdigest()


class PluginFileTooLarge(ValueError):
    """読取上限を超えた。`reason` は固定の `file_too_large`。"""

    reason = "file_too_large"

    def __init__(self, path: str, limit: int) -> None:
        super().__init__(f"{path} exceeds {limit} bytes")
        self.path = path
        self.limit = limit


def read_file_bounded(path: str | os.PathLike, *, limit: int = MAX_PLUGIN_FILE_BYTES) -> bytes:
    """`path` を最大 `limit + 1` bytes だけ読む。

    上限は読んだ bytes 数で判定する (fstat の size は読取中に伸び得る)。超過は
    `PluginFileTooLarge`、通常ファイルでないもの (FIFO 等) は `OSError`。O_NONBLOCK は
    FIFO に差し替えられた名前で読取が止まらないため。
    """
    fd = os.open(os.fspath(path), os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{os.fspath(path)} is not a regular file")
        data = read_fd_up_to(fd, limit)
        if data is None:
            raise PluginFileTooLarge(os.fspath(path), limit)
        return data
    finally:
        os.close(fd)


def content_hash_of_dir(plugin_dir: str | os.PathLike, *,
                        limit: int = MAX_PLUGIN_FILE_BYTES) -> str:
    """`plugin_dir` の `plugin.py` と `config.yaml` を上限つきで読んだ content_hash。"""
    d = Path(plugin_dir)
    return content_hash_bytes(read_file_bounded(d / "plugin.py", limit=limit),
                              read_file_bounded(d / "config.yaml", limit=limit))


__all__ = [
    "MAX_PLUGIN_FILE_BYTES",
    "PLUGIN_FILE_NAMES",
    "PluginFileTooLarge",
    "content_hash_bytes",
    "content_hash_of_dir",
    "read_fd_up_to",
    "read_file_bounded",
    "write_all",
]
