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
# ABI v2 以降で追加されたアクセス種別。
_ACCESS_FS_REFER = 1 << 13      # ABI v2 — ディレクトリ間の link/rename
_ACCESS_FS_TRUNCATE = 1 << 14   # ABI v3 — truncate(2)/ftruncate(2)/O_TRUNC
_ACCESS_FS_IOCTL_DEV = 1 << 15  # ABI v5 — デバイスファイルへの ioctl(2)

# ABI v1 の全 handled_access_fs (0x1FFF)。
_ABI_V1_HANDLED_ACCESS_FS = (
    _ACCESS_FS_EXECUTE | _ACCESS_FS_WRITE_FILE | _ACCESS_FS_READ_FILE |
    _ACCESS_FS_READ_DIR | _ACCESS_FS_REMOVE_DIR | _ACCESS_FS_REMOVE_FILE |
    _ACCESS_FS_MAKE_CHAR | _ACCESS_FS_MAKE_DIR | _ACCESS_FS_MAKE_REG |
    _ACCESS_FS_MAKE_SOCK | _ACCESS_FS_MAKE_FIFO | _ACCESS_FS_MAKE_BLOCK |
    _ACCESS_FS_MAKE_SYM
)

# **handled_access_fs に列挙しないアクセス種別は ruleset の判定対象外 =
# 素通しする。** レビュー 1 周目 (codex Critical / sonnet Critical、指揮者も
# 再現) — 旧稿は ABI v1 の 13 種だけを宣言していたため、ABI v3 で追加された
# `TRUNCATE` が完全にノーチェックになっていた。実測: `restrict_to` 適用後の
# プロセスから、**allowlist に一切列挙していない絶対パス**の `data/agentic.db`
# 相当ファイルを `os.truncate(p, 0)` で 0 バイトに破壊できた (read-only パスの
# 既存ファイルも同様)。通常の `write` は拒否されるため「効いているように
# 見える」のが厄介だった。設計書 §4.6 の「`data/` へ構造的に到達不能」は
# 機密性だけでなく**完全性・可用性**も含めて読む。
_HANDLED_ACCESS_FS = _ABI_V1_HANDLED_ACCESS_FS | _ACCESS_FS_TRUNCATE

# **意図的に handled に含めない種別とその脅威分析** (レビュー 1 周目 codex
# の要求 — 「ABI v1 の全種を列挙した」ことを「全 FS 操作を制限した」ことと
# 同一視しない):
# - `_ACCESS_FS_REFER` (v2): handled に**含めない方が fail closed**。Landlock
#   の仕様上、REFER を handled にしない場合は**ディレクトリ間の link/rename が
#   一律拒否**される。含めて許可を与えると逆に穴になる。
# - `_ACCESS_FS_IOCTL_DEV` (v5): デバイスファイルへの ioctl を制御する。本
#   モジュールの allowlist にデバイスノードを含むディレクトリを渡す想定が無く
#   (渡せばそれ自体が設計誤り)、デバイスファイルの open 自体が allowlist 外で
#   拒否されるため、handled にしなくても `data/` 到達には寄与しない。
#   **Task 18 で `/dev` を含むパスを allowlist に入れる場合はここを見直すこと。**

# 本モジュールが要求する最低 ABI。`TRUNCATE` (v3) を強制できない ABI 1/2 では
# 完全性を守れないため、improve profile を通してはならない (設計書 §4.6
# 「Landlock が利用不能な環境では improve profile の worker は起動拒否」)。
_REQUIRED_ABI = 3

_READ_ONLY_ACCESS = _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR
# `read_write_paths` は設計書 §4.6 の「専用 workdir 読書き」に対応する —
# **ディレクトリを作れない領域は workdir として使えない**ため、
# `MAKE_DIR`/`REMOVE_DIR` を含める (レビュー 1 周目 codex Important)。
# `TRUNCATE` は `open(..., "w")` (= `O_TRUNC`) に必要 — handled に入れた以上、
# rw 側には明示的に付与しないと既存ファイルの書き換えができなくなる。
# **`EXECUTE` はどのマスクにも与えていない** — 設計書 §6 の improve loop 許可
# ツールには `uv run pytest` 実行と `gh` による PR 作成が含まれるため、
# **Task 18 で実行権の与え方 (専用の exec_paths を設けるか、実行を親 RPC に
# 限定するか) を裁定する必要がある**。本 task では判断しない。
_READ_WRITE_ACCESS = (
    _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR | _ACCESS_FS_WRITE_FILE |
    _ACCESS_FS_MAKE_REG | _ACCESS_FS_REMOVE_FILE |
    _ACCESS_FS_MAKE_DIR | _ACCESS_FS_REMOVE_DIR | _ACCESS_FS_TRUNCATE)

_PR_SET_NO_NEW_PRIVS = 38


class LandlockUnavailable(Exception):
    """カーネル非対応・非対応アーキテクチャ・syscall 失敗の単一表現。"""


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64),
                ("parent_fd", ctypes.c_int32)]


def is_available() -> bool:
    """Landlock が**本モジュールの要求水準で**使えるかを返す。

    x86_64 以外は無条件 False。ABI が `_REQUIRED_ABI` (=3) 未満の場合も
    False — `TRUNCATE` を強制できないカーネルでは完全性を守れないため、
    「使えるが弱い」状態を許さず fail closed にする (レビュー 1 周目 codex)。
    """
    if platform.machine() != "x86_64":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    version = libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), None,
        ctypes.c_size_t(0), ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION))
    return version >= _REQUIRED_ABI


def restrict_to(*, read_only_paths: list[Path],
                read_write_paths: list[Path]) -> None:
    """呼び出しプロセスを Landlock で FS allowlist に制限する
    (**不可逆 — プロセス生涯にわたって有効**、以後の子プロセスにも継承
    される)。利用不能なら `LandlockUnavailable`。
    """
    if not is_available():
        raise LandlockUnavailable(
            "Landlock is not available (non-x86_64, or kernel ABI < "
            f"{_REQUIRED_ABI})")

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    ruleset_attr = _RulesetAttr(handled_access_fs=_HANDLED_ACCESS_FS)
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
