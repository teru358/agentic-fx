"""Landlock (Linux kernel 5.13+, ABI v1) による FS 自己制限 — ctypes による
syscall 直叩き (プラン8, 設計書 §4.6)。improve worker profile (Task 18) が
`data/` への到達不能を OS レベルで強制するために使う。

**x86_64 Linux 専用** — landlock_* syscall 番号はアーキテクチャ依存であり、
本モジュールは x86_64 の番号のみをハードコードする。他アーキテクチャでは
`is_available()` が無条件 False を返す (fail closed — improve worker の
起動を拒否する)。

実機検証済み (writing-plans, x86_64 / kernel 7.0.0-28-generic):
`landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)` が
ABI バージョン 8 を返す / `landlock_add_rule` で O_PATH ディレクトリ fd に
対する読み取り専用ルールを追加できる / `prctl(PR_SET_NO_NEW_PRIVS, 1)` →
`landlock_restrict_self` の順で自己制限後、許可ディレクトリ外への
アクセスが `PermissionError` になることを実測確認済み。
"""
from __future__ import annotations

import ctypes
import os
import platform
from pathlib import Path

# x86_64 の landlock syscall 番号 (Linux 5.13+)。
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446

_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

_ACCESS_FS_EXECUTE = 1 << 0
_ACCESS_FS_WRITE_FILE = 1 << 1
_ACCESS_FS_READ_FILE = 1 << 2
_ACCESS_FS_READ_DIR = 1 << 3
_ACCESS_FS_REMOVE_DIR = 1 << 4
_ACCESS_FS_REMOVE_FILE = 1 << 5
_ACCESS_FS_MAKE_CHAR = 1 << 6
_ACCESS_FS_MAKE_DIR = 1 << 7
_ACCESS_FS_MAKE_REG = 1 << 8
_ACCESS_FS_MAKE_SOCK = 1 << 9
_ACCESS_FS_MAKE_FIFO = 1 << 10
_ACCESS_FS_MAKE_BLOCK = 1 << 11
_ACCESS_FS_MAKE_SYM = 1 << 12

# ABI v1 の全 handled_access_fs (ruleset attr に必須 — 「この ruleset が
# 判定対象とするアクセス種別」の宣言。0x1FFF)。
_ABI_V1_HANDLED_ACCESS_FS = (
    _ACCESS_FS_EXECUTE | _ACCESS_FS_WRITE_FILE | _ACCESS_FS_READ_FILE |
    _ACCESS_FS_READ_DIR | _ACCESS_FS_REMOVE_DIR | _ACCESS_FS_REMOVE_FILE |
    _ACCESS_FS_MAKE_CHAR | _ACCESS_FS_MAKE_DIR | _ACCESS_FS_MAKE_REG |
    _ACCESS_FS_MAKE_SOCK | _ACCESS_FS_MAKE_FIFO | _ACCESS_FS_MAKE_BLOCK |
    _ACCESS_FS_MAKE_SYM
)

_READ_ONLY_ACCESS = _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR
_READ_WRITE_ACCESS = (
    _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR | _ACCESS_FS_WRITE_FILE |
    _ACCESS_FS_MAKE_REG | _ACCESS_FS_REMOVE_FILE)

_PR_SET_NO_NEW_PRIVS = 38


class LandlockUnavailable(Exception):
    """カーネル非対応・非対応アーキテクチャ・syscall 失敗の単一表現。"""


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64),
                ("parent_fd", ctypes.c_int32)]


def is_available() -> bool:
    """Landlock ABI バージョンを問い合わせる。x86_64 以外は無条件 False。"""
    if platform.machine() != "x86_64":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    version = libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), None,
        ctypes.c_size_t(0), ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION))
    return version >= 1


def restrict_to(*, read_only_paths: list[Path],
                read_write_paths: list[Path]) -> None:
    """呼び出しプロセスを Landlock で FS allowlist に制限する
    (**不可逆 — プロセス生涯にわたって有効**、以後の子プロセスにも継承
    される)。利用不能なら `LandlockUnavailable`。
    """
    if not is_available():
        raise LandlockUnavailable(
            "Landlock is not available (non-x86_64, or kernel ABI < 1)")

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    ruleset_attr = _RulesetAttr(handled_access_fs=_ABI_V1_HANDLED_ACCESS_FS)
    ruleset_fd = libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), ctypes.byref(ruleset_attr),
        ctypes.c_size_t(ctypes.sizeof(ruleset_attr)), ctypes.c_uint32(0))
    if ruleset_fd < 0:
        errno = ctypes.get_errno()
        raise LandlockUnavailable(
            f"landlock_create_ruleset failed: {os.strerror(errno)}")

    try:
        for path, access in (
                *((p, _READ_ONLY_ACCESS) for p in read_only_paths),
                *((p, _READ_WRITE_ACCESS) for p in read_write_paths)):
            parent_fd = os.open(str(path), os.O_PATH | os.O_DIRECTORY)
            try:
                rule_attr = _PathBeneathAttr(allowed_access=access,
                                             parent_fd=parent_fd)
                rc = libc.syscall(
                    ctypes.c_long(_SYS_LANDLOCK_ADD_RULE), ctypes.c_int(ruleset_fd),
                    ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
                    ctypes.byref(rule_attr), ctypes.c_uint32(0))
                if rc != 0:
                    errno = ctypes.get_errno()
                    raise LandlockUnavailable(
                        f"landlock_add_rule failed for {path}: "
                        f"{os.strerror(errno)}")
            finally:
                os.close(parent_fd)

        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            errno = ctypes.get_errno()
            raise LandlockUnavailable(
                f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(errno)}")

        rc = libc.syscall(ctypes.c_long(_SYS_LANDLOCK_RESTRICT_SELF),
                          ctypes.c_int(ruleset_fd), ctypes.c_uint32(0))
        if rc != 0:
            errno = ctypes.get_errno()
            raise LandlockUnavailable(
                f"landlock_restrict_self failed: {os.strerror(errno)}")
    finally:
        os.close(ruleset_fd)
