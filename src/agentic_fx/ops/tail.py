"""巨大な log / activity でも読む量を上限で止める後方 tail。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MAX_SCAN_BYTES = 4 * 1024 * 1024
MAX_LINE_CHARS = 2000
_CHUNK = 64 * 1024

ACTIVITY_CATEGORIES = frozenset({"NEWS", "TECH", "AGGREGATE", "TRADE", "IMPROVE",
                                 "APPROVAL", "SYSTEM"})


@dataclass(frozen=True, slots=True)
class TailResult:
    lines: list[str]
    truncated: bool
    bytes_read: int


def tail_lines(path: Path, n: int, *, keep: Callable[[str], bool] | None = None,
               max_scan: int = MAX_SCAN_BYTES, max_line: int = MAX_LINE_CHARS) -> TailResult:
    """末尾から最大 ``max_scan`` byte だけ読み、条件に合う最後の ``n`` 行を返す。

    走査が上限に達して先頭まで読めなかった場合は ``truncated`` を立てる。
    1 行は ``max_line`` 文字で切る (巨大行で応答が膨らまないように)。
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return TailResult([], False, 0)
    try:
        size = os.fstat(fd).st_size
        position, read_total = size, 0
        buffer = b""
        found: list[str] = []
        lines_seen = 0
        while position > 0 and read_total < max_scan and len(found) < n:
            step = min(_CHUNK, position, max_scan - read_total)
            position -= step
            chunk = os.pread(fd, step, position)
            read_total += len(chunk)
            buffer = chunk + buffer
            parts = buffer.split(b"\n")
            # 先頭の断片は前の chunk と繋がる可能性があるので次回へ持ち越す。
            buffer = parts[0]
            for raw in reversed(parts[1:]):
                if not raw:
                    continue
                lines_seen += 1
                line = raw.decode("utf-8", errors="replace")[:max_line]
                if keep is None or keep(line):
                    found.append(line)
                    if len(found) >= n:
                        break
        if position > 0 and buffer and lines_seen == 0 and read_total >= max_scan:
            # 上限内に改行が無い巨大行。行頭は読めないので、読めた範囲を切って返す。
            found.append(buffer.decode("utf-8", errors="replace")[:max_line])
            buffer = b""
        if position == 0 and buffer and len(found) < n:
            line = buffer.decode("utf-8", errors="replace")[:max_line]
            if keep is None or keep(line):
                found.append(line)
            buffer = b""
        truncated = position > 0 and len(found) < n
        return TailResult(list(reversed(found)), truncated, read_total)
    finally:
        os.close(fd)


def activity_filter(category: str | None) -> Callable[[str], bool] | None:
    if category is None:
        return None
    if category not in ACTIVITY_CATEGORIES:
        raise ValueError(category)
    return lambda line: line.split("\t", 2)[1:2] == [category]
