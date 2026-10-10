"""機能別の seccomp profile (`afx-ops-bot` / `afx-ops-web` / `afx-ops-notify`)。

共通部分 (process・memory・file・時刻・signal、共通拒否集合) を一つ持ち、機能ごとに
socket と通信 syscall の分類だけを差し替える。表と最終 BPF の hash は下の定数で pin し、
表を変えたら pin も変えないと起動できない。

分類の根拠は CPython 3.13 + glibc の実 process の strace (UDS connect、getaddrinfo と
AI_ADDRCONFIG、UDP DNS、TCP connect、TLS 設定の読込み、thread 起動、asyncio の loop、
pre-bind した listener の accept)。trace に現れない syscall は ALLOW にしない。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from afx_ops.isolation import bpf
from afx_ops.isolation.policy import (
    ArgTest,
    Branch,
    Condition,
    PolicyTable,
    Profile,
    build_table,
)

FEATURES = ("bot", "web", "notify")
PROFILE_VERSION = 2

CONSTANTS: Mapping[str, int] = {
    "AF_UNIX": 1, "AF_INET": 2, "AF_INET6": 10, "AF_NETLINK": 16,
    "SOCK_STREAM": 1, "SOCK_DGRAM": 2, "SOCK_RAW": 3, "SOCK_TYPE_MASK": 0xF,
    "IPPROTO_IP": 0, "IPPROTO_TCP": 6, "IPPROTO_UDP": 17, "NETLINK_ROUTE": 0,
    "MSG_FASTOPEN": 0x20000000,
    "PR_GET_DUMPABLE": 3, "PR_SET_DUMPABLE": 4, "PR_SET_PTRACER": 0x59616D61,
    "F_DUPFD": 0, "F_GETFD": 1, "F_SETFD": 2, "F_GETFL": 3, "F_SETFL": 4,
    "F_DUPFD_CLOEXEC": 1030,
    "TCGETS": 0x5401, "TIOCGWINSZ": 0x5413, "FIONREAD": 0x541B, "FIONBIO": 0x5421,
    "TCGETS2": 0x802C542A, "FIONCLEX": 0x5450, "FIOCLEX": 0x5451,
    "CLONE_VM": 0x100, "CLONE_FS": 0x200, "CLONE_FILES": 0x400, "CLONE_SIGHAND": 0x800,
    "CLONE_THREAD": 0x10000, "CLONE_SYSVSEM": 0x40000, "CLONE_SETTLS": 0x80000,
    "CLONE_PARENT_SETTID": 0x100000, "CLONE_CHILD_CLEARTID": 0x200000,
}

# glibc の pthread_create が clone3 の ENOSYS 後に legacy clone へ渡す flags そのもの
# (exit signal 0)。これと 64 bit 全体で一致する clone だけを thread 作成として通す
CLONE_THREAD_FLAGS = (
    CONSTANTS["CLONE_VM"] | CONSTANTS["CLONE_FS"] | CONSTANTS["CLONE_FILES"]
    | CONSTANTS["CLONE_SIGHAND"] | CONSTANTS["CLONE_THREAD"] | CONSTANTS["CLONE_SYSVSEM"]
    | CONSTANTS["CLONE_SETTLS"] | CONSTANTS["CLONE_PARENT_SETTID"]
    | CONSTANTS["CLONE_CHILD_CLEARTID"])


def _named(*names: str) -> tuple[tuple[str, int], ...]:
    return tuple((n, CONSTANTS[n]) for n in names)


def _in(arg: int, *names: str, width: str = "lo32") -> ArgTest:
    return ArgTest(arg=arg, op="in", width=width, values=_named(*names))


def _null(arg: int) -> ArgTest:
    return ArgTest(arg=arg, op="in", width="u64", values=(("NULL", 0),))


def _type_is(*names: str) -> ArgTest:
    return ArgTest(arg=1, op="masked_in", values=_named(*names),
                   mask=("SOCK_TYPE_MASK", CONSTANTS["SOCK_TYPE_MASK"]))


def _no_fastopen(arg: int) -> ArgTest:
    return ArgTest(arg=arg, op="no_bits", mask=("MSG_FASTOPEN", CONSTANTS["MSG_FASTOPEN"]))


def _cond(condition_id: str, decision: Branch) -> Condition:
    return Condition(condition_id, decision)


_LIBRARY_LIST = (
    _cond("afx-ops-clone-thread-only-v1", Branch(
        ArgTest(arg=0, op="in", width="u64", values=(("CLONE_THREAD_FLAGS", CLONE_THREAD_FLAGS),)),
        "ALLOW", "EPERM")),
    # PR_SET_DUMPABLE は 0 にするときだけ。PR_SET_PTRACER は明示的に拒否する
    _cond("afx-ops-prctl-v1", Branch(
        _in(0, "PR_SET_DUMPABLE"),
        Branch(ArgTest(arg=1, op="in", width="u64", values=(("ZERO", 0),)), "ALLOW", "EPERM"),
        Branch(_in(0, "PR_SET_PTRACER"), "EPERM",
               Branch(_in(0, "PR_GET_DUMPABLE"), "ALLOW", "EPERM")))),
    _cond("afx-ops-fcntl-v1", Branch(
        _in(1, "F_DUPFD", "F_DUPFD_CLOEXEC", "F_GETFD", "F_SETFD", "F_GETFL", "F_SETFL"),
        "ALLOW", "EINVAL")),
    _cond("afx-ops-ioctl-v1", Branch(
        _in(1, "TCGETS", "TCGETS2", "TIOCGWINSZ", "FIONBIO", "FIONREAD", "FIOCLEX", "FIONCLEX"),
        "ALLOW", "ENOTTY")),
    _cond("afx-ops-prlimit64-self-v1", Branch(
        ArgTest(arg=0, op="in", width="u64", values=(("self", 0),)), "ALLOW", "EPERM")),
    _cond("afx-ops-socketpair-unix-v1", Branch(_in(0, "AF_UNIX"), "ALLOW", "EAFNOSUPPORT")),
    # bot / notify: UDS stream (API と NSS)、INET の TCP (外向き) と UDP (DNS)、
    # getaddrinfo の AI_ADDRCONFIG / 宛先整列が開く NETLINK_ROUTE
    _cond("afx-ops-socket-client-v1", Branch(
        _in(0, "AF_UNIX"),
        Branch(_type_is("SOCK_STREAM"), "ALLOW", "EACCES"),
        Branch(_in(0, "AF_INET", "AF_INET6"),
               Branch(_type_is("SOCK_STREAM"),
                      Branch(_in(2, "IPPROTO_IP", "IPPROTO_TCP"), "ALLOW", "EACCES"),
                      Branch(_type_is("SOCK_DGRAM"),
                             Branch(_in(2, "IPPROTO_IP", "IPPROTO_UDP"), "ALLOW", "EACCES"),
                             "EACCES")),
               Branch(_in(0, "AF_NETLINK"),
                      Branch(_type_is("SOCK_RAW"),
                             Branch(_in(2, "NETLINK_ROUTE"), "ALLOW", "EACCES"), "EACCES"),
                      "EAFNOSUPPORT")))),
    # web: UDS stream (API) だけ。新規の INET socket は作らせない
    _cond("afx-ops-socket-web-v1", Branch(
        _in(0, "AF_UNIX"),
        Branch(_type_is("SOCK_STREAM"), "ALLOW", "EACCES"),
        Branch(_in(0, "AF_INET", "AF_INET6"), "EACCES", "EAFNOSUPPORT"))),
    # TCP fast open は sendto / sendmsg で connect を兼ねるので、通信 syscall では使わせない
    _cond("afx-ops-sendto-no-fastopen-v1", Branch(_no_fastopen(3), "ALLOW", "EPERM")),
    _cond("afx-ops-sendmsg-no-fastopen-v1", Branch(_no_fastopen(2), "ALLOW", "EPERM")),
    _cond("afx-ops-sendmmsg-no-fastopen-v1", Branch(_no_fastopen(3), "ALLOW", "EPERM")),
    # web は接続済み socket の送受信だけ (宛先 / 送信元 address 引数が NULL)
    _cond("afx-ops-sendto-connected-v1", Branch(
        _no_fastopen(3), Branch(_null(4), "ALLOW", "EPERM"), "EPERM")),
    _cond("afx-ops-recvfrom-connected-v1", Branch(_null(4), "ALLOW", "EPERM")),
)

CONDITION_LIBRARY: Mapping[str, Condition] = {c.condition_id: c for c in _LIBRARY_LIST}


# --- 共通の分類 ---------------------------------------------------------------
# ALLOW: 自 process に閉じる memory / file / fd / 時刻 / signal の操作 (path 操作は Landlock
# が判定する)、接続済み socket の属性、thread の同期。

_COMMON_ALLOW = frozenset("""
read write open close stat fstat lstat poll lseek mmap mprotect munmap brk rt_sigaction
rt_sigprocmask rt_sigreturn pread64 pwrite64 readv writev access pipe select sched_yield
mremap msync mincore madvise dup dup2 pause nanosleep getitimer alarm setitimer getpid
sendfile connect shutdown getsockname getpeername setsockopt getsockopt exit uname
fsync fdatasync truncate ftruncate getdents getcwd chdir fchdir rename mkdir rmdir creat
link unlink symlink readlink umask gettimeofday getrlimit getrusage sysinfo times getuid
getgid geteuid getegid getppid getpgrp getgroups getresuid getresgid getpgid getsid capget
rt_sigpending rt_sigtimedwait rt_sigsuspend sigaltstack statfs fstatfs getpriority
sched_getparam sched_getscheduler sched_get_priority_max sched_get_priority_min
sched_rr_get_interval munlock munlockall arch_prctl gettid readahead getxattr lgetxattr
fgetxattr listxattr llistxattr flistxattr time futex sched_getaffinity epoll_create
getdents64 set_tid_address restart_syscall fadvise64 clock_gettime clock_getres
clock_nanosleep exit_group epoll_wait epoll_ctl openat mkdirat newfstatat unlinkat
renameat linkat symlinkat readlinkat faccessat pselect6 ppoll set_robust_list epoll_pwait
signalfd timerfd_create eventfd fallocate timerfd_settime timerfd_gettime signalfd4
eventfd2 epoll_create1 dup3 pipe2 preadv pwritev getcpu sched_getattr renameat2 getrandom
copy_file_range preadv2 pwritev2 statx rseq close_range openat2 faccessat2 epoll_pwait2
futex_waitv futex_wake futex_wait futex_requeue getxattrat listxattrat file_getattr
rseq_slice_yield
""".split())

# EPERM: 他 process・資源・kernel 状態への作用、process 生成と exec、namespace と mount、
# 共通拒否集合 (ptrace、memory copy、pidfd の fd 窃取、kcmp、io_uring、bpf、bind、listen)、
# metadata の変更、POSIX timer と SysV IPC。tgkill (他 process への signal は Landlock の scope
# でも止まるが、seccomp でも閉じる) と flock (読める共有 file への advisory lock) は全機能の
# 実 trace に現れないので拒否する。tgkill が拒否されると glibc の abort() は SIGABRT を
# 届けられず、異常終了命令 (SIGSEGV) で終わる (どちらも異常終了で、unit の再起動に乗る)。
_COMMON_EPERM = frozenset("""
shmget shmat shmctl bind listen fork vfork execve wait4 kill semget semop semctl shmdt
msgget msgsnd msgrcv msgctl chmod fchmod chown fchown lchown ptrace syslog setuid setgid
setpgid setsid setreuid setregid setgroups setresuid setresgid setfsuid setfsgid capset
rt_sigqueueinfo utime mknod uselib ustat sysfs setpriority sched_setparam
sched_setscheduler mlock mlockall vhangup modify_ldt pivot_root _sysctl adjtimex setrlimit
chroot sync acct settimeofday mount umount2 swapon swapoff reboot sethostname setdomainname
iopl ioperm init_module delete_module quotactl nfsservctl setxattr lsetxattr fsetxattr
removexattr lremovexattr fremovexattr tkill sched_setaffinity io_setup io_destroy
io_getevents io_submit io_cancel lookup_dcookie remap_file_pages semtimedop timer_create
timer_settime timer_gettime timer_getoverrun timer_delete clock_settime utimes mbind
set_mempolicy get_mempolicy mq_open mq_unlink mq_timedsend mq_timedreceive mq_notify
mq_getsetattr kexec_load waitid ioprio_set ioprio_get inotify_init inotify_add_watch
inotify_rm_watch inotify_init1 migrate_pages mknodat fchownat futimesat fchmodat unshare
get_robust_list splice tee sync_file_range vmsplice move_pages utimensat
rt_tgsigqueueinfo fanotify_init fanotify_mark name_to_handle_at open_by_handle_at
clock_adjtime syncfs setns process_vm_readv process_vm_writev kcmp finit_module
sched_setattr seccomp memfd_create kexec_file_load bpf execveat userfaultfd membarrier
mlock2 pkey_mprotect pkey_alloc pkey_free io_pgetevents pidfd_send_signal io_uring_setup
io_uring_enter io_uring_register open_tree move_mount fsopen fsconfig fsmount fspick
pidfd_open pidfd_getfd process_madvise mount_setattr quotactl_fd landlock_create_ruleset
landlock_add_rule landlock_restrict_self memfd_secret process_mrelease
set_mempolicy_home_node cachestat fchmodat2 map_shadow_stack statmount listmount
lsm_get_self_attr lsm_set_self_attr lsm_list_modules mseal setxattrat removexattrat
open_tree_attr file_setattr listns personality recvmmsg tgkill flock
""".split())

_COMMON_EACCES = frozenset({"perf_event_open"})

# ENOSYS: clone3 (glibc が legacy clone へ戻る)、keyring、廃止・未実装の番号
_COMMON_ENOSYS = frozenset("""
create_module get_kernel_syms query_module getpmsg putpmsg afs_syscall tuxcall security
set_thread_area get_thread_area epoll_ctl_old epoll_wait_old vserver add_key request_key
keyctl uretprobe uprobe clone3
""".split())

_COMMON_CONDITIONS = {
    "clone": "afx-ops-clone-thread-only-v1",
    "prctl": "afx-ops-prctl-v1",
    "fcntl": "afx-ops-fcntl-v1",
    "ioctl": "afx-ops-ioctl-v1",
    "prlimit64": "afx-ops-prlimit64-self-v1",
    "socketpair": "afx-ops-socketpair-unix-v1",
}

# 機能ごとの通信 syscall。ここに無い名前は共通の分類に従う
_CLIENT_COMM = {
    "COND": {
        "socket": "afx-ops-socket-client-v1",
        "sendto": "afx-ops-sendto-no-fastopen-v1",
        "sendmsg": "afx-ops-sendmsg-no-fastopen-v1",
        "sendmmsg": "afx-ops-sendmmsg-no-fastopen-v1",
    },
    "ALLOW": {"recvfrom", "recvmsg"},
    "EPERM": {"accept", "accept4"},
}

# web の accept は pre-bind した listener の自前 loop だけ。syscall は trace で accept4 に固定
WEB_ACCEPT_SYSCALL = "accept4"
_WEB_COMM = {
    "COND": {
        "socket": "afx-ops-socket-web-v1",
        "sendto": "afx-ops-sendto-connected-v1",
        "recvfrom": "afx-ops-recvfrom-connected-v1",
    },
    "ALLOW": {WEB_ACCEPT_SYSCALL},
    "EPERM": {"accept", "sendmsg", "sendmmsg", "recvmsg"},
}

_COMM = {"bot": _CLIENT_COMM, "notify": _CLIENT_COMM, "web": _WEB_COMM}


def make_profile(feature: str) -> Profile:
    if feature not in FEATURES:
        raise ValueError(feature)
    comm = _COMM[feature]
    conditions = {**_COMMON_CONDITIONS, **comm["COND"]}
    classification = {
        "ALLOW": _COMMON_ALLOW | frozenset(comm["ALLOW"]),
        "COND": frozenset(conditions),
        "EPERM": _COMMON_EPERM | frozenset(comm["EPERM"]),
        "EACCES": _COMMON_EACCES,
        "ENOSYS": _COMMON_ENOSYS,
    }
    return Profile(name=f"afx-ops-{feature}", version=PROFILE_VERSION,
                   classification=classification, conditions=conditions)


# 表 (canonical text) と最終 BPF bytes の SHA-256。表を変えたらここも変える
PINNED_HASHES: Mapping[str, tuple[str, str]] = {
    "bot": ("28a0c1af9915d3477ada056e63792a76cd43d92c2fc08fe5d5eb5079b6c38f0c",
           "b562e27e9d71bc818457f4cdd4d0a22606a48f2e62bbf0f8b0bdbddaa9a391d7"),
    "web": ("fbb6ae842a14dfaec77f127c2a8cd0379c29ec77f935506159211b817db4247a",
           "291b89949f8c88d77aa6fd8af3d2321c293d796d56e96638a85c637b24a99efe"),
    "notify": ("cd70ead38ff35e3fa89988399bff9ac0b9962f28c1b78b89d60c94b71d9caac9",
              "b562e27e9d71bc818457f4cdd4d0a22606a48f2e62bbf0f8b0bdbddaa9a391d7"),
}


@dataclass(frozen=True)
class FeatureFilter:
    feature: str
    table: PolicyTable
    compiled: bpf.CompiledPolicy


@lru_cache(maxsize=None)
def feature_filter(feature: str) -> FeatureFilter:
    """機能の表を作り、BPF にし、pin した hash と照合する。"""
    table = build_table(make_profile(feature), CONDITION_LIBRARY)
    compiled = bpf.compile_table(table)
    policy_hash, bpf_hash = PINNED_HASHES[feature]
    bpf.verify_hashes(compiled, policy_hash=policy_hash, bpf_hash=bpf_hash)
    return FeatureFilter(feature, table, compiled)
