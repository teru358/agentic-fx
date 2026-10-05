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
import errno
import os
import platform
import stat as _stat
import struct
import sys
import sysconfig
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agentic_fx.core.plugin_files import PLUGIN_FILE_NAMES

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
#   → **見直し済み (プラン8 Task 18, 2026-08-09。レビュー 2 周目
#   `/code-review` が「この申し送りが未処理のまま `/dev` が入った」と指摘した
#   ことによる)**: improve profile は `/dev/urandom` のために `/dev` を
#   ディレクトリ単位で read-only 許可する (単一ファイル指定は `restrict_to` が
#   `O_PATH | O_DIRECTORY` で open するため不可 — ユーザー裁定でスコープ外)。
#   したがって**デバイスファイルの read open は成立し、その ioctl は ruleset の
#   外に残る**。`_ACCESS_FS_IOCTL_DEV` は ABI v5 の定義なので、handled に加える
#   なら `_REQUIRED_ABI` を 3 → 5 に上げることになり、v3/v4 カーネルで improve
#   profile が一律起動拒否になる (要求水準そのものの変更)。
#   **判断: 現状維持。** 根拠 — ①`/dev` 許可は `data/` 到達に寄与しない
#   (§4.6 の意味論は保たれる) ②`WorkerRunner` は `start_new_session=True` で
#   子を起動するため制御端末を持たず `/dev/tty` は `ENXIO` で開けない (実測)
#   ③具体的な escape 経路は特定されていない。
#   **プラン 9 で improve に実ツールセットを入れる前に再評価すること。**

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
# **`MAKE_SOCK`** (A-4 検収是正 2026-08-22, advisor 指摘): `mission_worker.py`
# の improve 分岐 (`_start_mcp_dispatcher`) は `workdir/afx.sock` に
# `AF_UNIX` の `bind()` を行う (プラン10 Task4 Step 7)。Landlock は
# `bind(2)` を `LANDLOCK_ACCESS_FS_MAKE_SOCK` で制御するため、これを
# 欠くと workdir 配下であっても bind が `PermissionError` (EACCES 相当)
# になる — 実測: この 1 行を追加する前は
# `test_worker_runner_reaps_real_cli_pgid_via_mission_worker_wiring`
# (実 Landlock を踏む) が `_start_mcp_dispatcher` の fail-closed raise
# 経由で `ready: ok=False` になり timeout で red になっていた。
_READ_WRITE_ACCESS = (
    _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR | _ACCESS_FS_WRITE_FILE |
    _ACCESS_FS_MAKE_REG | _ACCESS_FS_REMOVE_FILE |
    _ACCESS_FS_MAKE_DIR | _ACCESS_FS_REMOVE_DIR | _ACCESS_FS_TRUNCATE |
    _ACCESS_FS_MAKE_SOCK)

# プラン10 Task 5: execute パスの自己充足マスク。
# EXECUTE | READ_FILE | READ_DIR の和で、同一 inode に対する ro ルールとの
# 併合に依存しない。
_EXECUTE_ACCESS = (
    _ACCESS_FS_EXECUTE | _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR)

# プラン10 Task 5 5-C 改訂 (2026-08-22, 裁定 A): ファイル (通常ファイル) 単位
# の EXECUTE マスク。**READ_DIR を含めてはならない** — 通常ファイルの fd に
# READ_DIR を含む allowed_access を渡すと landlock_add_rule が EINVAL を返す
# (probe 実測)。ディレクトリ単位の `_EXECUTE_ACCESS` とは別のマスクにするのは
# 意図的 — 「ディレクトリ単位の EXECUTE」と「ファイル単位の EXECUTE」という
# セキュリティ上の差異を型 (mask) に残す。
_EXECUTE_FILE_ACCESS = _ACCESS_FS_EXECUTE | _ACCESS_FS_READ_FILE

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


def _assert_allowlist_excludes_data_dir(
    paths: list[Path], *, guarded_data_dir: Path,
) -> None:
    """allowlist のどの 1 パスも `guarded_data_dir` の祖先・一致・子孫で
    ないことを確認し、違反したら fail closed する
    (プラン8 Task 18 / プラン10 Task 5 で `core/landlock.py` へ移設)。
    呼び出し側 (`mission_worker._bootstrap_improve_profile`) が
    `guarded_data_dir` を自身の独立した式 (`__file__` 由来、`code_root`
    からは導かない) で算出して渡す責務を持つ — このヘルパ自体は
    「渡された値同士の包含関係」だけを機械的に見る。"""
    for p in paths:
        resolved = Path(p).resolve()
        if (resolved == guarded_data_dir
                or resolved in guarded_data_dir.parents
                or guarded_data_dir in resolved.parents):
            raise RuntimeError(
                f"improve worker allowlist would expose the history data "
                f"directory: {resolved} covers or lives under "
                f"{guarded_data_dir} — refusing to start "
                "(fail closed, 設計書 §2.1)")


def elf_interpreter(path: Path) -> Path | None:
    """`path` の ELF PT_INTERP を読み、interpreter の実体パスを返す。
    static / static-pie (PT_INTERP 無し) なら None。
    **Landlock 適用前に呼ぶこと** (適用後は読めない場合がある)。

    非 ELF・読取不能は `RuntimeError` (fail closed)。呼び出し側が
    `#!/usr/bin/env node` 型のシェバンラッパを誤って渡した場合、黙って
    None を返すと後段で理由の分からない exec 失敗になるため
    (プラン10 Task 5 5-C 改訂, 2026-08-22, 裁定 A)。
    """
    with open(path, "rb") as f:
        head = f.read(64)
        if len(head) < 64 or head[:4] != b"\x7fELF":
            raise RuntimeError(
                f"{path} is not an ELF executable — cannot determine its "
                "dynamic loader (improve worker refuses to start, fail closed)")
        e_phoff, = struct.unpack_from("<Q", head, 0x20)
        e_phentsize, = struct.unpack_from("<H", head, 0x36)
        e_phnum, = struct.unpack_from("<H", head, 0x38)
        f.seek(e_phoff)
        ph = f.read(e_phentsize * e_phnum)
    for i in range(e_phnum):
        o = i * e_phentsize
        if struct.unpack_from("<I", ph, o)[0] != 3:   # PT_INTERP
            continue
        p_offset, = struct.unpack_from("<Q", ph, o + 8)
        p_filesz, = struct.unpack_from("<Q", ph, o + 32)
        with open(path, "rb") as f:
            f.seek(p_offset)
            raw = f.read(p_filesz)
        interp = raw.split(b"\x00")[0].decode()
        return Path(interp).resolve()
    return None


def interpreter_files_for(exec_targets: Sequence[Path]) -> list[Path]:
    """exec 対象群が要する動的ローダの実ファイル集合 (重複除去、実在のみ)。"""
    out: list[Path] = []
    for t in exec_targets:
        interp = elf_interpreter(Path(t).resolve())
        if interp is not None and interp.is_file() and interp not in out:
            out.append(interp)
    return out


def restrict_to(*, read_only_paths: list[Path],
                read_write_paths: list[Path],
                execute_paths: Sequence[Path] = (),
                execute_file_paths: Sequence[Path] = ()) -> None:
    """呼び出しプロセスを Landlock で FS allowlist に制限する
    (**不可逆 — プロセス生涯にわたって有効**、以後の子プロセスにも継承
    される)。利用不能なら `LandlockUnavailable`。

    execute_paths は `_EXECUTE_ACCESS` (EXECUTE|READ_FILE|READ_DIR の自己充足
    マスク) で許可する。同一 inode に対する read_only ルールとの併合には
    依存しない (probe landlock_probe.py と同一設計)。

    execute_file_paths は**通常ファイル単位**で `_EXECUTE_FILE_ACCESS`
    (EXECUTE|READ_FILE、READ_DIR を含まない) を許可する — ディレクトリ単位の
    `execute_paths` とは異なり `O_PATH` のみで開く (`O_DIRECTORY` を付けない)。
    ディレクトリが渡された場合は `S_ISREG` 検査で拒否する (プラン10 Task 5
    5-C 改訂, 2026-08-22, 裁定 A — カーネルはディレクトリ fd +
    EXECUTE|READ_FILE を黙って受理してしまうため、呼び出し側のミスを
    ここで fail closed にする)。
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
        for path, access, is_dir in (
                *((p, _READ_ONLY_ACCESS, True) for p in read_only_paths),
                *((p, _READ_WRITE_ACCESS, True) for p in read_write_paths),
                *((p, _EXECUTE_ACCESS, True) for p in execute_paths),
                *((p, _EXECUTE_FILE_ACCESS, False) for p in execute_file_paths)):
            if not is_dir and not _stat.S_ISREG(os.stat(str(path)).st_mode):
                # カーネルは dir fd + (EXECUTE|READ_FILE) を**黙って受理する**
                # (probe 実測 rc=0)。READ_DIR 無しの再帰付与という壊れた
                # ルールが無検出で入るため、呼び出し側のミスをここで
                # fail closed する。
                raise LandlockUnavailable(
                    f"execute_file_paths entry is not a regular file: {path}")
            parent_fd = os.open(str(path),
                                os.O_PATH | (os.O_DIRECTORY if is_dir else 0))
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


# ---------------------------------------------------------------------------
# plugin worker 用の ruleset
#
# `restrict_to` (improve / gate の dir 単位 allowlist) とは別系統。plugin worker は
# 読み取り専用の最小規則を「検査済み fd」だけで組み、ABI に応じて network / scope /
# TSYNC を足す。`restrict_to` と上の定数は変えない。
# ---------------------------------------------------------------------------

_ACCESS_NET_BIND_TCP = 1 << 0     # ABI v4
_ACCESS_NET_CONNECT_TCP = 1 << 1  # ABI v4
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI v6
_SCOPE_SIGNAL = 1 << 1                # ABI v6
# landlock_restrict_self の flag。ABI 8 以上で、呼び出し元以外の全 thread にも
# 同じ domain を掛ける。
RESTRICT_SELF_TSYNC = 1 << 3

# 本 profile が定義済みの最大 ABI。これを超える kernel でも ABI 8 の定義で掛ける。
_MAX_KNOWN_ABI = 8
# plugin worker が許す最低 ABI (TRUNCATE を強制できること)。
PLUGIN_MIN_ABI = _REQUIRED_ABI

# ABI ごとの ruleset 構造体の大きさ (fs のみ / + net / + scope)。
_RULESET_SIZE_FS = 8
_RULESET_SIZE_NET = 16
_RULESET_SIZE_SCOPE = 24

# 規則の種別。
KIND_RUNTIME = "runtime"
KIND_SYSTEM = "system"
KIND_PLUGIN = "plugin"
KIND_PLUGIN_FILE = "plugin_file"
KIND_DEVICE = "device"

_DEVICE_PATH = "/dev/urandom"

SANDBOX_REASONS = (
    "landlock_abi_too_old", "landlock_task_inspection_failed",
    "landlock_multithreaded", "runtime_root_too_wide", "fd_open_failed",
    "allowlist_guarded", "allowlist_not_leaf", "plugin_file_invalid",
    "landlock_create_failed", "landlock_add_rule_failed",
    "no_new_privs_failed", "landlock_restrict_failed",
)


class LandlockSetupError(Exception):
    """plugin worker 用 ruleset の準備・適用の失敗。`reason` は固定の enum
    (`SANDBOX_REASONS`) で、呼び出し側が公開用の分類へ写す。`detail` は
    技術ログ用の自由文。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def landlock_abi() -> int:
    """kernel が提供する Landlock ABI 版を返す。非 x86_64・Landlock 無効・
    syscall 失敗は 0。"""
    if platform.machine() != "x86_64":
        return 0
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    version = libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), None, ctypes.c_size_t(0),
        ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION))
    return int(version) if version > 0 else 0


@dataclass(frozen=True)
class RulesetPlan:
    """ABI から決まる ruleset の形 (純データ)。"""

    abi: int
    handled_fs: int
    handled_net: int
    handled_scope: int
    attr_size: int
    restrict_flags: int
    tsync: str      # "applied" (flag で全 thread) | "single_task" (適用前に task 1 本を検査)
    network: str    # "applied" | "unsupported"
    scope: str      # "applied" | "unsupported"


def plan_for_abi(abi: int) -> RulesetPlan:
    """ABI 3 以上から ruleset の構造を決める。3 未満は `landlock_abi_too_old`。

    handled FS は ABI によらず同じ (EXECUTE を含む v1 の 13 種 + TRUNCATE)。
    network は ABI 4+、scope は ABI 6+、TSYNC は ABI 8+ だけ足す。TSYNC が
    使えない ABI で flag を渡すと kernel が EINVAL にするため、その ABI では
    flags 0 とし、呼び出し側が single-task を検査する。"""
    if abi < PLUGIN_MIN_ABI:
        raise LandlockSetupError("landlock_abi_too_old", f"abi={abi}")
    abi = min(abi, _MAX_KNOWN_ABI)
    net = abi >= 4
    scope = abi >= 6
    tsync = abi >= 8
    return RulesetPlan(
        abi=abi,
        handled_fs=_HANDLED_ACCESS_FS,
        handled_net=(_ACCESS_NET_BIND_TCP | _ACCESS_NET_CONNECT_TCP) if net else 0,
        handled_scope=(_SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL) if scope else 0,
        attr_size=(_RULESET_SIZE_SCOPE if scope
                   else _RULESET_SIZE_NET if net else _RULESET_SIZE_FS),
        restrict_flags=RESTRICT_SELF_TSYNC if tsync else 0,
        tsync="applied" if tsync else "single_task",
        network="applied" if net else "unsupported",
        scope="applied" if scope else "unsupported",
    )


def check_single_task(task_dir: str = "/proc/self/task") -> None:
    """TSYNC を使えない ABI で、適用時に自 process の task が 1 本だけで
    あることを確認する (他の thread に domain が及ばないため)。"""
    try:
        n = len(os.listdir(task_dir))
    except OSError as exc:
        raise LandlockSetupError(
            "landlock_task_inspection_failed", f"{task_dir}: {exc}") from None
    if n != 1:
        raise LandlockSetupError("landlock_multithreaded", f"tasks={n}")


# --- guarded root と allowlist の自己検査 (純関数) -----------------------------

def default_repo_root() -> Path:
    """この module の位置から導く repo root (呼び出し元の引数・handshake からは
    導かない)。"""
    return Path(__file__).resolve().parents[3]


def default_home() -> Path:
    return Path(os.path.expanduser("~")).resolve()


@dataclass(frozen=True)
class GuardedRoots:
    """full = 祖先・一致・子孫のどれも allowlist に入れない。
    cover = 祖先・一致 (= その root を覆う規則) を入れない。"""

    full: tuple[Path, ...]
    cover: tuple[Path, ...]


def guarded_roots(repo_root: Path, home: Path) -> GuardedRoots:
    repo_root, home = Path(repo_root).resolve(), Path(home).resolve()
    full = (repo_root / "data", repo_root / "config", repo_root / "logs",
            home / ".config")
    cover = (repo_root, repo_root / "plugins",
             repo_root / "plugins" / "_staging", home, Path("/tmp"), Path("/"))
    return GuardedRoots(tuple(p.resolve() for p in full),
                        tuple(p.resolve() for p in cover))


def path_relation(a: Path, b: Path) -> str | None:
    """a から見た b との関係: "equal" | "descendant" (a が b の子孫) |
    "ancestor" (a が b の祖先) | None。"""
    if a == b:
        return "equal"
    if b in a.parents:
        return "descendant"
    if a in b.parents:
        return "ancestor"
    return None


def check_runtime_root(path: Path, *, repo_root: Path, home: Path) -> None:
    """runtime root が `/`、`/usr`、`/usr/local`、`/opt`、`/var`、`/home`、
    `$HOME`、repo root の一致または祖先なら `runtime_root_too_wide`。"""
    wide = [Path(p).resolve() for p in ("/", "/usr", "/usr/local", "/opt",
                                         "/var", "/home")]
    wide += [Path(home).resolve(), Path(repo_root).resolve()]
    for w in wide:
        rel = path_relation(path, w)
        if rel in ("equal", "ancestor"):
            raise LandlockSetupError(
                "runtime_root_too_wide", f"{path} is {rel} of {w}")


def check_allowlist_path(kind: str, path: Path, guarded: GuardedRoots) -> None:
    """allowlist の 1 エントリを guarded root と照合し、違反は
    `allowlist_guarded`。plugin_file は親 dir (plugin) の検査で代表する。
    runtime / system は `/tmp` 配下も拒否する (`/tmp` 配下を取れるのは plugin
    leaf だけ)。"""
    if kind == KIND_PLUGIN_FILE:
        return
    for g in guarded.full:
        rel = path_relation(path, g)
        if rel:
            raise LandlockSetupError(
                "allowlist_guarded", f"{kind} {path} is {rel} of {g}")
    for g in guarded.cover:
        rel = path_relation(path, g)
        if rel in ("equal", "ancestor"):
            raise LandlockSetupError(
                "allowlist_guarded", f"{kind} {path} is {rel} of {g}")
    if kind in (KIND_RUNTIME, KIND_SYSTEM):
        if path_relation(path, Path("/tmp").resolve()) == "descendant":
            raise LandlockSetupError(
                "allowlist_guarded", f"{kind} {path} is under /tmp")


def runtime_subtree(code_root: Path, *, paths: dict[str, str] | None = None,
                    sys_path: Sequence[str] | None = None) -> list[Path]:
    """runtime として読ませる dir の集合。`code_root` (src/agentic_fx)、
    `sysconfig` の stdlib / platstdlib / purelib / platlib、`sys.path` 上の
    実体の lib-dynload だけ。実在する dir に限り、子孫は親へ畳む。"""
    paths = paths if paths is not None else sysconfig.get_paths()
    sys_path = sys_path if sys_path is not None else sys.path
    cand = [Path(code_root)]
    cand += [Path(paths[k]) for k in ("stdlib", "platstdlib", "purelib",
                                       "platlib") if k in paths]
    cand += [Path(p) for p in sys_path if str(p).endswith("lib-dynload")]
    real: list[Path] = []
    for p in cand:
        r = p.resolve()
        if r.is_dir() and r not in real:
            real.append(r)
    return [r for r in real
            if not any(o != r and o in r.parents for o in real)]


#: worker が必ず read 規則を張って開く system dir。存在しない host では worker が
#: 起動段で `fd_open_failed` になるので、host preflight が先に明確な reason を出す。
REQUIRED_SYSTEM_DIRS = (Path("/usr/lib"), Path("/usr/share/zoneinfo"))


def system_dirs() -> list[Path]:
    """配布物だけを置く読み取り専用 dir。実体が別なら `/usr/lib64`・`/lib64` も。"""
    out = list(REQUIRED_SYSTEM_DIRS)
    for extra in (Path("/usr/lib64"), Path("/lib64")):
        if extra.exists() and extra.resolve() not in [p.resolve() for p in out]:
            out.append(extra)
    return out


def missing_required_system_dirs() -> list[Path]:
    """`REQUIRED_SYSTEM_DIRS` のうち存在しないもの。tzdata の無い minimal 環境で
    zoneinfo が欠けると worker が起動できないため、host preflight が使う。"""
    return [p for p in REQUIRED_SYSTEM_DIRS if not p.exists()]


# --- allowlist (fd 系統) と適用 ------------------------------------------------

@dataclass(frozen=True)
class Rule:
    kind: str
    path: Path
    fd: int
    access: int


@dataclass
class Allowlist:
    """検査済み fd の規則集合。`plugin_dir_fds` (実体 path -> dir fd) は後段の
    loader が `openat` に使うため、`close_rule_fds` の後も残す。plugin dir 自体
    には規則を張らない。"""

    rules: list[Rule] = field(default_factory=list)
    plugin_dir_fds: dict[str, int] = field(default_factory=dict)

    def close_rule_fds(self) -> None:
        keep = set(self.plugin_dir_fds.values())
        for r in self.rules:
            if r.fd not in keep:
                _close_quietly(r.fd)

    def close_all(self) -> None:
        for r in self.rules:
            _close_quietly(r.fd)
        for fd in self.plugin_dir_fds.values():
            _close_quietly(fd)


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _open_dir_fd(path: Path, kind: str) -> tuple[int, Path]:
    try:
        fd = os.open(str(path), os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise LandlockSetupError("fd_open_failed", f"{kind} {path}: {exc}") from None
    try:
        real = Path(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError as exc:
        os.close(fd)
        raise LandlockSetupError("fd_open_failed", f"{kind} {path}: {exc}") from None
    return fd, real


def _open_plugin_file(dir_fd: int, name: str) -> int:
    """検査済み dir fd から `O_PATH|O_NOFOLLOW` で開く。symlink は symlink 自身の
    fd になるので通常ファイル検査で落ちる。"""
    try:
        fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=dir_fd)
    except OSError as exc:
        # fd / メモリの枯渇は環境側の事情で、候補の plugin が不正という意味にしない
        reason = ("fd_open_failed"
                  if exc.errno in (errno.EMFILE, errno.ENFILE, errno.ENOMEM)
                  else "plugin_file_invalid")
        raise LandlockSetupError(reason, f"{name}: {exc}") from None
    if not _stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise LandlockSetupError(
            "plugin_file_invalid", f"{name} is not a regular file")
    return fd


def build_allowlist(plugin_dirs: Sequence[Path], *,
                    repo_root: Path | None = None, home: Path | None = None,
                    ) -> Allowlist:
    """runtime / system / plugin file / device の規則を検査済み fd で組む。

    plugin は親が選んだ leaf だけを受け、dir には規則を張らず、直下の
    `plugin.py` と `config.yaml` の 2 ファイルの inode に READ_FILE だけを張る。
    失敗時は開いた fd を全て閉じてから `LandlockSetupError`。"""
    repo_root = Path(repo_root).resolve() if repo_root else default_repo_root()
    home = Path(home).resolve() if home else default_home()
    guarded = guarded_roots(repo_root, home)
    code_root = Path(__file__).resolve().parents[1]
    allow = Allowlist()
    try:
        seen: set[Path] = set()
        entries: list[tuple[str, Path, int]] = []
        groups = ((KIND_RUNTIME, runtime_subtree(code_root)),
                  (KIND_SYSTEM, system_dirs()),
                  (KIND_PLUGIN, [Path(p) for p in plugin_dirs]))
        for kind, group in groups:
            for p in group:
                fd, real = _open_dir_fd(p, kind)
                if real in seen:
                    os.close(fd)
                    continue
                seen.add(real)
                entries.append((kind, real, fd))
                # 失敗時に漏らさないよう、開いたらすぐ Allowlist に載せる
                if kind == KIND_PLUGIN:
                    allow.plugin_dir_fds[str(real)] = fd
                else:
                    allow.rules.append(Rule(kind, real, fd, _READ_ONLY_ACCESS))
        try:
            dev_fd = os.open(_DEVICE_PATH, os.O_PATH | os.O_CLOEXEC)
        except OSError as exc:
            raise LandlockSetupError(
                "fd_open_failed", f"{_DEVICE_PATH}: {exc}") from None
        allow.rules.append(Rule(KIND_DEVICE, Path(_DEVICE_PATH), dev_fd,
                                _ACCESS_FS_READ_FILE))
        if not _stat.S_ISCHR(os.fstat(dev_fd).st_mode):
            raise LandlockSetupError(
                "fd_open_failed", f"{_DEVICE_PATH} is not a character device")

        for kind, real, _fd in entries:
            if kind == KIND_RUNTIME:
                check_runtime_root(real, repo_root=repo_root, home=home)
        for kind, real, _fd in entries:
            check_allowlist_path(kind, real, guarded)
        for kind, real, fd in entries:
            if kind != KIND_PLUGIN:
                continue
            try:
                st = os.stat("plugin.py", dir_fd=fd, follow_symlinks=False)
            except OSError:
                st = None
            if st is None or not _stat.S_ISREG(st.st_mode):
                raise LandlockSetupError(
                    "allowlist_not_leaf", f"{real} has no regular plugin.py")
            for name in PLUGIN_FILE_NAMES:
                ffd = _open_plugin_file(fd, name)
                allow.rules.append(
                    Rule(KIND_PLUGIN_FILE, real / name, ffd, _ACCESS_FS_READ_FILE))
    except BaseException:
        allow.close_all()
        raise
    return allow


def restrict_with_allowlist(allow: Allowlist, plan: RulesetPlan) -> None:
    """ruleset を作り、規則を足し、NO_NEW_PRIVS の後に restrict_self する
    (不可逆)。flag は `plan.restrict_flags` のまま使い、失敗しても flags 0 へ
    落とさない。"""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    class _Attr(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64),
                    ("handled_access_net", ctypes.c_uint64),
                    ("scoped", ctypes.c_uint64)]

    attr = _Attr(plan.handled_fs, plan.handled_net, plan.handled_scope)
    rfd = libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), ctypes.byref(attr),
        ctypes.c_size_t(plan.attr_size), ctypes.c_uint32(0))
    if rfd < 0:
        raise LandlockSetupError(
            "landlock_create_failed",
            f"errno={ctypes.get_errno()} size={plan.attr_size}")
    try:
        for r in allow.rules:
            rule = _PathBeneathAttr(allowed_access=r.access, parent_fd=r.fd)
            rc = libc.syscall(
                ctypes.c_long(_SYS_LANDLOCK_ADD_RULE), ctypes.c_int(rfd),
                ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH), ctypes.byref(rule),
                ctypes.c_uint32(0))
            if rc != 0:
                raise LandlockSetupError(
                    "landlock_add_rule_failed",
                    f"{r.kind} {r.path} errno={ctypes.get_errno()}")
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise LandlockSetupError(
                "no_new_privs_failed", f"errno={ctypes.get_errno()}")
        rc = libc.syscall(ctypes.c_long(_SYS_LANDLOCK_RESTRICT_SELF),
                          ctypes.c_int(rfd),
                          ctypes.c_uint32(plan.restrict_flags))
        if rc != 0:
            raise LandlockSetupError(
                "landlock_restrict_failed",
                f"errno={ctypes.get_errno()} flags={plan.restrict_flags}")
    finally:
        os.close(rfd)


@dataclass(frozen=True)
class AppliedRuleset:
    """適用結果。`plugin_dir_fds` は loader 用に開いたまま返す (呼び出し側が閉じる)。"""

    plan: RulesetPlan
    plugin_dir_fds: dict[str, int]

    def attestation_fields(self) -> dict[str, object]:
        """二段 protocol の attested field のうち Landlock 由来の 4 つ。"""
        return {"landlock_fs_abi": self.plan.abi,
                "landlock_tsync": self.plan.tsync,
                "network": self.plan.network, "scope": self.plan.scope}


def apply_plugin_ruleset(plugin_dirs: Sequence[Path], *,
                         repo_root: Path | None = None,
                         home: Path | None = None) -> AppliedRuleset:
    """plugin worker の process に Landlock を掛ける (不可逆、継承される)。

    順序: ABI 測定 → (TSYNC 不可の ABI だけ) task 数 1 の検査 → allowlist を
    fd で組んで自己検査 → ruleset 適用。ABI 8 以上は `RESTRICT_SELF_TSYNC` で
    全 thread に及ぼす。常に kernel の実 ABI を使う。失敗は
    `LandlockSetupError`。"""
    return _apply_plugin_ruleset_for_abi(
        plugin_dirs, landlock_abi(), repo_root=repo_root, home=home)


def _apply_plugin_ruleset_for_abi(plugin_dirs: Sequence[Path], abi: int, *,
                                  repo_root: Path | None = None,
                                  home: Path | None = None) -> AppliedRuleset:
    """旧 ABI の分岐をこの kernel 上で検査するテスト専用。本体コードから
    呼ばない。実 ABI を超える値は実 ABI に切り詰める。"""
    abi = min(abi, landlock_abi())
    plan = plan_for_abi(abi)
    if plan.tsync == "single_task":
        check_single_task()
    allow = build_allowlist(plugin_dirs, repo_root=repo_root, home=home)
    try:
        # 規則を組む間に増えた thread は domain を受けないので、適用の直前にもう一度確かめる
        if plan.tsync == "single_task":
            check_single_task()
        restrict_with_allowlist(allow, plan)
    except BaseException:
        allow.close_all()
        raise
    allow.close_rule_fds()
    return AppliedRuleset(plan=plan, plugin_dir_fds=dict(allow.plugin_dir_fds))
