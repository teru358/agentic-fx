"""nested Landlock のテストで使い捨ての子 process 側に走る小さな helper。

`python -P -B <this file> '<json cfg>'` で起こされ、cfg["scenario"] の観測を
して結果を JSON 1 行で stdout に書く。Landlock は不可逆なので pytest 本体では
なくこの子に掛ける。kernel の応答 (errno) をそのまま記録し、判定は test 側で
する。
"""
from __future__ import annotations

import ctypes
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

CFG = json.loads(sys.argv[1])
SLEEP = "/usr/bin/sleep"

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def probe(fn) -> dict:
    try:
        return {"ok": fn()}
    except OSError as e:
        return {"errno": e.errno}


def apply_nested() -> dict:
    from agentic_fx.core import landlock_nested as N
    rs = N.apply_nested_landlock()
    return {"handled_net": rs.handled_net, "handled_scope": rs.handled_scope}


def apply_outer_scope_only() -> None:
    """worker 役に掛ける簡単な外側 domain (signal と abstract UDS の scope だけ)。
    nested の module を使わず、別の domain であることを保つため直接組む。"""
    attr = struct.pack("QQQ", 0, 0, 0b11)
    buf = ctypes.create_string_buffer(attr, len(attr))
    rfd = _libc.syscall(444, buf, ctypes.c_size_t(len(attr)), ctypes.c_uint32(0))
    if rfd < 0:
        raise OSError(ctypes.get_errno(), "outer create")
    if _libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "outer nnp")
    if _libc.syscall(446, ctypes.c_int(rfd), ctypes.c_uint32(0)) != 0:
        raise OSError(ctypes.get_errno(), "outer restrict")
    os.close(rfd)


def maybe_nested() -> dict | None:
    return apply_nested() if CFG.get("nested", True) else None


def bind_probe(family: int, host: str) -> dict:
    def f():
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            s.bind((host, 0))
            return s.getsockname()[1]
        finally:
            s.close()
    return probe(f)


def connect_probe(host: str, port: int) -> dict:
    def f():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        try:
            s.connect((host, port))
            return 1
        finally:
            s.close()
    return probe(f)


def abstract_connect_probe(name: str) -> dict:
    def f():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        try:
            s.connect("\0" + name)
            return 1
        finally:
            s.close()
    return probe(f)


def kill_probe(pid: int, sig: int) -> dict:
    return probe(lambda: os.kill(pid, sig) or 0)


def killpg_probe(pgid: int, sig: int) -> dict:
    return probe(lambda: os.killpg(pgid, sig) or 0)


def pidfd_signal_probe(pid: int, sig: int) -> dict:
    # この interpreter の build は os.pidfd_open を持たないことがあるので syscall で呼ぶ
    def f():
        fd = _libc.syscall(434, ctypes.c_int(pid), ctypes.c_uint(0))
        if fd < 0:
            raise OSError(ctypes.get_errno(), "pidfd_open")
        try:
            rc = _libc.syscall(424, ctypes.c_int(fd), ctypes.c_int(sig),
                               None, ctypes.c_uint(0))
            if rc != 0:
                raise OSError(ctypes.get_errno(), "pidfd_send_signal")
            return 0
        finally:
            os.close(fd)
    return probe(f)


# ---------------------------------------------------------------- scenarios

def sc_probes() -> dict:
    """外向きの各操作を一度ずつ試す (nested の有無を cfg で切り替え、対照に使う)。"""
    out = {"applied": maybe_nested()}
    out["bind4"] = bind_probe(socket.AF_INET, "127.0.0.1")
    out["bind6"] = bind_probe(socket.AF_INET6, "::1")
    if "port" in CFG:
        out["connect"] = connect_probe("127.0.0.1", CFG["port"])
    if "target" in CFG:
        out["kill0"] = kill_probe(CFG["target"], 0)
    if "abstract" in CFG:
        out["abstract"] = abstract_connect_probe(CFG["abstract"])
    return out


def sc_listen_nobind() -> dict:
    """bind 無しの listen。kernel が自動で port を割り当てるので、その port を
    先に知らせ、外から来る接続を 1 本待つ。"""
    applied = maybe_nested()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.listen(1)
    except OSError as e:
        return {"applied": applied, "listen": {"errno": e.errno}}
    port = s.getsockname()[1]
    emit({"port": port})
    s.settimeout(10)
    try:
        conn, _ = s.accept()
        conn.close()
        accepted = 1
    except OSError as e:
        accepted = {"errno": e.errno}
    return {"applied": applied, "listen": {"ok": port}, "accepted": accepted}


def sc_signal_outside() -> dict:
    applied = maybe_nested()
    pid, pgid = CFG["target"], CFG["target_pgid"]
    return {
        "applied": applied,
        "kill0": kill_probe(pid, 0),
        "kill_term": kill_probe(pid, signal.SIGTERM),
        "killpg0": killpg_probe(pgid, 0),
        "killpg_term": killpg_probe(pgid, signal.SIGTERM),
        "pidfd_term": pidfd_signal_probe(pid, signal.SIGTERM),
    }


def sc_signal_inside() -> dict:
    applied = maybe_nested()
    # 子 (domain を継承) へ signal 0 と SIGTERM
    g1 = subprocess.Popen([SLEEP, "60"])
    k0 = kill_probe(g1.pid, 0)
    kt = kill_probe(g1.pid, signal.SIGTERM)
    g1_rc = g1.wait(timeout=10)
    # 自 pgid への killpg。自分は SIGTERM を無視し、同じ pgid の子だけが死ぬ
    g2 = subprocess.Popen([SLEEP, "60"])
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    kpg = killpg_probe(os.getpgrp(), signal.SIGTERM)
    g2_rc = g2.wait(timeout=10)
    return {"applied": applied, "kill0": k0, "kill_term": kt, "g1_rc": g1_rc,
            "killpg_term": kpg, "g2_rc": g2_rc}


def sc_launcher_role() -> None:
    """worker 役の子として起こされ、nested を掛けて setsid し、孫を起こしてから
    worker 役への signal を試す。結果を 1 行書いて殺されるまで待つ。"""
    os.setsid()
    applied = apply_nested()
    g = subprocess.Popen([SLEEP, "60"])
    wpid = os.getppid()
    emit({"applied": applied, "pid": os.getpid(), "pgid": os.getpgrp(),
          "grandchild": g.pid,
          "to_worker_kill0": kill_probe(wpid, 0),
          "to_worker_term": kill_probe(wpid, signal.SIGTERM)})
    time.sleep(60)
    os._exit(3)


def sc_worker_role() -> dict:
    apply_outer_scope_only()
    # 孫が孤児になったとき自分が reap できるように
    if _libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "subreaper")
    cfg = dict(CFG, scenario="launcher_role")
    lp = subprocess.Popen([sys.executable, "-P", "-B", __file__, json.dumps(cfg)],
                          stdout=subprocess.PIPE, text=True)
    rep = json.loads(lp.stdout.readline())
    kpg = killpg_probe(rep["pgid"], signal.SIGKILL)
    reaped: dict[int, int] = {}
    deadline = time.monotonic() + 10
    while len(reaped) < 2 and time.monotonic() < deadline:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            time.sleep(0.02)
            continue
        reaped[pid] = (-os.WTERMSIG(status) if os.WIFSIGNALED(status)
                       else os.WEXITSTATUS(status))
    lp.returncode = reaped.get(lp.pid)
    return {"launcher": rep, "killpg_kill": kpg,
            "launcher_rc": reaped.get(rep["pid"]),
            "grandchild_rc": reaped.get(rep["grandchild"])}


def sc_abstract() -> dict:
    """外が bind した abstract socket への接続と、domain 内で bind した
    abstract socket への同じ domain の子からの接続。"""
    applied = maybe_nested()
    outside = abstract_connect_probe(CFG["abstract"])
    own = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    own.bind("\0" + CFG["own_abstract"])
    own.listen(1)
    pid = os.fork()
    if pid == 0:
        r = abstract_connect_probe(CFG["own_abstract"])
        os._exit(0 if "ok" in r else 100 + r["errno"])
    own.settimeout(10)
    try:
        conn, _ = own.accept()
        conn.close()
        own_accepted = 1
    except OSError as e:
        own_accepted = {"errno": e.errno}
    _, status = os.waitpid(pid, 0)
    return {"applied": applied, "outside": outside,
            "own_child_exit": os.waitstatus_to_exitcode(status),
            "own_accepted": own_accepted}


def sc_tsync() -> dict:
    """restrict より前に立てた thread にも domain が及ぶか。"""
    go = threading.Event()
    res: dict = {}

    def worker() -> None:
        go.wait(10)
        res["bind4"] = bind_probe(socket.AF_INET, "127.0.0.1")
        res["kill0"] = kill_probe(CFG["target"], 0)
        with open(f"/proc/self/task/{threading.get_native_id()}/status") as f:
            res["nnp"] = [ln.split()[1] for ln in f if ln.startswith("NoNewPrivs:")][0]

    t = threading.Thread(target=worker)
    t.start()
    applied = maybe_nested()
    go.set()
    t.join(10)
    return {"applied": applied, "thread": res,
            "main_bind4": bind_probe(socket.AF_INET, "127.0.0.1")}


def main() -> None:
    sc = CFG["scenario"]
    if sc == "launcher_role":
        sc_launcher_role()
        return
    fn = {"probes": sc_probes, "listen_nobind": sc_listen_nobind,
          "signal_outside": sc_signal_outside, "signal_inside": sc_signal_inside,
          "worker_role": sc_worker_role, "abstract": sc_abstract,
          "tsync": sc_tsync}[sc]
    try:
        emit({"result": fn()})
    except Exception as e:  # 失敗の形を test 側で読めるように残す
        reason = getattr(e, "reason", None)
        emit({"error": type(e).__name__, "reason": reason, "detail": str(e)})


if __name__ == "__main__":
    main()
