"""隔離段を通した worker で、表の外の syscall・process 生成・socket・signal を確かめる。

1 子 1 syscall で SIGSYS を見る (死んだ子は以後の probe を返せない)。errno で返る probe は
1 子にまとめる。判定は wait status と子の返す errno で行い、kernel log を使わない。
"""
from __future__ import annotations

import errno
import os
import socket
import uuid
from pathlib import Path

import pytest

from ._worker_isolation_support import (
    SIGSYS,
    cfg_for,
    copy_plugin,
    needs_sandbox,
    run_child,
)

_RAW = """
import ctypes
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
def raw(nr, *args):
    conv = [ctypes.c_long(a) if isinstance(a, int) else a for a in args]
    ctypes.set_errno(0)
    r = libc.syscall(ctypes.c_long(nr), *conv)
    return [r, ctypes.get_errno() if r == -1 else 0]
"""

# 1 session 1 syscall で SIGSYS になる呼び出し (target は隔離外の一時 file の path)
_KILLED = {
    "shmget": "raw(29, 0, 4096, 0o1600)",
    "semget": "raw(64, 0, 1, 0o1600)",
    "msgget": "raw(68, 0, 0o1600)",
    "mq_open": "raw(240, ctypes.c_char_p(b'afx_t3'), 0o100, 0o600, 0)",
    "setpriority": "raw(141, 0, 0, 5)",
    "sched_setaffinity": "raw(203, 0, 8, ctypes.byref(ctypes.c_ulong(1)))",
    "pidfd_open": "raw(434, cfg['parent_pid'], 0)",
    "kcmp": "raw(312, os.getpid(), cfg['parent_pid'], 0, 0, 0)",
    "prctl_set_ptracer": "raw(157, 0x59616d61, cfg['parent_pid'], 0, 0, 0)",
    "ptrace": "raw(101, 16, cfg['parent_pid'], 0, 0)",
    "unshare": "raw(272, 0x00020000)",
    "personality": "raw(135, 0xffffffff)",
    "fsopen": "raw(430, ctypes.c_char_p(b'tmpfs'), 0)",
    "chmod": "raw(90, ctypes.c_char_p(cfg['target'].encode()), 0o777)",
    "fchmodat2": "raw(452, -100, ctypes.c_char_p(cfg['target'].encode()), 0o777, 0)",
    "chown": "raw(92, ctypes.c_char_p(cfg['target'].encode()), 0, 0)",
    "utimensat": "raw(280, -100, ctypes.c_char_p(cfg['target'].encode()), 0, 0)",
    "setxattr": "raw(188, ctypes.c_char_p(cfg['target'].encode()), ctypes.c_char_p(b'user.afx'),"
                " ctypes.c_char_p(b'1'), 1, 0)",
    "file_setattr": "raw(469, -100, ctypes.c_char_p(cfg['target'].encode()), 0, 0, 0)",
    "keyctl": "raw(250, 0, -3, 0)",
    "add_key": "raw(248, ctypes.c_char_p(b'user'), ctypes.c_char_p(b'afx'),"
               " ctypes.c_char_p(b'x'), 1, -3)",
    "request_key": "raw(249, ctypes.c_char_p(b'user'), ctypes.c_char_p(b'afx'), 0, -3)",
    "os_sync": "os.sync()",
    "os_getcwd": "os.getcwd()",
    "getppid": "os.getppid()",
    "getuid": "os.getuid()",
    "geteuid": "os.geteuid()",
    "getgid": "os.getgid()",
    "getegid": "os.getegid()",
    "getpgrp": "os.getpgrp()",
    "times": "os.times()",
    "alarm": "raw(37, 0)",
    "sysinfo": "raw(99, ctypes.byref(ctypes.create_string_buffer(256)))",
}


def _ipc_counts() -> tuple[int, ...]:
    out = []
    for name in ("shm", "sem", "msg"):
        with open(f"/proc/sysvipc/{name}", encoding="ascii") as f:
            out.append(len(f.readlines()))
    return tuple(out)


def _meta(p: Path) -> tuple:
    st = p.stat()
    return (st.st_mode, st.st_uid, st.st_mtime_ns, st.st_atime_ns, os.listxattr(p))


@needs_sandbox
@pytest.mark.parametrize("name", sorted(_KILLED))
def test_disallowed_syscall_kills_the_isolated_worker_without_side_effects(tmp_path, name):
    main = copy_plugin("rsi", tmp_path / "plugins")
    target = tmp_path / "target.txt"
    target.write_text("t")
    os.utime(target, ns=(1_000_000_000, 1_000_000_000))
    before_meta, before_ipc = _meta(target), _ipc_counts()
    res = run_child(cfg_for(main, post=_RAW + _KILLED[name] + "\n", target=str(target),
                            parent_pid=os.getpid()))
    assert res.phase("isolated") is not None, res.stderr
    assert res.rc == -SIGSYS, (res.rc, res.lines, res.stderr)
    assert res.phase("done") is None
    assert _meta(target) == before_meta
    assert _ipc_counts() == before_ipc


_ERRNO_PROBES = """
import fcntl, signal, socket, threading
def _exc(fn):
    try:
        fn()
        return "ok"
    except OSError as e:
        return e.errno
fd = os.open("/dev/urandom", os.O_RDONLY)
r = {}
r["fcntl_setown"] = raw(72, fd, 8, os.getpid())[1]
r["fcntl_setsig"] = raw(72, fd, 10, 10)[1]
r["fcntl_setlease"] = raw(72, fd, 1024, 0)[1]
r["fcntl_getfl"] = raw(72, fd, 3, 0)[1]
r["ioctl_fiosetown"] = raw(16, fd, 0x8901, 0)[1]
r["ioctl_fs_setflags"] = raw(16, fd, 0x40086602, 0)[1]
def _fork():
    pid = os.fork()
    if pid == 0:
        os._exit(0)
r["fork"] = _exc(_fork)
r["raw_fork"] = raw(57)[1]
r["execve"] = _exc(lambda: os.execv("/usr/bin/true", ["true"]))
r["memfd_create"] = raw(319, ctypes.c_char_p(b"afx"), 0)[1]
r["execveat"] = raw(322, fd, ctypes.c_char_p(b""), 0, 0, 0x1000)[1]
r["socket"] = _exc(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM))
r["socket_unix"] = _exc(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
r["io_uring_setup"] = raw(425, 1, ctypes.byref(ctypes.create_string_buffer(120)))[1]
r["clone3"] = raw(435, 0, 0)[1]
r["kill_other"] = raw(62, cfg["parent_pid"], 0)[1]
_new = (ctypes.c_ulong * 2)(1, 1)
r["prlimit_other"] = raw(302, cfg["parent_pid"], 7, ctypes.byref(_new), 0)[1]
r["prlimit_self"] = raw(302, 0, 7, 0, ctypes.byref(ctypes.create_string_buffer(16)))[1]
got = []
signal.signal(signal.SIGUSR1, lambda *a: got.append("usr1"))
r["kill_self_usr1"] = _exc(lambda: os.kill(os.getpid(), signal.SIGUSR1))
r["usr1_delivered"] = got == ["usr1"]
r["raise_signal_sigsys"] = _exc(lambda: signal.raise_signal(signal.SIGSYS))
r["os_kill_self_sigsys"] = _exc(lambda: os.kill(os.getpid(), signal.SIGSYS))
r["raw_kill_self_sigsys"] = raw(62, os.getpid(), 31)[1]
r["raw_tgkill_self_sigsys"] = raw(234, os.getpid(), os.getpid(), 31)[1]
parts = []
ths = [threading.Thread(target=lambda k=k: parts.append(k)) for k in range(4)]
for t in ths:
    t.start()
for t in ths:
    t.join()
r["threads"] = sorted(parts)
os.close(fd)
out.update(r)
"""

# 隔離後に thread を作るので、per-uid の RLIMIT_NPROC をこの子だけ hard 上限まで緩める
# (512 では同じ uid の既存 task 数次第で thread を作れない)
_NO_NPROC_CAP = ("import resource\n"
                 "wi._NPROC_CAP = resource.getrlimit(resource.RLIMIT_NPROC)[1]\n")


@needs_sandbox
def test_named_errno_self_signals_and_threads_under_isolation(tmp_path):
    main = copy_plugin("rsi", tmp_path / "plugins")
    import resource
    nofile_before = resource.getrlimit(resource.RLIMIT_NOFILE)
    res = run_child(cfg_for(main, mid=_NO_NPROC_CAP, post=_RAW + _ERRNO_PROBES,
                            parent_pid=os.getpid()))
    assert res.rc == 0, res.stderr
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == nofile_before
    o = res.out
    E = errno
    assert (o["fcntl_setown"], o["fcntl_setsig"], o["fcntl_setlease"]) == (E.EPERM,) * 3
    assert o["fcntl_getfl"] == 0
    assert (o["ioctl_fiosetown"], o["ioctl_fs_setflags"]) == (E.ENOTTY, E.ENOTTY)
    assert (o["fork"], o["raw_fork"], o["execve"], o["memfd_create"], o["execveat"]) \
        == (E.EPERM,) * 5
    assert (o["socket"], o["socket_unix"], o["io_uring_setup"]) == (E.EACCES,) * 3
    assert o["clone3"] == E.ENOSYS
    assert (o["kill_other"], o["prlimit_other"], o["prlimit_self"]) == (E.EPERM, E.EPERM, 0)
    assert (o["kill_self_usr1"], o["usr1_delivered"]) == ("ok", True)
    assert (o["raise_signal_sigsys"], o["os_kill_self_sigsys"], o["raw_kill_self_sigsys"],
            o["raw_tgkill_self_sigsys"]) == (E.EPERM,) * 4
    assert o["threads"] == [0, 1, 2, 3]


# --- clone3 の fallback の差分 -------------------------------------------------------

_BLAS_AND_THREADS = """
import threading
import numpy as np
a = np.arange(256 * 256, dtype=np.float64).reshape(256, 256) % 7
out["trace"] = float(np.trace(a @ a.T))
parts = []
ths = [threading.Thread(target=lambda k=k: parts.append(k)) for k in range(4)]
for t in ths:
    t.start()
for t in ths:
    t.join()
out["threads"] = sorted(parts)
"""

_CLONE3_KILL = ("import dataclasses\nfrom agentic_fx.core import seccomp as S\n"
                "S.ALLOW6_TABLE = dataclasses.replace(S.ALLOW6_TABLE, fallback_enosys=())\n")


@needs_sandbox
@pytest.mark.parametrize("clone3", ["enosys", "kill"])
def test_clone3_fallback_is_what_lets_blas_and_threading_run(tmp_path, clone3):
    main = copy_plugin("rsi", tmp_path / "plugins")
    mid = _NO_NPROC_CAP + (_CLONE3_KILL if clone3 == "kill" else "")
    res = run_child(cfg_for(main, mid=mid, post=_BLAS_AND_THREADS),
                    env={"OPENBLAS_NUM_THREADS": "4"})
    if clone3 == "enosys":
        assert res.rc == 0, res.stderr
        assert res.out["threads"] == [0, 1, 2, 3]
    else:
        # OpenBLAS は numpy の import 時に thread を作るので、runtime import 自己試験で死ぬ
        assert res.rc == -SIGSYS
        assert res.phase("done") is None


# --- socket: pathname UDS は seccomp と組んで初めて閉じる ------------------------------

_NET_PROBE = """
import socket
def attempt(family, addr):
    try:
        s = socket.socket(family, socket.SOCK_STREAM)
    except OSError as e:
        return ["socket", e.errno]
    try:
        s.connect(addr)
        return ["connected", 0]
    except OSError as e:
        return ["connect", e.errno]
    finally:
        s.close()
out["tcp"] = attempt(socket.AF_INET, ("127.0.0.1", cfg["tcp_port"]))
out["pathname_uds"] = attempt(socket.AF_UNIX, cfg["uds_path"])
out["abstract_uds"] = attempt(socket.AF_UNIX, "\\0" + cfg["abstract_name"])
"""

_LANDLOCK_ONLY = ("from agentic_fx.core import seccomp as S\n"
                  "S.apply_allow6 = lambda: None\n")


def _count_accepted(sock: socket.socket) -> int:
    sock.setblocking(False)
    n = 0
    while True:
        try:
            conn, _ = sock.accept()
        except BlockingIOError:
            return n
        conn.close()
        n += 1


@needs_sandbox
@pytest.mark.parametrize("profile", ["full", "landlock_only"])
def test_tcp_and_unix_sockets_are_unreachable(tmp_path, profile):
    main = copy_plugin("rsi", tmp_path / "plugins")
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.bind(("127.0.0.1", 0))
    tcp.listen(8)
    uds_path = str(tmp_path / "s.sock")
    uds = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    uds.bind(uds_path)
    uds.listen(8)
    abstract_name = f"afx-t3-{uuid.uuid4().hex}"
    abstract = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    abstract.bind("\0" + abstract_name)
    abstract.listen(8)
    try:
        res = run_child(cfg_for(main, mid=_LANDLOCK_ONLY if profile == "landlock_only" else "",
                                post=_NET_PROBE, tcp_port=tcp.getsockname()[1],
                                uds_path=uds_path, abstract_name=abstract_name))
        hits = {k: _count_accepted(s) for k, s in
                (("tcp", tcp), ("pathname_uds", uds), ("abstract_uds", abstract))}
    finally:
        for s in (tcp, uds, abstract):
            s.close()
    o = res.out
    if profile == "full":
        assert o == {"tcp": ["socket", errno.EACCES], "pathname_uds": ["socket", errno.EACCES],
                     "abstract_uds": ["socket", errno.EACCES]}
        assert hits == {"tcp": 0, "pathname_uds": 0, "abstract_uds": 0}
    else:
        # 対照: Landlock (FS + network + scope) だけでは pathname UDS に届く
        assert o["tcp"] == ["connect", errno.EACCES]
        assert o["abstract_uds"] == ["connect", errno.EPERM]
        assert o["pathname_uds"] == ["connected", 0]
        assert hits == {"tcp": 0, "pathname_uds": 1, "abstract_uds": 0}
