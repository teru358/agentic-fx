"""subreaper の試験で CLI 役 (launcher が exec する側) と worker 役の小さな実行体。

CLI 役 (`python -I -S -c <この file の source> <mode> ...`): launcher の exec 後に
動き、孤児・zombie・prctl・clone の結果を stdout に 1 行 1 JSON で報告する。
agentic_fx は import できない (`-I -S`) ので標準ライブラリだけで書く。

worker 役 (`python <この file> worker`): 自分を subreaper にしてから launcher を起こし、
CLI を殺した後の zombie と孤児の行き先を観測する。pytest 自身を subreaper にすると
以後の試験に効いてしまうので、別 process に分けてある。
"""
import ctypes
import errno
import json
import os
import resource
import select
import signal
import sys
import threading
import time

_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_SIGCHLD = 17
_CLONE_PARENT = 0x8000
_CLONE_PARENT_SETTID = 0x100000
_SYS_CLONE = 56


def emit(**kw) -> None:
    os.write(1, (json.dumps(kw) + "\n").encode())


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    return libc


def _sleep_forever() -> None:
    time.sleep(120)


def _orphan_report(kind: str, gate_r: int) -> None:
    """double-fork の孫: 中間が回収された後で自分の ppid を報告する。"""
    if kind == "setsid":
        os.setsid()
    r, w = os.pipe()
    os.read(gate_r, 1)
    emit(role="orphan", kind=kind, pid=os.getpid(), ppid=os.getppid(),
         pipe_fd=r, pipe_ino=os.fstat(r).st_ino)
    _sleep_forever()
    os._exit(0)


def mode_orphans(kind: str, count: int) -> None:
    gate_r, gate_w = os.pipe()
    emit(role="probe", pid=os.getpid())
    mids = []
    for _ in range(count):
        mid = os.fork()
        if mid == 0:
            grand = os.fork()
            if grand == 0:
                _orphan_report(kind, gate_r)
            os._exit(0)
        mids.append(mid)
    for mid in mids:
        os.waitpid(mid, 0)
    os.write(gate_w, b"x" * count)
    emit(role="probe_done")
    _sleep_forever()


def mode_zombie() -> None:
    emit(role="probe", pid=os.getpid())
    child = os.fork()
    if child == 0:
        grand = os.fork()
        if grand == 0:
            _sleep_forever()
            os._exit(0)
        emit(role="zombie_child", zombie=os.getpid(), grandchild=grand)
        os._exit(0)
    # child を wait しない: zombie は CLI の下に残る
    _sleep_forever()


def mode_prctl() -> None:
    libc = _libc()
    res = {}
    for name, val in (("set0", 0), ("set1", 1)):
        ctypes.set_errno(0)
        rc = libc.prctl(_PR_SET_CHILD_SUBREAPER, val, 0, 0, 0)
        res[name] = errno.errorcode.get(ctypes.get_errno(), "?") if rc < 0 else "ok"
    flag = ctypes.c_int(-1)
    ctypes.set_errno(0)
    rc = libc.prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(flag), 0, 0, 0)
    res["get"] = (rc, flag.value)
    emit(role="prctl", **res)


def mode_clone() -> None:
    libc = _libc()
    res = {}
    for name, flags in (("clone_parent", _CLONE_PARENT | _SIGCHLD),
                        ("fork_like", _SIGCHLD),
                        ("parent_settid", _CLONE_PARENT_SETTID | _SIGCHLD)):
        ptid = ctypes.c_int(0)
        ctypes.set_errno(0)
        rc = libc.syscall(ctypes.c_long(_SYS_CLONE), ctypes.c_ulong(flags), ctypes.c_void_p(0),
                          ctypes.byref(ptid), ctypes.c_void_p(0), ctypes.c_ulong(0))
        if rc == 0:
            os._exit(0)
        err = ctypes.get_errno() if rc < 0 else 0
        if rc > 0:
            os.waitpid(rc, 0)
        res[name] = "ok" if rc > 0 else errno.errorcode.get(err, str(err))
    done = []
    t = threading.Thread(target=lambda: done.append(1))
    t.start()
    t.join(5)
    res["thread"] = "ok" if done else "failed"
    emit(role="clone", **res)


# --- worker 役 ---------------------------------------------------------------------

def _status(pid: int) -> dict:
    try:
        with open(f"/proc/{pid}/status") as f:
            return dict(line.split(":\t", 1) for line in f.read().splitlines() if ":\t" in line)
    except OSError:
        return {}


def _ppid(pid: int):
    v = _status(pid).get("PPid")
    return int(v) if v is not None else None


def _read_lines(fd: int, want: int, timeout: float) -> list:
    out, buf, end = [], b"", time.monotonic() + timeout
    while len(out) < want and time.monotonic() < end:
        if not select.select([fd], [], [], 0.1)[0]:
            continue
        data = os.read(fd, 4096)
        if not data:
            break
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            out.append(json.loads(line))
    return out


def _wait_until(pred, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def mode_worker(src_path: str) -> None:
    """CLI を kill した後の zombie と孤児を、subreaper の自分 (次の subreaper) が
    引き取って reap できることを観測して JSON で報告する。"""
    import subprocess

    from agentic_fx.core import cli_launch as L

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = _libc()
    assert libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    with open(src_path) as f:
        src = f.read()
    py = os.path.realpath(sys.executable)
    result = {"me": os.getpid()}
    r = L.launch_cli(
        cli_fd=L.open_cli_binary(py), argv=[py, "-I", "-S", "-c", src, "zombie"],
        env={"PATH": "/usr/bin:/bin", "LANG": "C"}, backend="claude",
        join=lambda pid: None, handoff=os.close, deadline=time.monotonic() + 10,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    result["started"] = r.started
    if not r.started:
        result["reason"] = r.reason
        print(json.dumps(result))
        return
    proc = r.launcher_proc
    cli = proc.pid
    result["cli"] = cli
    try:
        lines = _read_lines(proc.stdout.fileno(), 2, 10)
        info = {ln["role"]: ln for ln in lines}
        zombie, grand = info["zombie_child"]["zombie"], info["zombie_child"]["grandchild"]
        result["probe_pid"] = info["probe"]["pid"]
        _wait_until(lambda: _status(zombie).get("State", "").startswith("Z")
                    and _ppid(grand) == cli)
        result["before"] = {"zombie_state": _status(zombie).get("State", "")[:1],
                            "zombie_ppid": _ppid(zombie), "grand_ppid": _ppid(grand)}
        os.kill(cli, signal.SIGKILL)
        proc.wait(10)  # CLI 自身を reap
        _wait_until(lambda: _ppid(grand) == os.getpid())
        result["after"] = {"grand_ppid": _ppid(grand), "zombie_ppid": _ppid(zombie)}
        reaped = []
        _wait_until(lambda: (reaped.extend(_reap_all()) or zombie in reaped), 5)
        result["zombie_reaped"] = zombie in reaped
        result["grand_alive_child"] = _ppid(grand) == os.getpid()
    finally:
        try:
            os.kill(grand, signal.SIGKILL)
        except (OSError, NameError):
            pass
        _wait_until(lambda: (_reap_all() or True) and _ppid(locals().get("grand", 0)) is None, 5)
    print(json.dumps(result))


def _reap_all() -> list:
    out = []
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            break
        out.append(pid)
    return out


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "worker":
        mode_worker(__file__)
    elif mode == "orphans":
        mode_orphans(sys.argv[2], int(sys.argv[3]))
    elif mode == "zombie":
        mode_zombie()
    elif mode == "prctl":
        mode_prctl()
    elif mode == "clone":
        mode_clone()
