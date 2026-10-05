"""agentic_fx の worker subprocess が共有する資源上限。

plugin worker (`worker_isolation` の隔離段) と gate pytest worker (`plugin.worker`)
が同じ `RLIMIT_NPROC` の値を使うための 1 箇所。片方だけ書き換えると両者の上限が
食い違うため、定数はここだけに置く。import 時に numpy / pandas を読まない。
"""
from __future__ import annotations

#: RLIMIT_NPROC の上限。per-uid の累積なので、thread を十分に取れる寛大な値にする。
NPROC_CAP = 512

__all__ = ["NPROC_CAP"]
