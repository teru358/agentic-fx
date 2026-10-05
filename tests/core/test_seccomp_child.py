"""`allow/6` の効き目を実 kernel で見る。

filter は必ず使い捨ての子 process (`sys.executable -c`) の中で掛ける。pytest の
process には掛けない (掛けた瞬間に以後のテストが全部死ぬ)。子は `RLIMIT_CORE=0` で
起動し、SIGSYS で死んでも core を残さない。生き残った子は `ALIVE <json>` を書いて
`os._exit(0)` する (通常の interpreter 終了処理は表の外の syscall を呼ぶ)。
判定は wait status (Popen.returncode が負なら signal 死) で行い、kernel log に頼らない。
"""
from __future__ import annotations

import json
import os
import re
import signal
import struct
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agentic_fx.core import seccomp as S

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64"
    or S.probe_support().reason is not None,
    reason="seccomp allow/6 needs x86_64 Linux with TSYNC/LOG")

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "docs" / "examples" / "plugins"

_PRELUDE = """
import ctypes, errno, json, os, resource, signal, sys, threading, dataclasses
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
from agentic_fx.core import seccomp as S
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
def raw(nr, *args):
    conv = [ctypes.c_long(a) if isinstance(a, int) else a for a in args]
    ctypes.set_errno(0)
    r = libc.syscall(ctypes.c_long(nr), *conv)
    return [r, ctypes.get_errno() if r == -1 else 0]
def _exc(fn):
    try:
        fn()
        return "ok"
    except OSError as e:
        return e.errno
out = {}
"""

_APPLY_REAL = "S.apply_allow6()\n"

_EPILOGUE = """
os.write(1, ("ALIVE " + json.dumps(out) + "\\n").encode())
os._exit(0)
"""


def _control_apply(table_expr: str) -> str:
    return f"S.install_program(S.build_program({table_expr}, pid=os.getpid()), log=True)\n"


def run_child(action: str, *, pre: str = "", apply: str = _APPLY_REAL, env: dict | None = None,
              timeout: float = 120.0):
    code = (_PRELUDE + textwrap.dedent(pre) + apply + textwrap.dedent(action) + _EPILOGUE)
    full_env = dict(os.environ)
    full_env.setdefault("OPENBLAS_NUM_THREADS", "1")
    full_env.update(env or {})
    # -P: `-c` は sys.path に '' (cwd) を入れ、filter 後の import 探索が getcwd (表の外) を呼ぶ。
    # -B: 未キャッシュの module を import すると __pycache__ の mkdir (表の外) を呼ぶ。
    # worker 本体もこの 2 つを filter 前に避ける必要がある。ここでは子の起動形を揃えるだけ
    proc = subprocess.Popen([sys.executable, "-P", "-B", "-c", code], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=full_env)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    text = stdout.decode(errors="replace")
    alive = None
    if "ALIVE " in text:
        alive = json.loads(text.split("ALIVE ", 1)[1].splitlines()[0])
    return proc.returncode, alive, stderr.decode(errors="replace"), proc.pid


def assert_sigsys(result):
    rc, alive, err, _pid = result
    assert rc == -signal.SIGSYS, (rc, alive, err[-2000:])
    assert alive is None


def assert_alive(result):
    rc, alive, err, _pid = result
    assert rc == 0 and alive is not None, (rc, err[-2000:])
    return alive


@pytest.fixture
def sleeper():
    """他 process 操作の的。pytest 自身を的にしない。"""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        yield p
    finally:
        p.kill()
        p.wait()


def _limits(pid: int) -> str:
    return Path(f"/proc/{pid}/limits").read_text()


def _ipc_counts() -> tuple[int, int, int]:
    return tuple(len(Path(f"/proc/sysvipc/{n}").read_text().splitlines())
                 for n in ("shm", "msg", "sem"))


# --- 適用そのもの -------------------------------------------------------------

def test_apply_sets_one_filter_on_every_thread():
    pre = """
    started = threading.Event(); stop = threading.Event()
    def spin():
        started.set(); stop.wait()
    t = threading.Thread(target=spin, daemon=True); t.start(); started.wait()
    """
    action = """
    out["applied"] = [S.SECCOMP_PROFILE]
    rows = {}
    for tid in os.listdir("/proc/self/task"):
        st = open(f"/proc/self/task/{tid}/status").read()
        rows[tid] = [l.split()[1] for l in st.splitlines()
                     if l.split(":")[0] in ("NoNewPrivs", "Seccomp", "Seccomp_filters")]
    out["tasks"] = rows
    stop.set(); t.join()
    """
    alive = assert_alive(run_child(action, pre=pre))
    assert alive["applied"] == ["allow/6"]
    assert len(alive["tasks"]) == 2
    assert all(v == ["1", "2", "1"] for v in alive["tasks"].values())


# --- #8 process 生成 ----------------------------------------------------------

def test_process_creation_is_eperm_and_clone3_is_enosys_and_threads_work():
    action = """
    def fork_py():
        pid = os.fork()
        if pid == 0:
            os._exit(0)
    out["os_fork"] = _exc(fork_py)
    out["posix_spawn"] = _exc(lambda: os.posix_spawn("/bin/true", ["true"], {}))
    r = raw(57)
    if r[0] == 0:
        libc._exit(0)
    out["raw_fork"] = r
    r = raw(56, 17, 0, 0, 0, 0)             # SIGCHLD だけ = fork と同じ
    if r[0] == 0:
        libc._exit(0)
    out["clone_sigchld"] = r
    r = raw(56, 0x4111, 0, 0, 0, 0)         # CLONE_VM | CLONE_VFORK | SIGCHLD
    if r[0] == 0:
        libc._exit(0)
    out["clone_vfork"] = r
    r = raw(56, 0x003D0F00 | 0x10000000, 0, 0, 0, 0)   # thread flags + CLONE_NEWUSER
    out["clone_thread_newuser"] = r
    r = raw(58)
    if r[0] == 0:
        libc._exit(0)
    out["raw_vfork"] = r
    args = (ctypes.c_uint64 * 11)()
    args[4] = 17
    out["clone3"] = raw(435, ctypes.byref(args), ctypes.sizeof(args))
    box = []
    ts = [threading.Thread(target=lambda: box.append(1)) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    out["threads"] = len(box)
    """
    alive = assert_alive(run_child(action))
    assert alive["os_fork"] == 1
    assert alive["posix_spawn"] == 1
    for key in ("raw_fork", "clone_sigchld", "clone_vfork", "clone_thread_newuser", "raw_vfork"):
        assert alive[key] == [-1, 1], key
    assert alive["clone3"] == [-1, 38]
    assert alive["threads"] == 4


# --- #14 exec と memfd --------------------------------------------------------

def _tiny_static_elf(exit_code: int) -> bytes:
    """exit_group(exit_code) だけを呼ぶ x86_64 の静的 ELF。"""
    base = 0x400000
    code = (b"\xbf" + struct.pack("<I", exit_code)     # mov edi, exit_code
            + b"\xb8" + struct.pack("<I", 231)         # mov eax, exit_group
            + b"\x0f\x05")                              # syscall
    ehdr_size, phdr_size = 64, 56
    entry = base + ehdr_size + phdr_size
    total = ehdr_size + phdr_size + len(code)
    ehdr = (b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8)
            + struct.pack("<HHIQQQIHHHHHH", 2, 0x3E, 1, entry, ehdr_size, 0, 0,
                          ehdr_size, phdr_size, 1, 0, 0, 0))
    phdr = struct.pack("<IIQQQQQQ", 1, 5, 0, base, base, total, total, 0x1000)
    return ehdr + phdr + code


_MEMFD_EXEC = """
mfd = raw(319, ctypes.c_char_p(b"probe"), 0)
out["memfd_create"] = mfd
if mfd[0] >= 0:
    os.write(mfd[0], bytes.fromhex(ELF_HEX))
    out["execveat_memfd"] = raw(322, mfd[0], ctypes.c_char_p(b""), None, None, 0x1000)
"""


def test_exec_and_memfd_are_eperm_in_final_profile(tmp_path):
    pre = f"ELF_HEX = {_tiny_static_elf(42).hex()!r}\n"
    action = _MEMFD_EXEC + f"""
out["execve"] = raw(59, ctypes.c_char_p(b"/bin/true"), None, None)
fd = os.open({sys.executable!r}, os.O_RDONLY)
out["execveat_fd"] = raw(322, fd, ctypes.c_char_p(b""), None, None, 0x1000)
out["os_execv"] = _exc(lambda: os.execv("/bin/true", ["true"]))
"""
    alive = assert_alive(run_child(action, pre=pre))
    assert alive["memfd_create"] == [-1, 1]
    assert alive["execve"] == [-1, 1]
    assert alive["execveat_fd"] == [-1, 1]
    assert alive["os_execv"] == 1


def test_control_without_exec_rows_lets_memfd_execveat_run():
    # exec と memfd の名前つき拒否を外し allow へ移した対照。外すだけだと default KILL になる
    table = """dataclasses.replace(S.ALLOW6_TABLE,
        named_errno=tuple(r for r in S.ALLOW6_TABLE.named_errno
                          if r.name not in ("memfd_create", "execveat", "execve")),
        allow=S.ALLOW6_TABLE.allow + (S.AllowRule("memfd_create", 319, "explicit", "control"),
                                      S.AllowRule("execveat", 322, "explicit", "control"),
                                      S.AllowRule("execve", 59, "explicit", "control")))"""
    pre = f"ELF_HEX = {_tiny_static_elf(42).hex()!r}\n"
    rc, alive, err, _ = run_child(_MEMFD_EXEC, pre=pre, apply=_control_apply(table))
    # exec が成立すると置き換わった image が exit_group(42) で終わる
    assert rc == 42, (rc, alive, err[-2000:])


# --- #17 / #19 表の外は 1 子 1 syscall で SIGSYS --------------------------------

_SIGSYS_TARGET_CALLS = {
    "shmget": "raw(29, 0, 4096, 0o1600)",
    "msgget": "raw(68, 0, 0o1600)",
    "semget": "raw(64, 0, 1, 0o1600)",
    "mq_open": "raw(240, ctypes.c_char_p(b'afx_probe'), 0o102, 0o600, None)",
    "setpriority": "raw(141, 0, TARGET, 19)",
    "sched_setaffinity": "raw(203, TARGET, 8, ctypes.byref(ctypes.c_uint64(1)))",
    "pidfd_open": "raw(434, TARGET, 0)",
    "kcmp": "raw(312, os.getpid(), TARGET, 0, 0, 0)",
    "prctl_set_ptracer": "raw(157, 0x59616d61, TARGET, 0, 0, 0)",
    "ptrace_attach": "raw(101, 16, TARGET, 0, 0)",
    "unshare": "raw(272, 0x10000000)",
    "setns": "raw(308, 0, 0)",
    "personality": "raw(135, 0xffffffff)",
    "fsopen": "raw(430, ctypes.c_char_p(b'tmpfs'), 0)",
    "add_key": "raw(248, ctypes.c_char_p(b'user'), ctypes.c_char_p(b'afx'), None, 0, -2)",
    "request_key": "raw(249, ctypes.c_char_p(b'user'), ctypes.c_char_p(b'afx'), None, -2)",
    "keyctl": "raw(250, 0, -2, 0)",
    "tkill": "raw(200, TARGET, 0)",
    "rt_sigqueueinfo": "raw(129, TARGET, 0, ctypes.byref((ctypes.c_int * 32)()))",
    "rt_tgsigqueueinfo": "raw(297, TARGET, TARGET, 0, ctypes.byref((ctypes.c_int * 32)()))",
    "pidfd_send_signal": "raw(424, 0, 0, None, 0)",
    "process_vm_readv": "raw(310, TARGET, None, 0, None, 0, 0)",
    "migrate_pages": "raw(256, TARGET, 1, None, None)",
    "sched_setscheduler": "raw(144, TARGET, 0, None)",
    "ioprio_set": "raw(251, 1, TARGET, 0)",
}


@pytest.mark.parametrize("name", sorted(_SIGSYS_TARGET_CALLS))
def test_other_process_ipc_keyring_namespace_calls_are_killed(name, sleeper):
    before_ipc = _ipc_counts()
    before_limits = _limits(sleeper.pid)
    pre = f"TARGET = {sleeper.pid}\n"
    action = f"out['{name}'] = {_SIGSYS_TARGET_CALLS[name]}\n"
    assert_sigsys(run_child(action, pre=pre))
    assert _ipc_counts() == before_ipc
    assert _limits(sleeper.pid) == before_limits
    assert sleeper.poll() is None


_FILE_CALLS = {
    "chmod": "raw(90, PATH, 0o777)",
    "fchmod": "raw(91, FD, 0o777)",
    "fchmodat2": "raw(452, -100, PATH, 0o777, 0)",
    "chown": "raw(92, PATH, UID, -1)",
    "fchown": "raw(93, FD, UID, -1)",
    "utimensat": "raw(280, -100, PATH, None, 0)",
    "utimes": "raw(235, PATH, None)",
    "setxattr": "raw(188, PATH, ctypes.c_char_p(b'user.afx'), ctypes.c_char_p(b'v'), 1, 0)",
    "fsetxattr": "raw(190, FD, ctypes.c_char_p(b'user.afx'), ctypes.c_char_p(b'v'), 1, 0)",
    "file_setattr": "raw(469, -100, PATH, None, 0, 0)",
}


def _file_state(path: Path):
    st = os.stat(path)
    try:
        xattrs = sorted(os.listxattr(path))
    except OSError:
        xattrs = None
    return st.st_mode, st.st_mtime_ns, st.st_uid, xattrs


@pytest.mark.parametrize("name", sorted(_FILE_CALLS))
def test_metadata_changes_are_killed_and_file_is_unchanged(name, tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("x")
    os.chmod(target, 0o600)
    before = _file_state(target)
    pre = (f"PATH = ctypes.c_char_p({str(target).encode()!r})\n"
           f"FD = os.open({str(target)!r}, os.O_RDONLY)\nUID = {os.getuid()}\n")
    action = f"out['{name}'] = {_FILE_CALLS[name]}\n"
    assert_sigsys(run_child(action, pre=pre))
    assert _file_state(target) == before


def test_other_pid_kill_and_prlimit_are_eperm_self_signal_and_thread_work(sleeper):
    before_limits = _limits(sleeper.pid)
    pre = f"""
    TARGET = {sleeper.pid}
    hits = []
    signal.signal(signal.SIGUSR1, lambda s, f: hits.append(s))
    """
    action = """
    out["kill_other_term"] = raw(62, TARGET, 15)
    out["kill_other_0"] = raw(62, TARGET, 0)
    new = (ctypes.c_uint64 * 2)(16, 16)
    out["prlimit_other_set"] = raw(302, TARGET, 7, ctypes.byref(new), None)
    old = (ctypes.c_uint64 * 2)()
    out["prlimit_other_get"] = raw(302, TARGET, 7, None, ctypes.byref(old))
    out["prlimit_self_get"] = raw(302, 0, 7, None, ctypes.byref(old))[0]
    signal.raise_signal(signal.SIGUSR1)
    os.kill(os.getpid(), signal.SIGUSR1)
    out["hits"] = hits
    box = []
    t = threading.Thread(target=lambda: box.append(1)); t.start(); t.join()
    out["thread"] = box
    """
    alive = assert_alive(run_child(action, pre=pre))
    for key in ("kill_other_term", "kill_other_0", "prlimit_other_set", "prlimit_other_get"):
        assert alive[key] == [-1, 1], key
    assert alive["prlimit_self_get"] == 0
    assert alive["hits"] == [signal.SIGUSR1, signal.SIGUSR1]
    assert alive["thread"] == [1]
    assert sleeper.poll() is None
    assert _limits(sleeper.pid) == before_limits


def test_named_errno_rows_on_real_kernel(tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("x")
    pre = f"FD = os.open({str(target)!r}, os.O_RDONLY)\nTARGET = os.getpid()\n"
    action = """
    for name, cmd in (("F_SETOWN", 8), ("F_SETSIG", 10), ("F_SETOWN_EX", 15),
                      ("F_SETLEASE", 1024), ("F_NOTIFY", 1026), ("F_SETLK", 6), ("F_GETLK", 5),
                      ("F_OFD_SETLK", 37)):
        out["fcntl_" + name] = raw(72, FD, cmd, 0)
    out["fcntl_F_GETFL"] = raw(72, FD, 3)[0] >= 0
    out["fcntl_F_DUPFD_CLOEXEC"] = raw(72, FD, 1030, 0)[0] >= 0
    flags = ctypes.c_int(0)
    for name, req in (("FIOSETOWN", 0x8901), ("SIOCSPGRP", 0x8902),
                      ("FS_IOC_SETFLAGS", 0x40086602), ("FS_IOC_FSSETXATTR", 0x401C5820)):
        out["ioctl_" + name] = raw(16, FD, req, ctypes.byref(flags))
    out["socket_raw"] = raw(41, 2, 1, 0)
    import socket
    out["socket_py"] = _exc(lambda: socket.socket())
    out["io_uring_setup"] = raw(425, 1, ctypes.byref((ctypes.c_uint8 * 120)()))
    out["fork"] = raw(57)
    out["vfork"] = raw(58)
    out["execve"] = raw(59, ctypes.c_char_p(b"/bin/true"), None, None)
    out["execveat"] = raw(322, FD, ctypes.c_char_p(b""), None, None, 0x1000)
    out["memfd_create"] = raw(319, ctypes.c_char_p(b"x"), 0)
    out["clone3"] = raw(435, None, 0)
    """
    alive = assert_alive(run_child(action, pre=pre))
    for name in ("F_SETOWN", "F_SETSIG", "F_SETOWN_EX", "F_SETLEASE", "F_NOTIFY", "F_SETLK",
                 "F_GETLK", "F_OFD_SETLK"):
        assert alive["fcntl_" + name] == [-1, 1], name
    assert alive["fcntl_F_GETFL"] is True and alive["fcntl_F_DUPFD_CLOEXEC"] is True
    for name in ("FIOSETOWN", "SIOCSPGRP", "FS_IOC_SETFLAGS", "FS_IOC_FSSETXATTR"):
        assert alive["ioctl_" + name] == [-1, 25], name
    assert alive["socket_raw"] == [-1, 13]
    assert alive["socket_py"] == 13
    assert alive["io_uring_setup"] == [-1, 13]
    for name in ("fork", "vfork", "execve", "execveat", "memfd_create"):
        assert alive[name] == [-1, 1], name
    assert alive["clone3"] == [-1, 38]


# --- #20 番号表の外 -----------------------------------------------------------

@pytest.mark.parametrize("nr", [468, 469, 470, 471, 600, 1000, 0x3FFFFFFF, 0x40000000 + 39])
def test_unknown_numbers_are_killed(nr):
    assert_sigsys(run_child(f"out['r'] = raw({nr}, 0, 0, 0, 0, 0)\n"))


_DEFAULT_ALLOW = "dataclasses.replace(S.ALLOW6_TABLE, default_action=S.RET_ALLOW)"


@pytest.mark.parametrize("nr", [471, 600, 1000, 0x3FFFFFFF])
def test_table_end_row_kills_under_default_allow_control(nr):
    assert_sigsys(run_child(f"out['r'] = raw({nr}, 0, 0, 0, 0, 0)\n",
                            apply=_control_apply(_DEFAULT_ALLOW)))


def test_default_allow_control_really_lets_unlisted_numbers_reach_kernel():
    # 上の対照が「何でも殺す filter」になっていないことの確認。470 (listns) は kernel に届く
    alive = assert_alive(run_child("out['r'] = raw(470, 0, 0, 0, 0)\n",
                                   apply=_control_apply(_DEFAULT_ALLOW)))
    assert alive["r"][0] == -1 and alive["r"][1] not in (0, 38)


# --- #25 errno を返せない API は SIGSYS ---------------------------------------

_KILLED_APIS = {
    "os.sync": "os.sync()",
    "os.getppid": "os.getppid()",
    "os.getuid": "os.getuid()",
    "os.geteuid": "os.geteuid()",
    "os.getgid": "os.getgid()",
    "os.getegid": "os.getegid()",
    "os.getpgrp": "os.getpgrp()",
    "os.times": "os.times()",
    "signal.alarm": "signal.alarm(0)",
    "sysinfo": "os.sysconf('SC_PHYS_PAGES')",
    "os.getcwd": "os.getcwd()",
}


@pytest.mark.parametrize("api", sorted(_KILLED_APIS))
def test_errno_less_apis_kill_the_process(api):
    assert_sigsys(run_child(f"out['v'] = repr({_KILLED_APIS[api]})\n"))


# --- #29 自分宛て SIGSYS は EPERM ----------------------------------------------

def test_self_sigsys_is_eperm_and_process_survives():
    action = """
    pid = os.getpid()
    out["raise_signal"] = _exc(lambda: signal.raise_signal(signal.SIGSYS))
    out["os_kill"] = _exc(lambda: os.kill(pid, signal.SIGSYS))
    out["raw_kill"] = raw(62, pid, 31)
    out["raw_tgkill"] = raw(234, pid, pid, 31)
    out["raw_kill_0"] = raw(62, 0, 31)
    out["raw_kill_group"] = raw(62, -pid, 31)
    out["raw_kill_all"] = raw(62, -1, 31)
    """
    alive = assert_alive(run_child(action))
    assert alive["raise_signal"] == 1 and alive["os_kill"] == 1
    for key in ("raw_kill", "raw_tgkill", "raw_kill_0", "raw_kill_group", "raw_kill_all"):
        assert alive[key] == [-1, 1], key


def test_abort_still_raises_sigabrt():
    rc, _alive, _err, _pid = run_child("os.abort()\n")
    assert rc == -signal.SIGABRT


# --- #21 / #31 正常 workload と clone3 fallback の差分 ---------------------------

_WORKLOAD = """
import numpy as np
import pandas as pd
import agentic_fx.core.plugin_contract  # noqa: F401
df = pd.DataFrame({"c": np.arange(500.0)},
                  index=pd.date_range("2026-01-01", periods=500, freq="h", tz="UTC"))
out["tz"] = str(df.index.tz_convert("America/New_York")[0])
out["roll"] = float(df["c"].rolling(5).mean().iloc[-1])
a = np.ones((600, 600))
out["dgemm"] = float((a @ a)[0, 0])
box = []
ts = [threading.Thread(target=lambda: box.append(1)) for _ in range(3)]
[t.start() for t in ts]; [t.join() for t in ts]
out["threads"] = len(box)
"""

_BLAS4 = {"OPENBLAS_NUM_THREADS": "4"}


def test_normal_workload_completes_with_openblas_threads():
    rc, alive, err, pid = run_child(_WORKLOAD, env=_BLAS4)
    assert rc == 0 and alive is not None, (rc, err[-2000:])
    assert alive == {"tz": "2025-12-31 19:00:00-05:00", "roll": 497.0, "dgemm": 600.0,
                     "threads": 3}
    _assert_journal_errno_set(pid, allowed={435})


def test_clone3_back_to_kill_makes_blas_and_threading_die():
    table = "dataclasses.replace(S.ALLOW6_TABLE, fallback_enosys=())"
    assert_sigsys(run_child(_WORKLOAD, apply=_control_apply(table), env=_BLAS4))


def test_clone3_back_to_kill_makes_plain_threading_die():
    table = "dataclasses.replace(S.ALLOW6_TABLE, fallback_enosys=())"
    action = "t = threading.Thread(target=lambda: None); t.start(); t.join()\n"
    assert_sigsys(run_child(action, apply=_control_apply(table)))
    assert_alive(run_child(action))


_EXAMPLES_RUN = """
import numpy as np
import pandas as pd
import math
n = 600
idx = pd.date_range("2026-01-05", periods=n, freq="h", tz="UTC")
close = pd.Series([1.10 + 0.01 * math.sin(i * 0.07) + 0.0003 * (i % 17) for i in range(n)],
                  index=idx)
df = pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]), "high": close + 0.0007,
                   "low": close - 0.0006, "close": close, "volume": 100.0}, index=idx)
res = {}
for name, src in SOURCES.items():
    ns = {"__name__": "indicator_" + name}
    exec(compile(src, name + "/plugin.py", "exec", dont_inherit=True), ns)
    got = ns["compute"](df.copy(), {})
    row = {}
    for key, val in sorted(got.items()):
        if isinstance(val, pd.Series):
            v = val.dropna()
            row[key] = [len(v), repr(float(v.iloc[-1])) if len(v) else None]
        else:
            row[key] = repr(float(val))
    res[name] = row
out["examples"] = res
"""


def _example_sources() -> dict[str, str]:
    out = {}
    for d in sorted(EXAMPLES.iterdir()):
        cfg = d / "config.yaml"
        if cfg.is_file() and re.search(r"^kind:\s*indicator\s*$", cfg.read_text(), re.MULTILINE):
            out[d.name] = (d / "plugin.py").read_text()
    return out


def test_example_indicators_match_unfiltered_run():
    sources = _example_sources()
    assert len(sources) >= 8
    pre = f"SOURCES = {sources!r}\n"
    filtered = assert_alive(run_child(_EXAMPLES_RUN, pre=pre, env=_BLAS4))
    unfiltered = assert_alive(run_child(_EXAMPLES_RUN, pre=pre, apply="", env=_BLAS4))
    assert filtered["examples"] == unfiltered["examples"]
    assert set(filtered["examples"]) == set(sources)


# --- kernel log (読めるときだけ) ------------------------------------------------

def _journal_records(pid: int, since: str) -> list[tuple[int, str]]:
    r = subprocess.run(["journalctl", "-k", "--no-pager", "-o", "cat", "--since", since,
                        "-g", f"type=1326.* pid={pid} "],
                       capture_output=True, text=True, timeout=20)
    recs = []
    for line in r.stdout.splitlines():
        m = re.search(r" syscall=(\d+) .*?code=(0x[0-9a-f]+)", line)
        if m:
            recs.append((int(m.group(1)), m.group(2)))
    return recs


def _assert_journal_errno_set(pid: int, *, allowed: set[int]) -> None:
    """LOG flag で記録された errno 判定の syscall 集合が `allowed` に収まること。

    kernel log が読めない host では何もしない (分類や受入の必須条件にしない)。
    printk の間引きで行が落ちることがあるので「含まれる」側だけを見る。
    """
    if S.check_kernel_log() != S.KERNEL_LOG_READABLE:
        return
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 120))
    recs = []
    for _ in range(10):
        recs = _journal_records(pid, since)
        if recs:
            break
        time.sleep(0.3)
    assert all(code != "0x80000000" for _nr, code in recs), recs
    assert {nr for nr, code in recs if code == "0x50000"} <= allowed, recs


def test_kill_is_recorded_in_kernel_log_when_readable():
    if S.check_kernel_log() != S.KERNEL_LOG_READABLE:
        pytest.skip("kernel log is not readable on this host (diagnostic only)")
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 5))
    result = run_child("os.sync()\n")
    assert_sigsys(result)
    pid = result[3]
    recs = []
    for _ in range(20):
        recs = _journal_records(pid, since)
        if recs:
            break
        time.sleep(0.3)
    if not recs:
        pytest.skip("kernel log rate limit dropped the record")
    assert (162, "0x80000000") in recs
