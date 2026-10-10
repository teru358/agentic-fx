"""ops process 用の Landlock ruleset (ABI 8 前提)。

FS は ABI 8 までの全 access を handled にし、許可は規則で列挙したものだけにする。
network は TCP bind を全拒否し、TCP connect は規則で許した port だけにする。scope で
signal と abstract UDS を同じ domain の内側に閉じる。restrict_self は呼んだ thread に
だけ掛かるので、呼び出し側が single thread を確かめてから呼ぶ。
"""
from __future__ import annotations

import ctypes
import os
import platform
import stat
from dataclasses import dataclass

from afx_ops.isolation._libc import libc as _libc

ACCESS_FS_EXECUTE = 1 << 0
ACCESS_FS_WRITE_FILE = 1 << 1
ACCESS_FS_READ_FILE = 1 << 2
ACCESS_FS_READ_DIR = 1 << 3
ACCESS_FS_REMOVE_DIR = 1 << 4
ACCESS_FS_REMOVE_FILE = 1 << 5
ACCESS_FS_MAKE_CHAR = 1 << 6
ACCESS_FS_MAKE_DIR = 1 << 7
ACCESS_FS_MAKE_REG = 1 << 8
ACCESS_FS_MAKE_SOCK = 1 << 9
ACCESS_FS_MAKE_FIFO = 1 << 10
ACCESS_FS_MAKE_BLOCK = 1 << 11
ACCESS_FS_MAKE_SYM = 1 << 12
ACCESS_FS_REFER = 1 << 13
ACCESS_FS_TRUNCATE = 1 << 14
ACCESS_FS_IOCTL_DEV = 1 << 15
HANDLED_FS = (1 << 16) - 1

ACCESS_NET_BIND_TCP = 1 << 0
ACCESS_NET_CONNECT_TCP = 1 << 1
HANDLED_NET = ACCESS_NET_BIND_TCP | ACCESS_NET_CONNECT_TCP

SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
SCOPE_SIGNAL = 1 << 1
SCOPED = SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL

# file に付けられる access (dir 専用の access を file の規則に付けると EINVAL)
FILE_ACCESS = (ACCESS_FS_EXECUTE | ACCESS_FS_WRITE_FILE | ACCESS_FS_READ_FILE
               | ACCESS_FS_TRUNCATE | ACCESS_FS_IOCTL_DEV)

READ = ACCESS_FS_READ_FILE | ACCESS_FS_READ_DIR
RUNTIME = ACCESS_FS_EXECUTE | READ
STATE = (READ | ACCESS_FS_WRITE_FILE | ACCESS_FS_MAKE_REG | ACCESS_FS_MAKE_DIR
         | ACCESS_FS_REMOVE_FILE | ACCESS_FS_REMOVE_DIR | ACCESS_FS_TRUNCATE)

REQUIRED_ABI = 8

_SYS_CREATE_RULESET = 444
_SYS_ADD_RULE = 445
_SYS_RESTRICT_SELF = 446
_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_RULE_NET_PORT = 2
_PR_SET_NO_NEW_PRIVS = 38


class LandlockError(Exception):
    """ruleset を組めない・掛けられない。`reason` は固定語。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64),
                ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _NetPortAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


@dataclass(frozen=True)
class PathRule:
    """`path` (symlink は辿った先) 以下に `access` を許す。file なら file の access だけ。

    required が偽なら、path が無いときは規則を足さずに進む (任意の設定 file)。
    fd を渡したときは path を開き直さずその fd の指す dir に規則を付ける (検査した dir と
    規則の対象を同じ inode に束ねる)。fd の所有と close は呼出し側。path は表示用。
    """

    path: str
    access: int
    required: bool = True
    fd: int | None = None


@dataclass(frozen=True)
class LandlockPlan:
    path_rules: tuple[PathRule, ...]
    connect_ports: tuple[int, ...] = ()


def abi_version() -> int:
    """kernel の Landlock ABI。使えなければ 0。"""
    if platform.machine() != "x86_64":
        return 0
    rc = _libc().syscall(ctypes.c_long(_SYS_CREATE_RULESET), None, ctypes.c_size_t(0),
                         ctypes.c_uint32(_CREATE_RULESET_VERSION))
    return int(rc) if rc > 0 else 0


def apply(plan: LandlockPlan) -> None:
    """ruleset を組んで呼んだ thread に掛ける。取り消せない。"""
    abi = abi_version()
    if abi < REQUIRED_ABI:
        raise LandlockError("landlock_abi_too_old", f"abi={abi}")
    libc = _libc()
    attr = _RulesetAttr(HANDLED_FS, HANDLED_NET, SCOPED)
    ruleset = libc.syscall(ctypes.c_long(_SYS_CREATE_RULESET), ctypes.byref(attr),
                           ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))
    if ruleset < 0:
        raise LandlockError("landlock_failed", f"create errno={ctypes.get_errno()}")
    try:
        for rule in plan.path_rules:
            _add_path_rule(libc, int(ruleset), rule)
        for port in plan.connect_ports:
            if not 0 < port < 65536:
                raise LandlockError("landlock_failed", f"port {port}")
            net = _NetPortAttr(ACCESS_NET_CONNECT_TCP, port)
            if libc.syscall(ctypes.c_long(_SYS_ADD_RULE), ctypes.c_int(int(ruleset)),
                            ctypes.c_int(_RULE_NET_PORT), ctypes.byref(net),
                            ctypes.c_uint32(0)) != 0:
                raise LandlockError("landlock_failed",
                                    f"net rule {port} errno={ctypes.get_errno()}")
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise LandlockError("landlock_failed", f"no_new_privs errno={ctypes.get_errno()}")
        if libc.syscall(ctypes.c_long(_SYS_RESTRICT_SELF), ctypes.c_int(int(ruleset)),
                        ctypes.c_uint32(0)) != 0:
            raise LandlockError("landlock_failed", f"restrict errno={ctypes.get_errno()}")
    finally:
        os.close(int(ruleset))


def _add_path_rule(libc: ctypes.CDLL, ruleset: int, rule: PathRule) -> None:
    if rule.fd is not None:
        _add_rule_for_fd(libc, ruleset, rule, rule.fd)
        return
    try:
        fd = os.open(rule.path, os.O_PATH | os.O_CLOEXEC)
    except FileNotFoundError:
        if rule.required:
            raise LandlockError("landlock_failed", f"missing {rule.path}") from None
        return
    except OSError as exc:
        raise LandlockError("landlock_failed", f"open {rule.path}: {exc.errno}") from None
    try:
        _add_rule_for_fd(libc, ruleset, rule, fd)
    finally:
        os.close(fd)


def _add_rule_for_fd(libc: ctypes.CDLL, ruleset: int, rule: PathRule, fd: int) -> None:
    access = rule.access if stat.S_ISDIR(os.fstat(fd).st_mode) else rule.access & FILE_ACCESS
    if not access:
        raise LandlockError("landlock_failed", f"no access for {rule.path}")
    attr = _PathBeneathAttr(access, fd)
    if libc.syscall(ctypes.c_long(_SYS_ADD_RULE), ctypes.c_int(ruleset),
                    ctypes.c_int(_RULE_PATH_BENEATH), ctypes.byref(attr),
                    ctypes.c_uint32(0)) != 0:
        raise LandlockError("landlock_failed",
                            f"path rule {rule.path} errno={ctypes.get_errno()}")
