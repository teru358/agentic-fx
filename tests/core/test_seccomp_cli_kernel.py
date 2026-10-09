"""生成した `improve-cli/1` の BPF を実 kernel が受け付け、代表行が表どおりに返ることを見る。

filter は使い捨ての子 process (`sys.executable -P -B -c`、`RLIMIT_CORE=0`) の中で掛ける。
`connect` は USER_NOTIF で listener が無いと意味が変わるので呼ばない。
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64",
    reason="improve-cli/1 は x86_64 Linux 専用")

_CHILD = r"""
import ctypes, errno, json, os, resource
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
from agentic_fx.core import seccomp_cli_bpf as B
from agentic_fx.core import seccomp_cli_policy as P
compiled = B.compile_policy()
NR = P.SNAPSHOT_NUMBERS
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long

class F(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8),
                ("k", ctypes.c_uint32)]

class Prog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(F))]

arr = (F * len(compiled.program))(*[F(*x) for x in compiled.program])
fprog = Prog(len(compiled.program), ctypes.cast(arr, ctypes.POINTER(F)))

def raw(nr, *args):
    conv = [ctypes.c_long(a) for a in args]
    ctypes.set_errno(0)
    r = libc.syscall(ctypes.c_long(nr), *conv)
    return [r, ctypes.get_errno() if r == -1 else 0]

def install(flags):
    ctypes.set_errno(0)
    r = libc.syscall(ctypes.c_long(317), ctypes.c_uint(1), ctypes.c_uint(flags), ctypes.byref(fprog))
    return [r, ctypes.get_errno() if r < 0 else 0]

out = {"flags": compiled.filter_flags}
out["nnp"] = libc.prctl(38, 1, 0, 0, 0)
# TSYNC_ESRCH を外すと TSYNC | NEW_LISTENER は受け付けない (install は起きない)
out["without_esrch"] = install(compiled.filter_flags & ~0x10)
out["install"] = install(compiled.filter_flags)
if out["install"][0] >= 0:
    os.close(out["install"][0])
out["getpid"] = raw(NR["getpid"])
out["ptrace"] = raw(NR["ptrace"], 0, 0, 0, 0)
out["perf_event_open"] = raw(NR["perf_event_open"], 0, 0, -1, -1, 0)
out["keyctl"] = raw(NR["keyctl"], 0, 0, 0, 0, 0)
out["nr472"] = raw(472)
out["socket_unix"] = raw(NR["socket"], 1, 1, 0)
out["socket_netlink"] = raw(NR["socket"], 16, 3, 0)
out["socket_dgram"] = raw(NR["socket"], 2, 2, 0)
s = raw(NR["socket"], 2, 1, 0)
out["socket_stream"] = s
if s[0] >= 0:
    os.close(s[0])
out["listen"] = raw(NR["listen"], -1, 1)
r = raw(NR["clone"], 0x8000 | 17, 0, 0, 0, 0)
if r[0] == 0:
    os._exit(0)
if r[0] > 0:
    os.waitpid(r[0], 0)
out["clone_parent"] = r
out["prctl_set_dumpable"] = raw(NR["prctl"], 4, 0, 0, 0, 0)
out["prctl_get_dumpable"] = raw(NR["prctl"], 3, 0, 0, 0, 0)
out["fcntl_setlkw"] = raw(NR["fcntl"], -1, 7, 0)
out["ioctl_tiocsti"] = raw(NR["ioctl"], -1, 0x5412, 0)
out["personality0"] = raw(NR["personality"], 0)
out["clone3"] = raw(NR["clone3"], 0, 0)
out["timer_create"] = raw(NR["timer_create"], 0, 0, 0)
os.write(1, ("ALIVE " + json.dumps(out) + "\n").encode())
os._exit(0)
"""


_PROBE = r"""
import ctypes, json, resource
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
from agentic_fx.core import seccomp_cli_bpf as B

class F(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8),
                ("k", ctypes.c_uint32)]

class Prog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(F))]

libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
libc.prctl(38, 1, 0, 0, 0)
arr = (F * 1)(F(0x06, 0, 0, 0x7FFF0000))  # RET ALLOW
fprog = Prog(1, ctypes.cast(arr, ctypes.POINTER(F)))
ctypes.set_errno(0)
r = libc.syscall(ctypes.c_long(317), ctypes.c_uint(1),
                 ctypes.c_uint(B.compile_policy().filter_flags), ctypes.byref(fprog))
print("PROBE " + json.dumps([r, ctypes.get_errno() if r < 0 else 0]))
"""


def _env():
    env = dict(os.environ)
    env.setdefault("OPENBLAS_NUM_THREADS", "1")
    return env


@pytest.fixture(scope="module")
def kernel_supports_filter_flags():
    """製品と同じ filter flags (TSYNC_ESRCH / WAIT_KILLABLE_RECV / NEW_LISTENER) を使い捨ての
    子で試し、kernel か container が受け付けないなら製品の不具合ではないので skip する。"""
    proc = subprocess.run([sys.executable, "-P", "-B", "-c", _PROBE],
                          capture_output=True, env=_env(), timeout=60)
    text = proc.stdout.decode(errors="replace")
    if "PROBE " not in text:
        pytest.skip(f"seccomp probe did not run: {proc.stderr.decode(errors='replace')[-300:]}")
    r, err = json.loads(text.split("PROBE ", 1)[1].splitlines()[0])
    if r < 0:
        pytest.skip(f"seccomp filter flags unsupported here: {errno.errorcode.get(err, err)}")


def test_kernel_accepts_program_and_sample_rows_match(kernel_supports_filter_flags):
    from agentic_fx.core import seccomp_cli_policy as P

    env = _env()
    proc = subprocess.run([sys.executable, "-P", "-B", "-c", textwrap.dedent(_CHILD)],
                          capture_output=True, env=env, timeout=60)
    text = proc.stdout.decode(errors="replace")
    assert proc.returncode == 0, (proc.returncode, proc.stderr.decode(errors="replace")[-2000:])
    out = json.loads(text.split("ALIVE ", 1)[1].splitlines()[0])

    assert out["flags"] == 1 | 8 | 16 | 32
    assert out["nnp"] == 0
    assert out["without_esrch"] == [-1, errno.EINVAL]
    assert out["install"][0] >= 0, out["install"]  # listener fd

    def denied(key, err):
        assert out[key] == [-1, err], (key, out[key])

    assert out["getpid"][0] > 0 and out["getpid"][1] == 0
    denied("ptrace", errno.EPERM)
    denied("perf_event_open", errno.EACCES)
    denied("keyctl", errno.ENOSYS)
    denied("nr472", errno.ENOSYS)
    denied("socket_unix", errno.EACCES)
    denied("socket_netlink", errno.EAFNOSUPPORT)
    denied("socket_dgram", errno.EACCES)
    assert out["socket_stream"][0] >= 0, out["socket_stream"]
    denied("listen", errno.EPERM)
    denied("clone_parent", errno.EPERM)
    denied("prctl_set_dumpable", errno.EINVAL)
    assert out["prctl_get_dumpable"][1] == 0 and out["prctl_get_dumpable"][0] >= 0
    denied("fcntl_setlkw", errno.EINVAL)
    denied("ioctl_tiocsti", errno.ENOTTY)
    denied("personality0", errno.EPERM)
    denied("clone3", errno.ENOSYS)
    denied("timer_create", errno.EPERM)

    # 子で観測した errno は data evaluator の表とも一致する
    nr = P.SNAPSHOT_NUMBERS
    for key, name, args in (("ptrace", "ptrace", ()), ("perf_event_open", "perf_event_open", ()),
                            ("keyctl", "keyctl", ()), ("socket_unix", "socket", (1, 1)),
                            ("socket_netlink", "socket", (16, 3)),
                            ("socket_dgram", "socket", (2, 2)),
                            ("clone_parent", "clone", (0x8000 | 17,)),
                            ("prctl_set_dumpable", "prctl", (4,)),
                            ("fcntl_setlkw", "fcntl", (0xFFFFFFFF, 7)),
                            ("ioctl_tiocsti", "ioctl", (0xFFFFFFFF, 0x5412)),
                            ("personality0", "personality", (0,)), ("listen", "listen", ())):
        full = tuple(args) + (0,) * (6 - len(args))
        assert P.evaluate(0xC000003E, nr[name], full) == 0x00050000 | out[key][1], key
