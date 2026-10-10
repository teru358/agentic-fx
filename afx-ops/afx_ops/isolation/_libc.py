"""libc の ctypes handle (prctl と生の syscall) を 1 箇所で用意する。

syscall の戻り値は long なので restype を必ず c_long にする (既定の int では 32 bit に
切り詰められ、負の errno の判定や fd の値を取り違える)。設定を 1 箇所に保つため、
隔離の各段はここからだけ handle を取る。
"""
from __future__ import annotations

import ctypes
import functools


@functools.cache
def libc() -> ctypes.CDLL:
    handle = ctypes.CDLL(None, use_errno=True)
    handle.syscall.restype = ctypes.c_long
    return handle
