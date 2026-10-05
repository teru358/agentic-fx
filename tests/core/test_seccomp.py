"""`allow/6` の表と命令列の純関数検査、利用可否の確認、kernel log 診断の分類。

filter の効き目 (実 kernel) は `test_seccomp_child.py` が使い捨ての子 process で見る。
このファイルは pytest の process に filter を一切掛けない。
"""
from __future__ import annotations

import dataclasses
import errno
import os
import subprocess
import sys
import textwrap

import pytest

from agentic_fx.core import seccomp as S

AUDIT_ARCH_I386 = 0x40000003


def _run_bpf(program, *, nr: int, arch: int = S.AUDIT_ARCH_X86_64, args=(0,) * 6) -> int:
    """seccomp_data に対する classic BPF の評価 (この filter が使う命令だけ)。"""
    data = {0: nr & 0xFFFFFFFF, 4: arch}
    for i, a in enumerate(args):
        a &= 0xFFFFFFFFFFFFFFFF
        data[16 + 8 * i] = a & 0xFFFFFFFF
        data[20 + 8 * i] = a >> 32
    acc = 0
    pc = 0
    while True:
        code, jt, jf, k = program[pc]
        if code == S.BPF_LD_W_ABS:
            acc = data[k]
            pc += 1
        elif code == S.BPF_ALU_AND_K:
            acc &= k
            pc += 1
        elif code == S.BPF_JEQ_K:
            pc += 1 + (jt if acc == k else jf)
        elif code == S.BPF_JGE_K:
            pc += 1 + (jt if acc >= k else jf)
        elif code == S.BPF_JSET_K:
            pc += 1 + (jt if acc & k else jf)
        elif code == S.BPF_RET_K:
            return k
        else:  # pragma: no cover - 生成器が知らない命令を出したら落とす
            raise AssertionError(f"unexpected opcode {code:#x}")


PID = 4242
KILL = S.RET_KILL_PROCESS
ALLOW = S.RET_ALLOW
EPERM = S.ret_errno(errno.EPERM)
EACCES = S.ret_errno(errno.EACCES)
ENOSYS = S.ret_errno(errno.ENOSYS)
ENOTTY = S.ret_errno(errno.ENOTTY)


@pytest.fixture(scope="module")
def prog():
    return S.build_program(S.ALLOW6_TABLE, pid=PID)


def test_profile_id_is_allow_6():
    assert S.profile_id() == "allow/6"
    assert S.SECCOMP_PROFILE == "allow/6"
    assert (S.PROFILE_NAME, S.PROFILE_VERSION) == ("allow", 6)


def test_program_has_100_instructions(prog):
    assert len(prog) == 100


def test_table_matches_spec_unconditional_allow_set():
    expected = {
        "read": 0, "write": 1, "close": 3, "lseek": 8, "pread64": 17, "openat": 257,
        "getdents64": 217, "stat": 4, "fstat": 5, "lstat": 6, "newfstatat": 262, "mmap": 9,
        "mprotect": 10, "munmap": 11, "brk": 12, "mbind": 237, "futex": 202, "rt_sigaction": 13,
        "sched_getaffinity": 204, "getrusage": 98, "uname": 63, "epoll_create1": 291,
        "exit_group": 231, "exit": 60, "gettid": 186, "rseq": 334, "set_robust_list": 273,
        "rt_sigprocmask": 14, "madvise": 28, "rt_sigreturn": 15, "restart_syscall": 219,
        "getpid": 39, "clock_gettime": 228, "clock_getres": 229, "gettimeofday": 96, "time": 201,
        "clock_nanosleep": 230, "nanosleep": 35, "sched_yield": 24, "getrandom": 318, "mremap": 25,
    }
    assert {r.name: r.nr for r in S.ALLOW6_TABLE.allow} == expected
    for r in S.ALLOW6_TABLE.allow:
        assert r.source in {"measured", "threaded", "explicit"} and r.reason


def test_table_named_errno_and_fallback_match_spec():
    assert {(r.name, r.nr, r.errno) for r in S.ALLOW6_TABLE.named_errno} == {
        ("socket", 41, errno.EACCES), ("io_uring_setup", 425, errno.EACCES),
        ("fork", 57, errno.EPERM), ("vfork", 58, errno.EPERM), ("execve", 59, errno.EPERM),
        ("memfd_create", 319, errno.EPERM), ("execveat", 322, errno.EPERM)}
    assert [(r.name, r.nr) for r in S.ALLOW6_TABLE.fallback_enosys] == [("clone3", 435)]
    assert S.ALLOW6_TABLE.fallback_enosys[0].evidence
    assert {r.name for r in S.ALLOW6_TABLE.conditional} == {
        "clone", "kill", "tgkill", "prlimit64", "fcntl", "ioctl"}
    assert S.ALLOW6_TABLE.table_end == 471
    assert S.ALLOW6_TABLE.default_action == KILL


def test_arch_and_x32_are_killed(prog):
    assert _run_bpf(prog, nr=0, arch=AUDIT_ARCH_I386) == KILL
    assert _run_bpf(prog, nr=0x40000000 + 39) == KILL
    assert _run_bpf(prog, nr=0x40000000) == KILL


@pytest.mark.parametrize("nr", [468, 469, 470, 471, 600, 1000, 0x3FFFFFFF])
def test_unknown_and_unlisted_numbers_are_killed(prog, nr):
    assert _run_bpf(prog, nr=nr) == KILL


@pytest.mark.parametrize("nr", [471, 600, 1000, 0x3FFFFFFF])
def test_table_end_row_kills_even_when_default_allows(nr):
    loose = dataclasses.replace(S.ALLOW6_TABLE, default_action=ALLOW)
    p = S.build_program(loose, pid=PID)
    assert _run_bpf(p, nr=nr) == KILL
    assert _run_bpf(p, nr=470) == ALLOW


def test_table_end_row_is_a_distinct_return_from_default(prog):
    # 番号表の外への分岐の飛び先は default の return と別の命令
    idx = next(i for i, ins in enumerate(prog)
               if ins[0] == S.BPF_JGE_K and ins[3] == S.ALLOW6_TABLE.table_end)
    target = idx + 1 + prog[idx][1]
    default_idx = next(i for i, ins in enumerate(prog)
                       if ins[0] == S.BPF_RET_K and ins[3] == S.ALLOW6_TABLE.default_action)
    assert prog[target] == (S.BPF_RET_K, 0, 0, KILL)
    assert target != default_idx


def test_every_unconditional_row_allows(prog):
    for r in S.ALLOW6_TABLE.allow:
        assert _run_bpf(prog, nr=r.nr) == ALLOW, r.name


def test_named_errno_rows(prog):
    for r in S.ALLOW6_TABLE.named_errno:
        assert _run_bpf(prog, nr=r.nr) == S.ret_errno(r.errno), r.name
    assert _run_bpf(prog, nr=435) == ENOSYS


@pytest.mark.parametrize("name,nr", [
    ("prctl", 157), ("getppid", 110), ("getuid", 102), ("geteuid", 107), ("getgid", 104),
    ("getegid", 108), ("getpgrp", 111), ("times", 100), ("alarm", 37), ("sysinfo", 99),
    ("sync", 162), ("getcwd", 79), ("shmget", 29), ("msgget", 68), ("semget", 64),
    ("mq_open", 240), ("add_key", 248), ("request_key", 249), ("keyctl", 250),
    ("tkill", 200), ("rt_sigqueueinfo", 129), ("pidfd_open", 434), ("pidfd_send_signal", 424),
    ("ptrace", 101), ("unshare", 272), ("setns", 308), ("personality", 135), ("fsopen", 430),
    ("chmod", 90), ("fchmodat2", 452), ("chown", 92), ("utimensat", 280), ("setxattr", 188),
    ("statx", 332), ("faccessat2", 439), ("socketpair", 53), ("connect", 42),
])
def test_unlisted_syscalls_fall_to_default_kill(prog, name, nr):
    assert _run_bpf(prog, nr=nr) == KILL, name


def test_clone_allows_thread_flags_only(prog):
    thread = 0x003D0F00  # glibc pthread_create の flags
    assert _run_bpf(prog, nr=56, args=(thread, 0, 0, 0, 0, 0)) == ALLOW
    assert _run_bpf(prog, nr=56, args=(17, 0, 0, 0, 0, 0)) == EPERM  # SIGCHLD = fork
    assert _run_bpf(prog, nr=56, args=(0x4111, 0, 0, 0, 0, 0)) == EPERM  # vfork 形
    for ns in (0x80, 0x20000, 0x02000000, 0x04000000, 0x08000000, 0x10000000, 0x20000000,
               0x40000000):
        assert _run_bpf(prog, nr=56, args=(thread | ns, 0, 0, 0, 0, 0)) == EPERM, hex(ns)
    for missing in (0x100, 0x800, 0x10000):
        assert _run_bpf(prog, nr=56, args=(thread & ~missing, 0, 0, 0, 0, 0)) == EPERM


def test_kill_and_tgkill_only_to_self_and_never_sigsys(prog):
    assert _run_bpf(prog, nr=62, args=(PID, 10)) == ALLOW
    assert _run_bpf(prog, nr=62, args=(PID, 31)) == EPERM
    assert _run_bpf(prog, nr=62, args=(PID + 1, 10)) == EPERM
    assert _run_bpf(prog, nr=62, args=(0, 10)) == EPERM
    assert _run_bpf(prog, nr=62, args=(-PID, 10)) == EPERM
    assert _run_bpf(prog, nr=62, args=(-1, 10)) == EPERM
    assert _run_bpf(prog, nr=234, args=(PID, PID + 3, 10)) == ALLOW
    assert _run_bpf(prog, nr=234, args=(PID, PID, 31)) == EPERM
    assert _run_bpf(prog, nr=234, args=(PID + 1, PID + 1, 10)) == EPERM


def test_prlimit64_only_self(prog):
    assert _run_bpf(prog, nr=302, args=(0, 7)) == ALLOW
    assert _run_bpf(prog, nr=302, args=(PID, 7)) == ALLOW
    assert _run_bpf(prog, nr=302, args=(PID + 1, 7)) == EPERM
    assert _run_bpf(prog, nr=302, args=(-1, 7)) == EPERM


def test_fcntl_and_ioctl_command_sets(prog):
    for cmd in (1, 2, 3, 4, 1030):
        assert _run_bpf(prog, nr=72, args=(0, cmd)) == ALLOW
    for cmd in (0, 5, 6, 7, 8, 9, 10, 11, 15, 16, 1024, 1025, 1026, 1031, 1033):
        assert _run_bpf(prog, nr=72, args=(0, cmd)) == EPERM, cmd
    for req in (0x5401, 0x802C542A, 0x5413):
        assert _run_bpf(prog, nr=16, args=(0, req)) == ALLOW
    for req in (0x8901, 0x8902, 0x40086602, 0x401C5820, 0x5412, 0x541C):
        assert _run_bpf(prog, nr=16, args=(0, req)) == ENOTTY, hex(req)


def test_build_rejects_jump_out_of_range():
    many = tuple(S.AllowRule(f"x{i}", i, "explicit", "x") for i in range(300))
    table = dataclasses.replace(S.ALLOW6_TABLE, allow=many, named_errno=(), fallback_enosys=(),
                                conditional=())
    with pytest.raises(S.FilterBuildError):
        S.build_program(table, pid=PID)


def test_build_rejects_duplicate_numbers_and_rows_beyond_table_end():
    dup = S.ALLOW6_TABLE.allow + (S.AllowRule("again", 0, "explicit", "x"),)
    with pytest.raises(S.FilterBuildError):
        S.build_program(dataclasses.replace(S.ALLOW6_TABLE, allow=dup), pid=PID)
    beyond = S.ALLOW6_TABLE.allow + (S.AllowRule("future", 500, "explicit", "x"),)
    with pytest.raises(S.FilterBuildError):
        S.build_program(dataclasses.replace(S.ALLOW6_TABLE, allow=beyond), pid=PID)


def test_program_embeds_given_pid():
    a = S.build_program(S.ALLOW6_TABLE, pid=100)
    assert _run_bpf(a, nr=62, args=(100, 10)) == ALLOW
    assert _run_bpf(a, nr=62, args=(101, 10)) == EPERM


# --- 利用可否 (filter を掛けない) -----------------------------------------------

def test_probe_support_reports_this_host_and_installs_nothing():
    # 念のため別 process で測り、測った後も filter が 0 本であることを見る
    code = textwrap.dedent("""
        import json, os
        from agentic_fx.core import seccomp as S
        s = S.probe_support()
        status = open("/proc/self/status").read()
        line = [l for l in status.splitlines() if l.startswith("Seccomp_filters:")][0]
        print(json.dumps({"reason": s.reason, "tsync": s.tsync, "log": s.log,
                          "kill": s.kill_process, "errno": s.errno_action,
                          "filters": int(line.split()[1])}))
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=60, check=True).stdout
    import json
    res = json.loads(out)
    assert res == {"reason": None, "tsync": True, "log": True, "kill": True, "errno": True,
                   "filters": 0}


def test_probe_support_off_x86_64(monkeypatch):
    monkeypatch.setattr(S.platform, "machine", lambda: "aarch64")
    s = S.probe_support()
    assert s.reason == "arch_unsupported" and not s.available


def test_probe_helpers_report_what_the_kernel_rejects_as_unsupported():
    # 未知の flag は EINVAL、未知の action は EOPNOTSUPP になる。kernel の実応答で見る
    code = textwrap.dedent("""
        import json
        from agentic_fx.core import seccomp as S
        libc = S._libc()
        out = {"unknown_flag": S._flag_supported(libc, 1 << 30),
               "tsync": S._flag_supported(libc, S.SECCOMP_FILTER_FLAG_TSYNC),
               "unknown_action": S._action_available(libc, 0x12340000),
               "kill_process": S._action_available(libc, S.RET_KILL_PROCESS)}
        status = open("/proc/self/status").read()
        line = [l for l in status.splitlines() if l.startswith("Seccomp_filters:")][0]
        out["filters"] = int(line.split()[1])
        print(json.dumps(out))
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=60, check=True).stdout
    import json
    assert json.loads(out) == {"unknown_flag": False, "tsync": True, "unknown_action": False,
                               "kill_process": True, "filters": 0}


def test_arch_rows_are_present(prog):
    # x32 の番号は番号表の末尾より大きいので末尾の行でも殺されるが、arch 検査の行は独立に持つ
    assert (S.BPF_LD_W_ABS, 0, 0, 4) == prog[0]
    assert prog[1][0] == S.BPF_JEQ_K and prog[1][3] == S.AUDIT_ARCH_X86_64
    x32 = [i for i, ins in enumerate(prog)
           if ins[0] == S.BPF_JGE_K and ins[3] == S.X32_SYSCALL_BIT]
    assert len(x32) == 1
    i = x32[0]
    assert prog[i + 1 + prog[i][1]] == (S.BPF_RET_K, 0, 0, KILL)
    assert prog[1 + 1 + prog[1][2]] == (S.BPF_RET_K, 0, 0, KILL)


class _FakeLibc:
    """kernel に触れない libc。`install_program` の呼び出し順と引数だけを記録する。"""

    def __init__(self, *, prctl_rc=0, syscall_rc=0):
        self.calls = []

        def prctl(*args):
            self.calls.append(("prctl", args))
            return prctl_rc

        def syscall(*args):
            self.calls.append(("syscall", args))
            return syscall_rc

        self.prctl = prctl
        self.syscall = syscall


def _fake_install(monkeypatch, **kw):
    fake = _FakeLibc(**kw)
    monkeypatch.setattr(S, "_libc", lambda: fake)
    monkeypatch.setattr(S.platform, "machine", lambda: "x86_64")
    return fake


def test_install_sets_no_new_privs_then_filter_with_tsync_and_log(monkeypatch):
    fake = _fake_install(monkeypatch)
    S.install_program(S.build_program(S.ALLOW6_TABLE, pid=PID))
    assert [c[0] for c in fake.calls] == ["prctl", "syscall"]
    assert fake.calls[0][1][:2] == (38, 1)
    nr, op, flags = (a.value for a in fake.calls[1][1][:3])
    assert (nr, op, flags) == (317, 1, S.SECCOMP_FILTER_FLAG_TSYNC | S.SECCOMP_FILTER_FLAG_LOG)


def test_install_treats_tsync_thread_id_return_as_failure(monkeypatch):
    # TSYNC は同期できなかった thread の id を正の値で返す
    _fake_install(monkeypatch, syscall_rc=12345)
    with pytest.raises(S.SeccompError) as ei:
        S.install_program(S.build_program(S.ALLOW6_TABLE, pid=PID))
    assert ei.value.reason == "seccomp_failed"


def test_install_reports_no_new_privs_failure(monkeypatch):
    fake = _fake_install(monkeypatch, prctl_rc=-1)
    with pytest.raises(S.SeccompError) as ei:
        S.install_program(S.build_program(S.ALLOW6_TABLE, pid=PID))
    assert ei.value.reason == "no_new_privs_failed"
    assert [c[0] for c in fake.calls] == ["prctl"]


def test_apply_allow6_returns_profile_and_size(monkeypatch):
    _fake_install(monkeypatch)
    applied = S.apply_allow6()
    assert applied == S.AppliedSeccomp(profile="allow/6", instructions=100)


def test_install_program_refuses_off_x86_64_without_touching_kernel(monkeypatch):
    # arch 検査が外れても pytest の process に本物の filter が入らないよう libc も差し替える
    fake = _fake_install(monkeypatch)
    monkeypatch.setattr(S.platform, "machine", lambda: "aarch64")
    with pytest.raises(S.SeccompError) as ei:
        S.install_program(S.build_program(S.ALLOW6_TABLE, pid=os.getpid()))
    assert ei.value.reason == "seccomp_failed"
    assert fake.calls == []


# --- kernel log 診断 ----------------------------------------------------------

AL_FULL = "kill_process kill_thread trap errno user_notif trace log"
JC = "/usr/bin/journalctl"


@pytest.mark.parametrize("args,code", [
    ((AL_FULL, JC, 0, "audit: type=1326 ...\n", ""), "kernel_log_readable"),
    ((AL_FULL, JC, 0, "", "Hint: You are currently not seeing messages from other users and "
      "the system.\n"), "kernel_log_unreadable:permission"),
    ((AL_FULL, JC, 1, "", "No journal files were opened due to insufficient permissions.\n"),
     "kernel_log_unreadable:permission"),
    ((AL_FULL, None, None, "", ""), "kernel_log_unreadable:no_journalctl"),
    ((AL_FULL, JC, None, "", ""), "kernel_log_unreadable:timeout"),
    ((AL_FULL, JC, 0, "", ""), "kernel_log_unreadable:empty"),
    (("kill_process kill_thread trap", JC, 0, "x\n", ""), "seccomp_log_missing:errno"),
    (("kill_thread trap errno", JC, 0, "x\n", ""), "seccomp_log_missing:kill_process"),
    ((None, JC, 0, "x\n", ""), "seccomp_log_missing:actions_logged_unreadable"),
])
def test_classify_kernel_log(args, code):
    assert S.classify_kernel_log(*args) == code
    assert code in S.KERNEL_LOG_CODES


def test_check_kernel_log_maps_timeout_and_missing_binary(tmp_path):
    al = tmp_path / "actions_logged"
    al.write_text(AL_FULL + "\n")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="journalctl", timeout=5)

    assert S.check_kernel_log(actions_logged_path=str(al), which=lambda _n: JC,
                              run=boom) == "kernel_log_unreadable:timeout"
    assert S.check_kernel_log(actions_logged_path=str(al), which=lambda _n: None,
                              run=boom) == "kernel_log_unreadable:no_journalctl"
    assert S.check_kernel_log(actions_logged_path=str(tmp_path / "nope"), which=lambda _n: JC,
                              run=boom) == "seccomp_log_missing:actions_logged_unreadable"

    def hint_only(*a, **k):
        return subprocess.CompletedProcess(a, 0, "", "Hint: You are currently not seeing "
                                           "messages from other users and the system.\n")

    assert S.check_kernel_log(actions_logged_path=str(al), which=lambda _n: JC,
                              run=hint_only) == "kernel_log_unreadable:permission"


def test_check_kernel_log_on_this_host_returns_a_fixed_code():
    assert S.check_kernel_log() in S.KERNEL_LOG_CODES


def test_sigsys_diagnostic_line_names_pid_and_journal_query():
    line = S.sigsys_diagnostic_line(1234)
    assert "\n" not in line
    assert "pid=1234" in line
    assert "journalctl -k -g 'type=1326.*pid=1234 '" in line
