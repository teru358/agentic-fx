"""plugin worker の seccomp allowlist `allow/6` (x86_64 専用)。

filter はこのモジュールのデータ表 (`ALLOW6_TABLE`) から機械的に生成する。
命令列を手で書かないのは、表と実際の判定がずれる余地を消すため。

評価順 (上から最初に当たった行で決まる):

1. arch が x86_64 でない、または x32 ABI (`nr >= 0x40000000`) → KILL_PROCESS
2. 番号表の外 (`nr >= table_end`) → KILL_PROCESS。default とは別の return
   命令に飛ばし、default を緩める変更があっても未知の番号は殺されたままにする
3. 無条件 allow
4. 名前つき errno (`socket` / `io_uring_setup` は EACCES、process 生成と exec は EPERM)
5. fallback 集合 (`clone3` だけ ENOSYS。glibc が `clone` へ切り替える)
6. 引数条件つき allow (条件の外は行ごとの errno)
7. それ以外 → default (KILL_PROCESS)

**filter の適用は必ず使い捨ての子 process の中で行う。** 適用は取り消せず、
呼んだ process の全 thread に及ぶ (TSYNC)。
"""
from __future__ import annotations

import ctypes
import errno as _errno
import os
import platform
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

PROFILE_NAME = "allow"
PROFILE_VERSION = 6
SECCOMP_PROFILE = f"{PROFILE_NAME}/{PROFILE_VERSION}"

# --- BPF と seccomp の定数 (x86_64) ------------------------------------------
BPF_LD_W_ABS = 0x20
BPF_JEQ_K = 0x15
BPF_JGE_K = 0x35
BPF_JSET_K = 0x45
BPF_ALU_AND_K = 0x54
BPF_RET_K = 0x06

AUDIT_ARCH_X86_64 = 0xC000003E
X32_SYSCALL_BIT = 0x40000000

RET_KILL_PROCESS = 0x80000000
RET_ALLOW = 0x7FFF0000
_RET_ERRNO_BASE = 0x00050000


def ret_errno(err: int) -> int:
    """`SECCOMP_RET_ERRNO` の戻り値。"""
    return _RET_ERRNO_BASE | (err & 0xFFFF)


# seccomp_data のオフセット: nr = 0、arch = 4、args[i] = 16 + 8*i (little endian)
_OFF_NR = 0
_OFF_ARCH = 4


def _arg_lo(i: int) -> int:
    return 16 + 8 * i


def _arg_hi(i: int) -> int:
    return 20 + 8 * i


_SYS_SECCOMP = 317
_SECCOMP_SET_MODE_FILTER = 1
_SECCOMP_GET_ACTION_AVAIL = 2
SECCOMP_FILTER_FLAG_TSYNC = 1
SECCOMP_FILTER_FLAG_LOG = 2
_PR_SET_NO_NEW_PRIVS = 38

# kernel が受け付ける命令数の上限 (BPF_MAXINSNS)
MAX_INSTRUCTIONS = 4096
# 条件分岐の jt/jf は 8 bit なので、飛び先は 255 命令先まで
_MAX_JUMP = 255

SIGSYS = 31
_CLONE_VM = 0x100
_CLONE_SIGHAND = 0x800
_CLONE_THREAD = 0x10000
CLONE_THREAD_TRIPLE = _CLONE_VM | _CLONE_SIGHAND | _CLONE_THREAD
# CLONE_NEWTIME | NEWNS | NEWCGROUP | NEWUTS | NEWIPC | NEWUSER | NEWPID | NEWNET
CLONE_NEW_MASK = (0x80 | 0x20000 | 0x02000000 | 0x04000000 | 0x08000000
                  | 0x10000000 | 0x20000000 | 0x40000000)


# --- 表の行 ----------------------------------------------------------------

@dataclass(frozen=True)
class AllowRule:
    """無条件に許す syscall。`source` は許可の出所 (実測 / thread 実測 / 明示)。"""
    name: str
    nr: int
    source: str
    reason: str


@dataclass(frozen=True)
class ErrnoRule:
    """名前つきで errno を返す syscall (呼んだ側が読める拒否)。"""
    name: str
    nr: int
    errno: int
    reason: str


@dataclass(frozen=True)
class FallbackRule:
    """ENOSYS を返す syscall。ENOSYS で旧い syscall へ切り替わることを
    差分試験で示したものだけを入れる。"""
    name: str
    nr: int
    evidence: str


@dataclass(frozen=True)
class FlagsThreadOnly:
    """arg の下位 32 bit に `required` が全部あり、`forbidden` が 1 つも無いときだけ許す。"""
    arg: int
    required: int
    forbidden: int


@dataclass(frozen=True)
class SelfPidNotSignal:
    """pid 引数が filter 生成時の自 pid で、signal 引数が `denied_signal` でないときだけ許す。"""
    pid_arg: int
    signal_arg: int
    denied_signal: int


@dataclass(frozen=True)
class PidZeroOrSelf:
    """pid 引数が 0 か filter 生成時の自 pid のときだけ許す。"""
    pid_arg: int


@dataclass(frozen=True)
class ArgInSet:
    """arg の下位 32 bit が `values` のどれかのときだけ許す。"""
    arg: int
    values: tuple[tuple[int, str], ...]


Condition = FlagsThreadOnly | SelfPidNotSignal | PidZeroOrSelf | ArgInSet


@dataclass(frozen=True)
class ConditionalRule:
    """引数条件つきで許す syscall。条件の外は `deny_errno`。"""
    name: str
    nr: int
    condition: Condition
    deny_errno: int
    reason: str


@dataclass(frozen=True)
class FilterTable:
    """filter を生成する元のデータ。"""
    allow: tuple[AllowRule, ...]
    named_errno: tuple[ErrnoRule, ...]
    fallback_enosys: tuple[FallbackRule, ...]
    conditional: tuple[ConditionalRule, ...]
    table_end: int
    default_action: int


_M = "measured"
_T = "threaded"
_E = "explicit"

_ALLOW6 = (
    AllowRule("read", 0, _M, "継承 pipe と許可済み read-only fd"),
    AllowRule("write", 1, _M, "継承 pipe と匿名 file"),
    AllowRule("close", 3, _M, "fd の後始末"),
    AllowRule("stat", 4, _M, "import 探索"),
    AllowRule("fstat", 5, _M, "import 探索"),
    AllowRule("lstat", 6, _M, "import 探索"),
    AllowRule("lseek", 8, _M, "許可済み fd の位置"),
    AllowRule("mmap", 9, _M, "メモリ確保と共有ライブラリ読込"),
    AllowRule("mprotect", 10, _M, "共有ライブラリ読込"),
    AllowRule("munmap", 11, _M, "メモリ解放"),
    AllowRule("brk", 12, _M, "メモリ確保"),
    AllowRule("rt_sigaction", 13, _M, "Python signal handler"),
    AllowRule("rt_sigprocmask", 14, _T, "thread の開始と終了"),
    AllowRule("rt_sigreturn", 15, _E, "signal handler からの復帰"),
    AllowRule("pread64", 17, _M, "許可済み fd の読取"),
    AllowRule("sched_yield", 24, _E, "BLAS の譲り"),
    AllowRule("mremap", 25, _E, "自メモリの realloc"),
    AllowRule("madvise", 28, _T, "thread stack の解放"),
    AllowRule("nanosleep", 35, _E, "旧 glibc の sleep"),
    AllowRule("getpid", 39, _E, "自 pid"),
    AllowRule("exit", 60, _T, "thread の終了"),
    AllowRule("uname", 63, _M, "platform"),
    AllowRule("gettimeofday", 96, _E, "vDSO fallback"),
    AllowRule("getrusage", 98, _M, "自己 CPU 観測"),
    AllowRule("gettid", 186, _T, "thread の開始"),
    AllowRule("time", 201, _E, "vDSO fallback"),
    AllowRule("futex", 202, _M, "自メモリの lock"),
    AllowRule("sched_getaffinity", 204, _M, "CPU 数判定"),
    AllowRule("getdents64", 217, _M, "到達先は Landlock が判定"),
    AllowRule("restart_syscall", 219, _E, "割り込まれた syscall の再開"),
    AllowRule("clock_gettime", 228, _E, "vDSO fallback"),
    AllowRule("clock_getres", 229, _E, "vDSO fallback"),
    AllowRule("clock_nanosleep", 230, _E, "wall timeout 内の sleep"),
    AllowRule("exit_group", 231, _M, "process 終了"),
    AllowRule("mbind", 237, _M, "numpy allocator の自 process NUMA 方針"),
    AllowRule("openat", 257, _M, "到達先は Landlock が判定"),
    AllowRule("newfstatat", 262, _M, "import 探索"),
    AllowRule("set_robust_list", 273, _T, "thread の開始"),
    AllowRule("epoll_create1", 291, _M, "runtime import"),
    AllowRule("getrandom", 318, _E, "乱数 seed"),
    AllowRule("rseq", 334, _T, "thread の開始"),
)

_NAMED_ERRNO6 = (
    ErrnoRule("socket", 41, _errno.EACCES, "socket 到達を閉じる"),
    ErrnoRule("io_uring_setup", 425, _errno.EACCES, "io_uring 経由の迂回を閉じる"),
    ErrnoRule("fork", 57, _errno.EPERM, "process を作らせない"),
    ErrnoRule("vfork", 58, _errno.EPERM, "process を作らせない"),
    ErrnoRule("execve", 59, _errno.EPERM, "別の実行イメージへ移らせない"),
    ErrnoRule("memfd_create", 319, _errno.EPERM, "memfd からの exec を閉じる"),
    ErrnoRule("execveat", 322, _errno.EPERM, "fd からの exec を閉じる"),
)

_FALLBACK6 = (
    FallbackRule(
        "clone3", 435,
        "KILL では OpenBLAS 4 thread と threading が SIGSYS で失敗し、"
        "ENOSYS では glibc が clone へ切り替えて完走する"),
)

_CONDITIONAL6 = (
    ConditionalRule(
        "clone", 56,
        FlagsThreadOnly(arg=0, required=CLONE_THREAD_TRIPLE, forbidden=CLONE_NEW_MASK),
        _errno.EPERM, "thread だけを許す"),
    ConditionalRule(
        "kill", 62, SelfPidNotSignal(pid_arg=0, signal_arg=1, denied_signal=SIGSYS),
        _errno.EPERM, "自 process の通常 signal だけ"),
    ConditionalRule(
        "tgkill", 234, SelfPidNotSignal(pid_arg=0, signal_arg=2, denied_signal=SIGSYS),
        _errno.EPERM, "自 thread group の通常 signal だけ"),
    ConditionalRule(
        "prlimit64", 302, PidZeroOrSelf(pid_arg=0),
        _errno.EPERM, "自 process の rlimit 参照と引下げだけ"),
    ConditionalRule(
        "fcntl", 72,
        ArgInSet(arg=1, values=((1, "F_GETFD"), (2, "F_SETFD"), (3, "F_GETFL"),
                                (4, "F_SETFL"), (1030, "F_DUPFD_CLOEXEC"))),
        _errno.EPERM, "owner・signal・lease・notify・lock を拒否する"),
    ConditionalRule(
        "ioctl", 16,
        ArgInSet(arg=1, values=((0x5401, "TCGETS"), (0x802C542A, "TCGETS2"),
                                (0x5413, "TIOCGWINSZ"))),
        _errno.ENOTTY, "端末属性の読取だけ"),
)

#: `allow/6` の表。変更したら `PROFILE_VERSION` と worker の profile 版を上げる。
ALLOW6_TABLE = FilterTable(
    allow=_ALLOW6,
    named_errno=_NAMED_ERRNO6,
    fallback_enosys=_FALLBACK6,
    conditional=_CONDITIONAL6,
    # x86_64 の番号表の末尾 (listns = 470) の次
    table_end=471,
    default_action=RET_KILL_PROCESS,
)


def profile_id() -> str:
    """適用される filter の profile 名と版 (`allow/6`)。"""
    return SECCOMP_PROFILE


def seccomp_filters_field(status: bytes) -> bytes | None:
    """`/proc/<pid>/status` の内容から `Seccomp_filters:` の値 (strip 済み) を返す。
    field が無ければ `None`。親の継承 filter preflight と worker の再検査が同じ解釈を
    使うための 1 箇所。"""
    for line in status.split(b"\n"):
        if line.startswith(b"Seccomp_filters:"):
            return line.split(b":", 1)[1].strip()
    return None


# --- 命令列の生成 ------------------------------------------------------------

Instruction = tuple[int, int, int, int]


class FilterBuildError(ValueError):
    """表から有効な filter を作れない (飛び先の範囲外、命令数超過、番号の重複)。"""


def _condition_block(rule: ConditionalRule, entry: str, pid: int, deny: str) -> list:
    c = rule.condition
    if isinstance(c, FlagsThreadOnly):
        return [(entry, BPF_LD_W_ABS, 0, 0, _arg_lo(c.arg)),
                (None, BPF_ALU_AND_K, 0, 0, c.required),
                (None, BPF_JEQ_K, 0, deny, c.required),
                (None, BPF_LD_W_ABS, 0, 0, _arg_lo(c.arg)),
                (None, BPF_JSET_K, deny, "allow", c.forbidden)]
    if isinstance(c, SelfPidNotSignal):
        # pid の上位 32 bit が 0 (負の pid = process group や -1 を外す) かつ下位が自 pid
        return [(entry, BPF_LD_W_ABS, 0, 0, _arg_hi(c.pid_arg)),
                (None, BPF_JEQ_K, 0, deny, 0),
                (None, BPF_LD_W_ABS, 0, 0, _arg_lo(c.pid_arg)),
                (None, BPF_JEQ_K, 0, deny, pid),
                (None, BPF_LD_W_ABS, 0, 0, _arg_lo(c.signal_arg)),
                (None, BPF_JEQ_K, deny, "allow", c.denied_signal)]
    if isinstance(c, PidZeroOrSelf):
        return [(entry, BPF_LD_W_ABS, 0, 0, _arg_hi(c.pid_arg)),
                (None, BPF_JEQ_K, 0, deny, 0),
                (None, BPF_LD_W_ABS, 0, 0, _arg_lo(c.pid_arg)),
                (None, BPF_JEQ_K, "allow", 0, 0),
                (None, BPF_JEQ_K, "allow", deny, pid)]
    if isinstance(c, ArgInSet):
        block = [(entry, BPF_LD_W_ABS, 0, 0, _arg_lo(c.arg))]
        block += [(None, BPF_JEQ_K, "allow", 0, value) for value, _name in c.values]
        block += [(None, BPF_RET_K, 0, 0, ret_errno(rule.deny_errno))]
        return block
    raise FilterBuildError(f"unknown condition type: {type(c).__name__}")


def _errno_label(err: int) -> str:
    return f"errno_{err}"


def _assemble(prog: list) -> tuple[Instruction, ...]:
    labels: dict[str, int] = {}
    for i, (label, *_rest) in enumerate(prog):
        if label is not None:
            if label in labels:
                raise FilterBuildError(f"duplicate label {label}")
            labels[label] = i
    def rel(target: int | str, at: int) -> int:
        if isinstance(target, str):
            return labels[target] - at - 1
        return target

    out: list[Instruction] = []
    for i, (_label, code, jt, jf, k) in enumerate(prog):
        jt_, jf_ = rel(jt, i), rel(jf, i)
        if not (0 <= jt_ <= _MAX_JUMP and 0 <= jf_ <= _MAX_JUMP):
            raise FilterBuildError(f"jump out of range at instruction {i}")
        out.append((code, jt_, jf_, k & 0xFFFFFFFF))
    if len(out) > MAX_INSTRUCTIONS:
        raise FilterBuildError(f"filter has {len(out)} instructions (> {MAX_INSTRUCTIONS})")
    return tuple(out)


def build_program(table: FilterTable, *, pid: int) -> tuple[Instruction, ...]:
    """表から BPF 命令列を作る。`pid` は kill / tgkill / prlimit64 の自 pid 条件に入る。"""
    nrs = ([r.nr for r in table.allow] + [r.nr for r in table.named_errno]
           + [r.nr for r in table.fallback_enosys] + [r.nr for r in table.conditional])
    if len(nrs) != len(set(nrs)):
        raise FilterBuildError("a syscall number appears in more than one row")
    if any(nr >= table.table_end for nr in nrs):
        raise FilterBuildError("a row lies beyond table_end")

    p: list = [
        (None, BPF_LD_W_ABS, 0, 0, _OFF_ARCH),
        (None, BPF_JEQ_K, 0, "kill", AUDIT_ARCH_X86_64),
        (None, BPF_LD_W_ABS, 0, 0, _OFF_NR),
        (None, BPF_JGE_K, "kill", 0, X32_SYSCALL_BIT),
        (None, BPF_JGE_K, "kill", 0, table.table_end),
    ]
    p += [(None, BPF_JEQ_K, "allow", 0, r.nr) for r in table.allow]
    p += [(None, BPF_JEQ_K, _errno_label(r.errno), 0, r.nr) for r in table.named_errno]
    p += [(None, BPF_JEQ_K, _errno_label(_errno.ENOSYS), 0, r.nr) for r in table.fallback_enosys]
    p += [(None, BPF_JEQ_K, f"cond_{r.name}", 0, r.nr) for r in table.conditional]
    p += [("default", BPF_RET_K, 0, 0, table.default_action)]

    for r in table.conditional:
        p += _condition_block(r, f"cond_{r.name}", pid, _errno_label(r.deny_errno))

    errnos = sorted({r.errno for r in table.named_errno}
                    | ({_errno.ENOSYS} if table.fallback_enosys else set())
                    | {r.deny_errno for r in table.conditional
                       if not isinstance(r.condition, ArgInSet)})
    p += [("allow", BPF_RET_K, 0, 0, RET_ALLOW)]
    p += [(_errno_label(e), BPF_RET_K, 0, 0, ret_errno(e)) for e in errnos]
    # 番号表の外と arch 違反の行き先。default とは別の命令にする
    p += [("kill", BPF_RET_K, 0, 0, RET_KILL_PROCESS)]
    return _assemble(p)


# --- 適用 --------------------------------------------------------------------

class SeccompError(Exception):
    """filter を適用できなかった。`reason` は固定の失敗名。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8),
                ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(_SockFilter))]


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def install_program(program: Sequence[Instruction], *, log: bool = True) -> None:
    """`no_new_privs` を立て、`program` を TSYNC (と LOG) で呼んだ process の全 thread に掛ける。

    取り消せない。使い捨ての子 process の中でだけ呼ぶ。
    """
    if platform.machine() != "x86_64":
        raise SeccompError("seccomp_failed", "x86_64 only")
    if not program or len(program) > MAX_INSTRUCTIONS:
        raise SeccompError("seccomp_failed", "bad program length")
    arr = (_SockFilter * len(program))(*[_SockFilter(*ins) for ins in program])
    fprog = _SockFprog(len(program), ctypes.cast(arr, ctypes.POINTER(_SockFilter)))
    libc = _libc()
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise SeccompError("no_new_privs_failed", os.strerror(ctypes.get_errno()))
    flags = SECCOMP_FILTER_FLAG_TSYNC | (SECCOMP_FILTER_FLAG_LOG if log else 0)
    rc = libc.syscall(ctypes.c_long(_SYS_SECCOMP), ctypes.c_uint(_SECCOMP_SET_MODE_FILTER),
                      ctypes.c_uint(flags), ctypes.byref(fprog))
    # TSYNC は同期できなかった thread の id を正の値で返す。0 以外は全部失敗
    if rc != 0:
        err = ctypes.get_errno() if rc < 0 else 0
        raise SeccompError("seccomp_failed", f"rc={rc} errno={err}")


@dataclass(frozen=True)
class AppliedSeccomp:
    profile: str
    instructions: int


def apply_allow6() -> AppliedSeccomp:
    """`allow/6` を呼んだ process に TSYNC | LOG で掛ける。自 pid は呼んだ時点の値。

    失敗は `SeccompError` (`reason` は `no_new_privs_failed` か `seccomp_failed`)。
    """
    try:
        program = build_program(ALLOW6_TABLE, pid=os.getpid())
    except FilterBuildError as exc:
        raise SeccompError("seccomp_failed", str(exc)) from exc
    install_program(program, log=True)
    return AppliedSeccomp(profile=SECCOMP_PROFILE, instructions=len(program))


# --- 利用可否の確認 (filter を掛けない) -----------------------------------------

@dataclass(frozen=True)
class SeccompSupport:
    """host の seccomp 対応。`reason` は使えない場合の固定名、使えれば None。"""
    reason: str | None
    tsync: bool
    log: bool
    kill_process: bool
    errno_action: bool

    @property
    def available(self) -> bool:
        return self.reason is None


def _flag_supported(libc: ctypes.CDLL, flags: int) -> bool:
    # filter に NULL を渡すと、kernel は flags を検査した後で copy に失敗し EFAULT を返す。
    # 未対応の flag なら EINVAL。filter は 1 本も入らない
    ctypes.set_errno(0)
    rc = libc.syscall(ctypes.c_long(_SYS_SECCOMP), ctypes.c_uint(_SECCOMP_SET_MODE_FILTER),
                      ctypes.c_uint(flags), ctypes.c_void_p(None))
    return rc == -1 and ctypes.get_errno() == _errno.EFAULT


def _action_available(libc: ctypes.CDLL, action: int) -> bool:
    value = ctypes.c_uint32(action)
    rc = libc.syscall(ctypes.c_long(_SYS_SECCOMP), ctypes.c_uint(_SECCOMP_GET_ACTION_AVAIL),
                      ctypes.c_uint(0), ctypes.byref(value))
    return rc == 0


def probe_support() -> SeccompSupport:
    """filter を掛けずに、`allow/6` の適用に要る機能があるかを測る。"""
    if platform.machine() != "x86_64":
        return SeccompSupport("arch_unsupported", False, False, False, False)
    try:
        libc = _libc()
        tsync = _flag_supported(libc, SECCOMP_FILTER_FLAG_TSYNC)
        log = _flag_supported(libc, SECCOMP_FILTER_FLAG_LOG)
        kill_process = _action_available(libc, RET_KILL_PROCESS)
        # GET_ACTION_AVAIL は data 部を持たない action 値だけを受け付ける
        errno_action = _action_available(libc, _RET_ERRNO_BASE)
    except (OSError, AttributeError):
        return SeccompSupport("seccomp_unavailable", False, False, False, False)
    ok = tsync and log and kill_process and errno_action
    return SeccompSupport(None if ok else "seccomp_unavailable", tsync, log, kill_process,
                          errno_action)


# --- kernel log 診断 ---------------------------------------------------------

KERNEL_LOG_READABLE = "kernel_log_readable"
KERNEL_LOG_PERMISSION = "kernel_log_unreadable:permission"
KERNEL_LOG_NO_JOURNALCTL = "kernel_log_unreadable:no_journalctl"
KERNEL_LOG_TIMEOUT = "kernel_log_unreadable:timeout"
KERNEL_LOG_EMPTY = "kernel_log_unreadable:empty"
ACTIONS_LOGGED_UNREADABLE = "seccomp_log_missing:actions_logged_unreadable"
ACTIONS_LOGGED_NO_KILL_PROCESS = "seccomp_log_missing:kill_process"
ACTIONS_LOGGED_NO_ERRNO = "seccomp_log_missing:errno"

KERNEL_LOG_CODES = frozenset({
    KERNEL_LOG_READABLE, KERNEL_LOG_PERMISSION, KERNEL_LOG_NO_JOURNALCTL, KERNEL_LOG_TIMEOUT,
    KERNEL_LOG_EMPTY, ACTIONS_LOGGED_UNREADABLE, ACTIONS_LOGGED_NO_KILL_PROCESS,
    ACTIONS_LOGGED_NO_ERRNO,
})

_ACTIONS_LOGGED_PATH = "/proc/sys/kernel/seccomp/actions_logged"


def classify_kernel_log(actions_logged: str | None, journalctl_path: str | None,
                        returncode: int | None, stdout: str, stderr: str) -> str:
    """kernel log で seccomp の記録を読めるかを固定 code にする。

    journalctl は権限が無くても rc 0 で本文が空になり Hint だけを出すことがあるので、
    rc ではなく本文の有無で決める。`returncode=None` は timeout を表す。
    """
    if actions_logged is None:
        return ACTIONS_LOGGED_UNREADABLE
    logged = set(actions_logged.split())
    if "kill_process" not in logged:
        return ACTIONS_LOGGED_NO_KILL_PROCESS
    if "errno" not in logged:
        return ACTIONS_LOGGED_NO_ERRNO
    if journalctl_path is None:
        return KERNEL_LOG_NO_JOURNALCTL
    if returncode is None:
        return KERNEL_LOG_TIMEOUT
    if stdout.strip():
        return KERNEL_LOG_READABLE
    lowered = stderr.lower()
    if "insufficient permissions" in lowered or "not seeing messages" in lowered:
        return KERNEL_LOG_PERMISSION
    return KERNEL_LOG_EMPTY


def check_kernel_log(*, timeout: float = 5.0,
                     actions_logged_path: str = _ACTIONS_LOGGED_PATH,
                     which: Callable[[str], str | None] = shutil.which,
                     run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """この host で seccomp の kernel log を読めるかを測る。分類には使わない診断専用。"""
    try:
        with open(actions_logged_path, encoding="ascii") as f:
            actions_logged: str | None = f.read()
    except OSError:
        actions_logged = None
    journalctl = which("journalctl")
    returncode: int | None = None
    stdout = stderr = ""
    if journalctl is not None:
        try:
            done = run([journalctl, "-k", "-n", "1", "--no-pager", "-o", "cat"],
                       capture_output=True, text=True, timeout=timeout)
            returncode, stdout, stderr = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired:
            returncode = None
        except OSError:
            journalctl = None
    return classify_kernel_log(actions_logged, journalctl, returncode, stdout, stderr)


def sigsys_diagnostic_line(pid: int) -> str:
    """SIGSYS で終わった worker について運用者に出す固定 1 行。"""
    return (f"plugin worker pid={int(pid)} was killed by SIGSYS; "
            f"see: journalctl -k -g 'type=1326.*pid={int(pid)} '")


__all__ = [
    "ALLOW6_TABLE",
    "KERNEL_LOG_CODES",
    "PROFILE_NAME",
    "PROFILE_VERSION",
    "RET_ALLOW",
    "RET_KILL_PROCESS",
    "SECCOMP_PROFILE",
    "AllowRule",
    "AppliedSeccomp",
    "ArgInSet",
    "ConditionalRule",
    "ErrnoRule",
    "FallbackRule",
    "FilterBuildError",
    "FilterTable",
    "FlagsThreadOnly",
    "PidZeroOrSelf",
    "SeccompError",
    "SeccompSupport",
    "SelfPidNotSignal",
    "apply_allow6",
    "build_program",
    "check_kernel_log",
    "classify_kernel_log",
    "install_program",
    "probe_support",
    "seccomp_filters_field",
    "ret_errno",
    "sigsys_diagnostic_line",
]
