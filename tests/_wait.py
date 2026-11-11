"""fd 番号に上限の無い待ち。

`select.select` は fd 番号が 1024 以上だと ValueError になる。1 プロセスで
多数の test を回すと閉じ忘れの fd で番号が伸びるため、test からの待ちは
ここの poll ベースの関数を使う。
"""
from __future__ import annotations

import select
from typing import Any

_READ = select.POLLIN | select.POLLHUP | select.POLLERR
_WRITE = select.POLLOUT | select.POLLERR | select.POLLHUP


def _fd(obj: Any) -> int:
    return obj if isinstance(obj, int) else obj.fileno()


def _wait(obj: Any, mask: int, timeout: float | None) -> bool:
    p = select.poll()
    p.register(_fd(obj), mask)
    ms = None if timeout is None else max(0, int(timeout * 1000 + 0.999))
    return bool(p.poll(ms))


def wait_readable(fileobj: Any, timeout: float | None) -> bool:
    """読める (EOF・エラーを含む) なら True。timeout 秒で False。
    fileobj は fd 番号か fileno() を持つ object。buffered file の
    userspace buffer は見ない (fd の状態だけを見る)。"""
    return _wait(fileobj, _READ, timeout)


def wait_writable(fileobj: Any, timeout: float | None) -> bool:
    """書ける (エラーを含む) なら True。timeout 秒で False。"""
    return _wait(fileobj, _WRITE, timeout)
