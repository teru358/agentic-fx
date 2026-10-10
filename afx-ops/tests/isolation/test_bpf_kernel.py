"""生成した機能別 BPF を実 kernel に入れ、代表引数の syscall の実 errno が evaluator と一致する。

filter は使い捨ての子 process (`RLIMIT_CORE=0`) の中で掛ける。許可の行は「kernel まで
届いた」ことが分かる結果 (成功か、seccomp が返さない errno) を期待値として持つ。
"""
from __future__ import annotations

import errno
import json
import signal
import subprocess
import sys

import pytest

from _afx_isolation_support import needs_x86_64, no_core
from afx_ops.isolation import profiles as P
from afx_ops.isolation.policy import AUDIT_ARCH_X86_64, RET_ALLOW, evaluate, ret_errno
from afx_ops.isolation.syscalls_x86_64 import SNAPSHOT_NUMBERS as NR

pytestmark = needs_x86_64

_CHILD = r"""
import ctypes, json, os, resource, sys
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
from afx_ops.isolation import bpf, profiles
feature, probes = sys.argv[1], json.loads(sys.argv[2])
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
bpf.install(profiles.feature_filter(feature).compiled)
out = []
for nr, args in probes:
    ctypes.set_errno(0)
    r = libc.syscall(ctypes.c_long(nr), *[ctypes.c_long(a) for a in args])
    res = [int(r), ctypes.get_errno() if r == -1 else 0]
    out.append(res)
    if res[0] >= 0 and nr == 41:
        os.close(res[0])
print("OUT " + json.dumps(out))
"""

OK = "ok"  # 成功 (戻り値 >= 0)


def _probes(feature: str) -> list[tuple[str, tuple, object]]:
    """(syscall 名, 引数, 許可されたときの期待)。拒否の行の期待は evaluator が決める。"""
    flags = P.CLONE_THREAD_FLAGS
    probes = [
        ("getpid", (), OK),
        ("ptrace", (16, 1, 0, 0), None),
        ("process_vm_readv", (1, 0, 0, 0, 0, 0), None),
        ("process_vm_writev", (1, 0, 0, 0, 0, 0), None),
        ("pidfd_open", (1, 0), None),
        ("pidfd_getfd", (0, 0, 0), None),
        ("kcmp", (1, 1, 0, 0, 0), None),
        ("process_madvise", (0, 0, 0, 0, 0), None),
        ("perf_event_open", (0, 0, -1, -1, 0), None),
        ("bpf", (0, 0, 0), None),
        ("io_uring_setup", (1, 0), None),
        ("io_uring_enter", (0, 0, 0, 0, 0, 0), None),
        ("io_uring_register", (0, 0, 0, 0), None),
        ("keyctl", (0, 0, 0, 0, 0), None),
        ("add_key", (0, 0, 0, 0, 0), None),
        ("clone3", (0, 0), None),
        ("clone", (0x10000000 | 17, 0, 0, 0, 0), None),
        ("clone", (0x8000 | 17, 0, 0, 0, 0), None),
        ("clone", (17, 0, 0, 0, 0), None),
        ("clone", (flags | 0x10000000, 0, 0, 0, 0), None),
        ("clone", (flags | (1 << 32), 0, 0, 0, 0), None),
        ("unshare", (0x10000000,), None),
        ("setns", (0, 0), None),
        ("mount", (0, 0, 0, 0, 0), None),
        ("bind", (-1, 0, 0), None),
        ("listen", (-1, 1), None),
        ("accept", (-1, 0, 0), ("errno", errno.EBADF)),
        ("accept4", (-1, 0, 0, 0), ("errno", errno.EBADF)),
        ("execve", (0, 0, 0), None),
        ("fork", (), None),
        ("kill", (1, 0), None),
        ("tkill", (1, 0), None),
        ("wait4", (-1, 0, 0, 0), None),
        ("prctl", (4, 0, 0, 0, 0), OK),
        ("prctl", (4, 1, 0, 0, 0), None),
        ("prctl", (4, 1 << 32, 0, 0, 0), None),
        ("prctl", (3, 0, 0, 0, 0), OK),
        ("prctl", (0x59616D61, 0, 0, 0, 0), None),
        ("prctl", (15, 0, 0, 0, 0), None),
        ("fcntl", (0, 1), OK),
        ("fcntl", (0, 8, 1), None),  # F_SETOWN
        ("ioctl", (-1, 0x5421, 0), ("errno", errno.EBADF)),
        ("ioctl", (0, 0x5412, 0), None),  # TIOCSTI
        ("prlimit64", (0, 7, 0, 0), OK),
        ("prlimit64", (1, 7, 0, 0), None),
        ("socket", (1, 1 | 0o2000000, 0), OK),
        ("socket", (1, 2, 0), None),
        ("socket", (2, 1, 0), OK),
        ("socket", (10, 2, 0), OK),
        ("socket", (2, 3, 1), None),
        ("socket", (16, 3, 0), OK),
        ("socket", (16, 3, 9), None),
        ("socket", (17, 3, 0), None),
        ("socketpair", (2, 1, 0, 0), None),
        ("sendto", (-1, 0, 0, 0, 0, 0), ("errno", errno.EBADF)),
        ("sendto", (-1, 0, 0, 0, 0x1000, 16), ("errno", errno.EBADF)),
        ("sendto", (-1, 0, 0, 0x20000000, 0, 0), None),
        ("recvfrom", (-1, 0, 0, 0, 0, 0), ("errno", errno.EBADF)),
        ("recvfrom", (-1, 0, 0, 0, 0x1000, 0x2000), ("errno", errno.EBADF)),
        ("sendmsg", (-1, 0, 0), ("errno", errno.EBADF)),
        ("recvmsg", (-1, 0, 0), ("errno", errno.EBADF)),
        ("recvmmsg", (-1, 0, 0, 0, 0), None),
        ("seccomp", (0, 0, 0), None),
        ("memfd_create", (0, 0), None),
    ]
    return probes


def _expected(feature: str, name: str, args: tuple, allowed) -> object:
    from afx_ops.isolation.profiles import feature_filter
    table = feature_filter(feature).table
    full = tuple(a & 0xFFFFFFFFFFFFFFFF for a in args)
    value = evaluate(table, AUDIT_ARCH_X86_64, NR[name], full)
    if value == RET_ALLOW:
        assert allowed is not None, f"{feature} {name}{args}: 表は許可だが期待が無い"
        return allowed
    assert value & 0xFFFF0000 == 0x00050000, hex(value)
    return ("errno", value & 0xFFFF)


@pytest.mark.parametrize("feature", P.FEATURES)
def test_bpf_matches_evaluator_on_kernel(feature, child_env):
    probes = _probes(feature)
    payload = json.dumps([(NR[name], list(args)) for name, args, _ in probes])
    done = subprocess.run([sys.executable, "-I", "-c", _CHILD, feature, payload],
                          capture_output=True, text=True, timeout=60, env=child_env,
                          preexec_fn=no_core)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.split("OUT ", 1)[1])
    mismatches = []
    allowed_rows = denied_rows = 0
    for (name, args, allowed), (rc, err) in zip(probes, out):
        expected = _expected(feature, name, args, allowed)
        if expected is allowed:
            allowed_rows += 1
        else:
            denied_rows += 1
        ok = rc >= 0 if expected == OK else (rc, err) == (-1, expected[1])
        if not ok:
            mismatches.append((name, args, expected, (rc, err)))
    assert mismatches == []
    assert allowed_rows >= 10 and denied_rows >= 30


def test_x32_syscall_kills_the_process(child_env):
    code = _CHILD.replace('out = []', 'libc.syscall(ctypes.c_long(0x40000000 | 39))\nout = []')
    done = subprocess.run([sys.executable, "-I", "-c", code, "bot", "[]"],
                          capture_output=True, text=True, timeout=60, env=child_env,
                          preexec_fn=no_core)
    assert done.returncode == -signal.SIGSYS
    assert "OUT" not in done.stdout


def test_ret_errno_shape():
    assert ret_errno(errno.EPERM) == 0x00050001
