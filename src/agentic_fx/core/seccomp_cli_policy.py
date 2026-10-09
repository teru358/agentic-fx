"""改善 mission の CLI 部分木に掛ける seccomp `improve-cli/1` の表 (x86_64 専用)。

x86_64 の syscall 番号 0〜471 の一つ一つに action を一つ与える。未割当の番号 (hole)
も ENOSYS として明示する。ceiling (472) 以上の番号は表を引かず ENOSYS、x86_64 以外の
arch と x32 ABI は KILL_PROCESS。

評価順:

1. arch が x86_64 でない、または x32 (`nr >= 0x40000000`) → KILL_PROCESS
2. `nr >= CEILING` → ERRNO(ENOSYS)
3. 0〜471 → 表の action (ALLOW / COND / NOTIF / EPERM / EACCES / ENOSYS)

COND は引数の判定をデータ (`CONDITIONS`) で持ち、data evaluator と BPF 生成器
(`seccomp_cli_bpf`) の両方がこのデータだけに従う。番号 → 名前の対応は header から
取った snapshot をこの module に同梱し、実行時に header を読まない (header との照合は
テストで行う)。

policy hash は canonical text (行 `nr\\tname\\taction\\tcondition_id` の 472 行と、
条件定義の行) の SHA-256。条件の判定値・errno・引数位置のどれを変えても hash が変わる。
"""
from __future__ import annotations

import errno as _errno
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

from agentic_fx.core.seccomp import (
    AUDIT_ARCH_X86_64,
    RET_ALLOW,
    RET_KILL_PROCESS,
    X32_SYSCALL_BIT,
    ret_errno,
)

PROFILE_NAME = "improve-cli"
PROFILE_VERSION = 1
PROFILE_ID = f"{PROFILE_NAME}/{PROFILE_VERSION}"

# 表が明示分類する番号の上限 (header の最大番号 + 1 と一致しなければ生成失敗)
CEILING = 472

RET_USER_NOTIF = 0x7FC00000

SECCOMP_FILTER_FLAG_TSYNC = 1 << 0
SECCOMP_FILTER_FLAG_LOG = 1 << 1
SECCOMP_FILTER_FLAG_NEW_LISTENER = 1 << 3
SECCOMP_FILTER_FLAG_TSYNC_ESRCH = 1 << 4
SECCOMP_FILTER_FLAG_WAIT_KILLABLE_RECV = 1 << 5
# LOG は含めない: 拒否の連打で audit log を膨らませないため。TSYNC と NEW_LISTENER を
# 併用するには TSYNC_ESRCH が要る。WAIT_KILLABLE_RECV は signal 下の通知 livelock を防ぐ
FILTER_FLAGS = (SECCOMP_FILTER_FLAG_TSYNC | SECCOMP_FILTER_FLAG_NEW_LISTENER
                | SECCOMP_FILTER_FLAG_TSYNC_ESRCH | SECCOMP_FILTER_FLAG_WAIT_KILLABLE_RECV)

ACTIONS = ("ALLOW", "COND", "NOTIF", "EPERM", "EACCES", "ENOSYS")
# 条件の結果に使う名前と戻り値
RESULT_ERRNO = {
    "EPERM": _errno.EPERM, "EACCES": _errno.EACCES, "ENOSYS": _errno.ENOSYS,
    "EINVAL": _errno.EINVAL, "ENOTTY": _errno.ENOTTY, "EAFNOSUPPORT": _errno.EAFNOSUPPORT,
}


def result_value(result: str) -> int:
    """action / 条件結果の名前 → seccomp の戻り値。"""
    if result == "ALLOW":
        return RET_ALLOW
    if result == "NOTIF":
        return RET_USER_NOTIF
    return ret_errno(RESULT_ERRNO[result])


class PolicyBuildError(ValueError):
    """表または BPF を作れない。`reason` は固定の失敗名。"""

    REASONS = frozenset({
        "unclassified", "duplicate", "unknown_name", "number_collision", "ceiling_mismatch",
        "condition_mismatch", "bad_condition", "too_many_instructions", "jump_out_of_range",
        "invalid_name", "empty_program", "bad_opcode", "bad_field", "bad_load_offset",
        "no_final_return", "unreachable_instruction", "number_out_of_range",
    })

    def __init__(self, reason: str, detail: str = "") -> None:
        assert reason in self.REASONS, reason
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason


# --- 番号 snapshot (asm/unistd_64.h、`nr:name`) ------------------------------
_SNAPSHOT = """
0:read 1:write 2:open 3:close 4:stat 5:fstat 6:lstat 7:poll 8:lseek 9:mmap 10:mprotect
11:munmap 12:brk 13:rt_sigaction 14:rt_sigprocmask 15:rt_sigreturn 16:ioctl 17:pread64
18:pwrite64 19:readv 20:writev 21:access 22:pipe 23:select 24:sched_yield 25:mremap 26:msync
27:mincore 28:madvise 29:shmget 30:shmat 31:shmctl 32:dup 33:dup2 34:pause 35:nanosleep
36:getitimer 37:alarm 38:setitimer 39:getpid 40:sendfile 41:socket 42:connect 43:accept
44:sendto 45:recvfrom 46:sendmsg 47:recvmsg 48:shutdown 49:bind 50:listen 51:getsockname
52:getpeername 53:socketpair 54:setsockopt 55:getsockopt 56:clone 57:fork 58:vfork 59:execve
60:exit 61:wait4 62:kill 63:uname 64:semget 65:semop 66:semctl 67:shmdt 68:msgget 69:msgsnd
70:msgrcv 71:msgctl 72:fcntl 73:flock 74:fsync 75:fdatasync 76:truncate 77:ftruncate
78:getdents 79:getcwd 80:chdir 81:fchdir 82:rename 83:mkdir 84:rmdir 85:creat 86:link
87:unlink 88:symlink 89:readlink 90:chmod 91:fchmod 92:chown 93:fchown 94:lchown 95:umask
96:gettimeofday 97:getrlimit 98:getrusage 99:sysinfo 100:times 101:ptrace 102:getuid
103:syslog 104:getgid 105:setuid 106:setgid 107:geteuid 108:getegid 109:setpgid 110:getppid
111:getpgrp 112:setsid 113:setreuid 114:setregid 115:getgroups 116:setgroups 117:setresuid
118:getresuid 119:setresgid 120:getresgid 121:getpgid 122:setfsuid 123:setfsgid 124:getsid
125:capget 126:capset 127:rt_sigpending 128:rt_sigtimedwait 129:rt_sigqueueinfo
130:rt_sigsuspend 131:sigaltstack 132:utime 133:mknod 134:uselib 135:personality 136:ustat
137:statfs 138:fstatfs 139:sysfs 140:getpriority 141:setpriority 142:sched_setparam
143:sched_getparam 144:sched_setscheduler 145:sched_getscheduler 146:sched_get_priority_max
147:sched_get_priority_min 148:sched_rr_get_interval 149:mlock 150:munlock 151:mlockall
152:munlockall 153:vhangup 154:modify_ldt 155:pivot_root 156:_sysctl 157:prctl
158:arch_prctl 159:adjtimex 160:setrlimit 161:chroot 162:sync 163:acct 164:settimeofday
165:mount 166:umount2 167:swapon 168:swapoff 169:reboot 170:sethostname 171:setdomainname
172:iopl 173:ioperm 174:create_module 175:init_module 176:delete_module 177:get_kernel_syms
178:query_module 179:quotactl 180:nfsservctl 181:getpmsg 182:putpmsg 183:afs_syscall
184:tuxcall 185:security 186:gettid 187:readahead 188:setxattr 189:lsetxattr 190:fsetxattr
191:getxattr 192:lgetxattr 193:fgetxattr 194:listxattr 195:llistxattr 196:flistxattr
197:removexattr 198:lremovexattr 199:fremovexattr 200:tkill 201:time 202:futex
203:sched_setaffinity 204:sched_getaffinity 205:set_thread_area 206:io_setup 207:io_destroy
208:io_getevents 209:io_submit 210:io_cancel 211:get_thread_area 212:lookup_dcookie
213:epoll_create 214:epoll_ctl_old 215:epoll_wait_old 216:remap_file_pages 217:getdents64
218:set_tid_address 219:restart_syscall 220:semtimedop 221:fadvise64 222:timer_create
223:timer_settime 224:timer_gettime 225:timer_getoverrun 226:timer_delete 227:clock_settime
228:clock_gettime 229:clock_getres 230:clock_nanosleep 231:exit_group 232:epoll_wait
233:epoll_ctl 234:tgkill 235:utimes 236:vserver 237:mbind 238:set_mempolicy
239:get_mempolicy 240:mq_open 241:mq_unlink 242:mq_timedsend 243:mq_timedreceive
244:mq_notify 245:mq_getsetattr 246:kexec_load 247:waitid 248:add_key 249:request_key
250:keyctl 251:ioprio_set 252:ioprio_get 253:inotify_init 254:inotify_add_watch
255:inotify_rm_watch 256:migrate_pages 257:openat 258:mkdirat 259:mknodat 260:fchownat
261:futimesat 262:newfstatat 263:unlinkat 264:renameat 265:linkat 266:symlinkat
267:readlinkat 268:fchmodat 269:faccessat 270:pselect6 271:ppoll 272:unshare
273:set_robust_list 274:get_robust_list 275:splice 276:tee 277:sync_file_range 278:vmsplice
279:move_pages 280:utimensat 281:epoll_pwait 282:signalfd 283:timerfd_create 284:eventfd
285:fallocate 286:timerfd_settime 287:timerfd_gettime 288:accept4 289:signalfd4 290:eventfd2
291:epoll_create1 292:dup3 293:pipe2 294:inotify_init1 295:preadv 296:pwritev
297:rt_tgsigqueueinfo 298:perf_event_open 299:recvmmsg 300:fanotify_init 301:fanotify_mark
302:prlimit64 303:name_to_handle_at 304:open_by_handle_at 305:clock_adjtime 306:syncfs
307:sendmmsg 308:setns 309:getcpu 310:process_vm_readv 311:process_vm_writev 312:kcmp
313:finit_module 314:sched_setattr 315:sched_getattr 316:renameat2 317:seccomp 318:getrandom
319:memfd_create 320:kexec_file_load 321:bpf 322:execveat 323:userfaultfd 324:membarrier
325:mlock2 326:copy_file_range 327:preadv2 328:pwritev2 329:pkey_mprotect 330:pkey_alloc
331:pkey_free 332:statx 333:io_pgetevents 334:rseq 335:uretprobe 336:uprobe
424:pidfd_send_signal 425:io_uring_setup 426:io_uring_enter 427:io_uring_register
428:open_tree 429:move_mount 430:fsopen 431:fsconfig 432:fsmount 433:fspick 434:pidfd_open
435:clone3 436:close_range 437:openat2 438:pidfd_getfd 439:faccessat2 440:process_madvise
441:epoll_pwait2 442:mount_setattr 443:quotactl_fd 444:landlock_create_ruleset
445:landlock_add_rule 446:landlock_restrict_self 447:memfd_secret 448:process_mrelease
449:futex_waitv 450:set_mempolicy_home_node 451:cachestat 452:fchmodat2 453:map_shadow_stack
454:futex_wake 455:futex_wait 456:futex_requeue 457:statmount 458:listmount
459:lsm_get_self_attr 460:lsm_set_self_attr 461:lsm_list_modules 462:mseal 463:setxattrat
464:getxattrat 465:listxattrat 466:removexattrat 467:open_tree_attr 468:file_getattr
469:file_setattr 470:listns 471:rseq_slice_yield
"""


def numbers_from_header(text: str) -> dict[str, int]:
    """`asm/unistd_64.h` 形式の text から名前 → 番号を取る。"""
    return {m.group(1): int(m.group(2))
            for m in re.finditer(r"^#define __NR_(\w+)\s+(\d+)\s*$", text, re.M)}


def _parse_snapshot(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in text.split():
        nr, name = item.split(":")
        out[name] = int(nr)
    return out


SNAPSHOT_NUMBERS: Mapping[str, int] = _parse_snapshot(_SNAPSHOT)


# --- 条件の定数 ---------------------------------------------------------------

CONSTANTS: Mapping[str, int] = {
    "AF_UNIX": 1, "AF_INET": 2, "AF_INET6": 10,
    "SOCK_STREAM": 1, "SOCK_TYPE_MASK": 0xF, "IPPROTO_IP": 0, "IPPROTO_TCP": 6,
    "MSG_FASTOPEN": 0x20000000,
    "CLONE_NEWTIME": 0x80, "CLONE_NEWNS": 0x20000, "CLONE_PARENT": 0x8000,
    "CLONE_NEWCGROUP": 0x02000000, "CLONE_NEWUTS": 0x04000000, "CLONE_NEWIPC": 0x08000000,
    "CLONE_NEWUSER": 0x10000000, "CLONE_NEWPID": 0x20000000, "CLONE_NEWNET": 0x40000000,
    "MEMBARRIER_CMD_QUERY": 0,
    "MEMBARRIER_CMD_PRIVATE_EXPEDITED": 1 << 3,
    "MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED": 1 << 4,
    "MEMBARRIER_CMD_PRIVATE_EXPEDITED_SYNC_CORE": 1 << 5,
    "MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED_SYNC_CORE": 1 << 6,
    "MEMBARRIER_CMD_PRIVATE_EXPEDITED_RSEQ": 1 << 7,
    "MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED_RSEQ": 1 << 8,
    "PER_QUERY": 0xFFFFFFFF,
    "PR_SET_PDEATHSIG": 1, "PR_GET_PDEATHSIG": 2, "PR_GET_DUMPABLE": 3,
    "PR_SET_NAME": 15, "PR_GET_NAME": 16, "PR_GET_SECCOMP": 21, "PR_CAPBSET_READ": 23,
    "PR_GET_CHILD_SUBREAPER": 37, "PR_SET_NO_NEW_PRIVS": 38, "PR_GET_NO_NEW_PRIVS": 39,
    "PR_SET_THP_DISABLE": 41, "PR_GET_THP_DISABLE": 42,
    "PR_SET_VMA": 0x53564D41, "PR_SET_VMA_ANON_NAME": 0,
    "F_DUPFD": 0, "F_GETFD": 1, "F_SETFD": 2, "F_GETFL": 3, "F_SETFL": 4, "F_GETLK": 5,
    "F_SETLK": 6, "F_DUPFD_CLOEXEC": 1030, "F_GETPIPE_SZ": 1032, "F_ADD_SEALS": 1033,
    "F_GET_SEALS": 1034,
    "TCGETS": 0x5401, "TIOCGPGRP": 0x540F, "TIOCGWINSZ": 0x5413, "FIONREAD": 0x541B,
    "FIONBIO": 0x5421, "TCGETS2": 0x802C542A, "TIOCGPTN": 0x80045430, "FIONCLEX": 0x5450,
    "FIOCLEX": 0x5451, "FICLONE": 0x40049409, "FICLONERANGE": 0x4020940D,
}

# clone で拒否する bit: 全 namespace flag と CLONE_PARENT (CLONE_PARENT_SETTID は許す)
CLONE_FORBIDDEN_MASK = (
    CONSTANTS["CLONE_NEWTIME"] | CONSTANTS["CLONE_NEWNS"] | CONSTANTS["CLONE_PARENT"]
    | CONSTANTS["CLONE_NEWCGROUP"] | CONSTANTS["CLONE_NEWUTS"] | CONSTANTS["CLONE_NEWIPC"]
    | CONSTANTS["CLONE_NEWUSER"] | CONSTANTS["CLONE_NEWPID"] | CONSTANTS["CLONE_NEWNET"])


def named_condition_values() -> dict[str, int]:
    """条件に使う名前つき定数 (header との照合用)。"""
    return {**CONSTANTS, "CLONE_FORBIDDEN_MASK": CLONE_FORBIDDEN_MASK}


# --- 条件のデータ -------------------------------------------------------------

_U32 = 0xFFFFFFFF
_U64 = 0xFFFFFFFFFFFFFFFF
_OPS = ("in", "masked_in", "no_bits")
_WIDTHS = ("lo32", "u64")


@dataclass(frozen=True)
class ArgTest:
    """引数 1 個の判定。

    width: `lo32` は下位 32 bit だけを見る (int 型の引数。kernel も切り詰める)。
    `u64` は上位 32 bit が 0 かつ下位が値に一致 (pid と pointer。符号拡張した負値を外す)。
    op: `in` は値の集合、`masked_in` は mask との AND が集合、`no_bits` は mask の bit が
    一つも立っていない。
    """

    arg: int
    op: str
    width: str = "lo32"
    values: tuple[tuple[str, int], ...] = ()
    mask: tuple[str, int] | None = None

    def holds(self, args: Sequence[int]) -> bool:
        v = args[self.arg] & _U64
        lo = v & _U32
        if self.width == "u64" and v >> 32:
            return False
        if self.op == "in":
            return any(lo == x for _n, x in self.values)
        assert self.mask is not None
        if self.op == "masked_in":
            return any(lo & self.mask[1] == x for _n, x in self.values)
        return lo & self.mask[1] == 0

    def canonical(self) -> dict:
        return {"arg": self.arg, "op": self.op, "width": self.width,
                "values": [[n, x] for n, x in self.values],
                "mask": None if self.mask is None else [self.mask[0], self.mask[1]]}


@dataclass(frozen=True)
class Branch:
    """`test` が成り立てば `then`、成り立たなければ `otherwise`。葉は結果の名前
    (ALLOW か errno 名)。同じ引数を規則ごとに読み直さないよう、判定は木で持つ。
    """

    test: ArgTest
    then: Branch | str
    otherwise: Branch | str


def _walk(node: Branch | str, args: Sequence[int]) -> str:
    while isinstance(node, Branch):
        node = node.then if node.test.holds(args) else node.otherwise
    return node


def _canonical_node(node: Branch | str) -> object:
    if isinstance(node, str):
        return node
    return {"test": node.test.canonical(), "then": _canonical_node(node.then),
            "else": _canonical_node(node.otherwise)}


@dataclass(frozen=True)
class Condition:
    """条件つき syscall 1 個の引数判定。"""

    condition_id: str
    decision: Branch

    def evaluate(self, args: Sequence[int]) -> str:
        return _walk(self.decision, args)

    def canonical(self) -> str:
        return json.dumps(_canonical_node(self.decision), sort_keys=True, separators=(",", ":"))


def _named(*names: str) -> tuple[tuple[str, int], ...]:
    return tuple((n, CONSTANTS[n]) for n in names)


def _in(arg: int, *names: str, width: str = "lo32") -> ArgTest:
    return ArgTest(arg=arg, op="in", width=width, values=_named(*names))


def _no_bits(arg: int, mask_name: str, mask: int) -> ArgTest:
    return ArgTest(arg=arg, op="no_bits", mask=(mask_name, mask))


def _fastopen(arg: int) -> ArgTest:
    return _no_bits(arg, "MSG_FASTOPEN", CONSTANTS["MSG_FASTOPEN"])


CONDITIONS: Mapping[str, Condition] = {
    # AF_INET/6 の TCP stream だけ。AF_UNIX と INET の他 type は EACCES、他 family は EAFNOSUPPORT
    "socket": Condition("socket-stream-v1", Branch(
        _in(0, "AF_INET", "AF_INET6"),
        Branch(ArgTest(arg=1, op="masked_in", values=_named("SOCK_STREAM"),
                       mask=("SOCK_TYPE_MASK", CONSTANTS["SOCK_TYPE_MASK"])),
               Branch(_in(2, "IPPROTO_IP", "IPPROTO_TCP"), "ALLOW", "EACCES"),
               "EACCES"),
        Branch(_in(0, "AF_UNIX"), "EACCES", "EAFNOSUPPORT"))),
    "socketpair": Condition("socketpair-unix-v1", Branch(
        _in(0, "AF_UNIX"), "ALLOW", "EAFNOSUPPORT")),
    "clone": Condition("clone-no-namespace-v1", Branch(
        _no_bits(0, "CLONE_FORBIDDEN_MASK", CLONE_FORBIDDEN_MASK), "ALLOW", "EPERM")),
    "prlimit64": Condition("prlimit64-self-v1", Branch(
        ArgTest(arg=0, op="in", width="u64", values=(("self", 0),)), "ALLOW", "EPERM")),
    "membarrier": Condition("membarrier-private-v1", Branch(
        _in(0, "MEMBARRIER_CMD_QUERY", "MEMBARRIER_CMD_PRIVATE_EXPEDITED",
            "MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED",
            "MEMBARRIER_CMD_PRIVATE_EXPEDITED_SYNC_CORE",
            "MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED_SYNC_CORE",
            "MEMBARRIER_CMD_PRIVATE_EXPEDITED_RSEQ",
            "MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED_RSEQ"),
        "ALLOW", "EPERM")),
    "personality": Condition("personality-query-v1", Branch(
        _in(0, "PER_QUERY"), "ALLOW", "EPERM")),
    "sendto": Condition("sendto-no-fastopen-v1", Branch(
        _fastopen(3),
        Branch(ArgTest(arg=4, op="in", width="u64", values=(("NULL", 0),)), "ALLOW", "EPERM"),
        "EPERM")),
    "sendmsg": Condition("sendmsg-no-fastopen-v1", Branch(_fastopen(2), "ALLOW", "EPERM")),
    "sendmmsg": Condition("sendmmsg-no-fastopen-v1", Branch(_fastopen(3), "ALLOW", "EPERM")),
    "prctl": Condition("prctl-allowlist-v1", Branch(
        _in(0, "PR_SET_NAME", "PR_GET_NAME", "PR_GET_THP_DISABLE", "PR_SET_THP_DISABLE",
            "PR_SET_PDEATHSIG", "PR_GET_PDEATHSIG", "PR_SET_NO_NEW_PRIVS", "PR_GET_NO_NEW_PRIVS",
            "PR_GET_SECCOMP", "PR_CAPBSET_READ", "PR_GET_DUMPABLE", "PR_GET_CHILD_SUBREAPER"),
        "ALLOW",
        # PR_SET_VMA は匿名 mapping の名前付けだけ (第 2 引数は unsigned long)
        Branch(_in(0, "PR_SET_VMA"),
               Branch(_in(1, "PR_SET_VMA_ANON_NAME", width="u64"), "ALLOW", "EINVAL"),
               "EINVAL"))),
    "fcntl": Condition("fcntl-allowlist-v1", Branch(
        _in(1, "F_DUPFD", "F_DUPFD_CLOEXEC", "F_GETFD", "F_SETFD", "F_GETFL", "F_SETFL",
            "F_GETLK", "F_SETLK", "F_GET_SEALS", "F_ADD_SEALS", "F_GETPIPE_SZ"),
        "ALLOW", "EINVAL")),
    "ioctl": Condition("ioctl-allowlist-v1", Branch(
        _in(1, "TCGETS", "TCGETS2", "TIOCGWINSZ", "TIOCGPGRP", "TIOCGPTN", "FIONBIO",
            "FIONREAD", "FIOCLEX", "FIONCLEX", "FICLONE", "FICLONERANGE"),
        "ALLOW", "ENOTTY")),
}


# --- 分類 (名前 → action) -----------------------------------------------------
# ALLOW: 自 process に閉じる操作、Landlock が判定する path 操作、自分の fd / socket の I/O、
# process 生成と exec、nested Landlock が範囲を絞る signal、制限を足すだけの seccomp / landlock。
# EPERM: 他 process・資源・kernel 状態への作用、metadata の変更 (path 形・fd 形とも)、
# POSIX timer、listen。EACCES: perf_event_open。ENOSYS: clone3 (glibc が clone へ戻る)、
# keyring、廃止・未実装の番号。


_ALLOW = frozenset("""
read write open close stat fstat lstat poll lseek mmap mprotect munmap brk rt_sigaction
rt_sigprocmask rt_sigreturn pread64 pwrite64 readv writev access pipe select sched_yield
mremap msync mincore madvise dup dup2 pause nanosleep getitimer alarm setitimer getpid
sendfile accept recvfrom recvmsg shutdown bind getsockname getpeername setsockopt
getsockopt fork vfork execve exit wait4 kill uname flock fsync fdatasync truncate
ftruncate getdents getcwd chdir fchdir rename mkdir rmdir creat link unlink symlink
readlink umask gettimeofday getrlimit getrusage sysinfo times getuid getgid geteuid
getegid setpgid getppid getpgrp setsid getgroups getresuid getresgid getpgid getsid
capget rt_sigpending rt_sigtimedwait rt_sigqueueinfo rt_sigsuspend sigaltstack statfs
fstatfs getpriority sched_getparam sched_getscheduler sched_get_priority_max
sched_get_priority_min sched_rr_get_interval munlock munlockall arch_prctl setrlimit
gettid readahead getxattr lgetxattr fgetxattr listxattr llistxattr flistxattr tkill time
futex sched_getaffinity epoll_create getdents64 set_tid_address restart_syscall
fadvise64 clock_gettime clock_getres clock_nanosleep exit_group epoll_wait epoll_ctl
tgkill mbind set_mempolicy get_mempolicy waitid ioprio_get inotify_init
inotify_add_watch inotify_rm_watch openat mkdirat newfstatat unlinkat renameat linkat
symlinkat readlinkat faccessat pselect6 ppoll set_robust_list splice tee sync_file_range
vmsplice epoll_pwait signalfd timerfd_create eventfd fallocate timerfd_settime
timerfd_gettime accept4 signalfd4 eventfd2 epoll_create1 dup3 pipe2 inotify_init1 preadv
pwritev rt_tgsigqueueinfo recvmmsg getcpu sched_getattr renameat2 seccomp getrandom
memfd_create execveat copy_file_range preadv2 pwritev2 statx rseq pidfd_send_signal
pidfd_open close_range openat2 faccessat2 epoll_pwait2 landlock_create_ruleset
landlock_add_rule landlock_restrict_self futex_waitv futex_wake futex_wait futex_requeue
getxattrat listxattrat file_getattr rseq_slice_yield
""".split())

_EPERM = frozenset("""
shmget shmat shmctl listen semget semop semctl shmdt msgget msgsnd msgrcv msgctl chmod
fchmod chown fchown lchown ptrace syslog setuid setgid setreuid setregid setgroups
setresuid setresgid setfsuid setfsgid capset utime mknod uselib ustat sysfs setpriority
sched_setparam sched_setscheduler mlock mlockall vhangup modify_ldt pivot_root _sysctl
adjtimex chroot sync acct settimeofday mount umount2 swapon swapoff reboot sethostname
setdomainname iopl ioperm init_module delete_module quotactl nfsservctl setxattr
lsetxattr fsetxattr removexattr lremovexattr fremovexattr sched_setaffinity io_setup
io_destroy io_getevents io_submit io_cancel lookup_dcookie remap_file_pages semtimedop
timer_create timer_settime timer_gettime timer_getoverrun timer_delete clock_settime
utimes mq_open mq_unlink mq_timedsend mq_timedreceive mq_notify mq_getsetattr kexec_load
ioprio_set migrate_pages mknodat fchownat futimesat fchmodat unshare get_robust_list
move_pages utimensat fanotify_init fanotify_mark name_to_handle_at open_by_handle_at
clock_adjtime syncfs setns process_vm_readv process_vm_writev kcmp finit_module
sched_setattr kexec_file_load bpf userfaultfd mlock2 pkey_mprotect pkey_alloc pkey_free
io_pgetevents io_uring_setup io_uring_enter io_uring_register open_tree move_mount
fsopen fsconfig fsmount fspick pidfd_getfd process_madvise mount_setattr quotactl_fd
memfd_secret process_mrelease set_mempolicy_home_node cachestat fchmodat2
map_shadow_stack statmount listmount lsm_get_self_attr lsm_set_self_attr
lsm_list_modules mseal setxattrat removexattrat open_tree_attr file_setattr listns
""".split())

_EACCES = frozenset("""
perf_event_open
""".split())

_ENOSYS = frozenset("""
create_module get_kernel_syms query_module getpmsg putpmsg afs_syscall tuxcall security
set_thread_area get_thread_area epoll_ctl_old epoll_wait_old vserver add_key request_key
keyctl uretprobe uprobe clone3
""".split())

_COND = frozenset(CONDITIONS)
_NOTIF = frozenset({"connect"})

CLASSIFICATION: Mapping[str, frozenset[str]] = {
    "ALLOW": _ALLOW, "COND": _COND, "NOTIF": _NOTIF, "EPERM": _EPERM, "EACCES": _EACCES,
    "ENOSYS": _ENOSYS,
}


# --- 表 ----------------------------------------------------------------------

@dataclass(frozen=True)
class Row:
    nr: int
    name: str | None  # None は未割当 (hole)
    action: str
    condition_id: str | None


@dataclass(frozen=True)
class PolicyTable:
    rows: tuple[Row, ...]
    conditions: Mapping[str, Condition]
    ceiling: int
    canonical_text: str
    policy_hash: str

    def counts(self) -> dict[str, int]:
        c = Counter(r.action for r in self.rows)
        return {a: c.get(a, 0) for a in ACTIONS}


def _canonical_text(rows: Iterable[Row], conditions: Mapping[str, Condition]) -> str:
    lines = [f"{r.nr}\t{r.name or '-'}\t{r.action}\t{r.condition_id or '-'}" for r in rows]
    lines += [f"cond\t{name}\t{conditions[name].condition_id}\t{conditions[name].canonical()}"
              for name in sorted(conditions)]
    return "".join(line + "\n" for line in lines)


def _check_condition(name: str, cond: Condition) -> None:
    def bad(msg: str) -> PolicyBuildError:
        return PolicyBuildError("bad_condition", f"{name}: {msg}")

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*-v\d+", cond.condition_id):
        raise bad(f"condition id {cond.condition_id!r}")
    if not isinstance(cond.decision, Branch):
        raise bad("decision must start with a test")
    results: set[str] = set()

    def visit(node: Branch | str) -> None:
        if isinstance(node, str):
            if node != "ALLOW" and node not in RESULT_ERRNO:
                raise bad(f"result {node!r}")
            results.add(node)
            return
        t = node.test
        if not 0 <= t.arg <= 5 or t.op not in _OPS or t.width not in _WIDTHS:
            raise bad(f"test {t}")
        if t.op == "in" and (not t.values or t.mask is not None):
            raise bad(f"test {t}")
        if t.op == "masked_in" and (not t.values or t.mask is None or t.width != "lo32"):
            raise bad(f"test {t}")
        if t.op == "no_bits" and (t.values or t.mask is None or t.width != "lo32"):
            raise bad(f"test {t}")
        if any(not 0 <= x <= _U32 for _n, x in t.values):
            raise bad(f"value out of 32 bit range in {t}")
        if t.mask is not None and not 0 < t.mask[1] <= _U32:
            raise bad(f"mask out of range in {t}")
        visit(node.then)
        visit(node.otherwise)

    visit(cond.decision)
    if "ALLOW" not in results or len(results) < 2:
        raise bad("a condition must both allow and deny")


def build_table(*, classification: Mapping[str, Iterable[str]] = CLASSIFICATION,
                conditions: Mapping[str, Condition] = CONDITIONS,
                numbers: Mapping[str, int] = SNAPSHOT_NUMBERS) -> PolicyTable:
    """分類と番号から全番号表を作る。未分類・重複・未知の名前・番号の範囲外・ceiling 不一致は生成失敗。"""
    ceiling = CEILING
    # 正準 text は ASCII でしか符号化できない。符号化の例外を外に出さず生成失敗にする
    names = set(numbers) | {n for members in classification.values() for n in members}
    bad_names = sorted(n for n in names if not n.isascii())
    if bad_names:
        raise PolicyBuildError("invalid_name", ", ".join(ascii(n) for n in bad_names))
    # 負の番号は `range(ceiling)` の走査に掛からず、分類済みでも表から黙って落ちる
    negative = sorted(f"{n}={nr}" for n, nr in numbers.items() if nr < 0)
    if negative:
        raise PolicyBuildError("number_out_of_range", ", ".join(negative))
    by_nr: dict[int, str] = {}
    for name, nr in sorted(numbers.items()):
        if nr in by_nr:
            raise PolicyBuildError("number_collision", f"{by_nr[nr]} / {name} = {nr}")
        by_nr[nr] = name
    sets = {a: frozenset(classification.get(a, ())) for a in ACTIONS}
    extra = set(classification) - set(ACTIONS)
    if extra:
        raise PolicyBuildError("unknown_name", f"actions {sorted(extra)}")
    owner: dict[str, str] = {}
    dups = []
    for action in ACTIONS:
        for name in sets[action]:
            if name in owner:
                dups.append(f"{name} ({owner[name]}/{action})")
            owner[name] = action
    if dups:
        raise PolicyBuildError("duplicate", ", ".join(sorted(dups)))
    unknown = set(owner) - set(numbers)
    if unknown:
        raise PolicyBuildError("unknown_name", ", ".join(sorted(unknown)))
    unclassified = set(numbers) - set(owner)
    if unclassified:
        raise PolicyBuildError("unclassified", ", ".join(sorted(unclassified)))
    if not numbers or max(numbers.values()) + 1 != ceiling:
        top = max(numbers.values()) if numbers else None
        raise PolicyBuildError("ceiling_mismatch", f"header max {top}, ceiling {ceiling}")
    if set(conditions) != sets["COND"]:
        raise PolicyBuildError(
            "condition_mismatch",
            f"{sorted(set(conditions) ^ sets['COND'])}")
    for name, cond in conditions.items():
        _check_condition(name, cond)

    rows = []
    for nr in range(ceiling):
        name = by_nr.get(nr)
        if name is None:
            rows.append(Row(nr, None, "ENOSYS", None))
            continue
        action = owner[name]
        rows.append(Row(nr, name, action,
                        conditions[name].condition_id if action == "COND" else None))
    text = _canonical_text(rows, conditions)
    return PolicyTable(rows=tuple(rows), conditions=dict(conditions), ceiling=ceiling,
                       canonical_text=text,
                       policy_hash=hashlib.sha256(text.encode("ascii")).hexdigest())


@lru_cache(maxsize=1)
def policy_table() -> PolicyTable:
    """製品の表 (同梱 snapshot と分類から作る)。"""
    return build_table()


def evaluate(arch: int, nr: int, args: Sequence[int], table: PolicyTable | None = None) -> int:
    """`(arch, nr, args)` に対し filter が返すべき seccomp 戻り値。BPF を見ない。"""
    t = policy_table() if table is None else table
    nr &= _U32  # seccomp_data.nr は 32 bit
    if arch & _U32 != AUDIT_ARCH_X86_64 or nr >= X32_SYSCALL_BIT:
        return RET_KILL_PROCESS
    if nr >= t.ceiling:
        return ret_errno(_errno.ENOSYS)
    row = t.rows[nr]
    if row.action == "COND":
        full = tuple(a & _U64 for a in args) + (0,) * (6 - len(args))
        return result_value(t.conditions[row.name].evaluate(full))
    return result_value(row.action)
