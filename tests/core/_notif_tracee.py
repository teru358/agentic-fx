"""connect supervisor の試験用: seccomp filter の下で connect を打つ子 process と、その操作役。

子側 (このファイルを script として起こす):

- `factory <ctl_fd>`: filter を持たない小さな process。要求ごとに fork して tracee を作る。
  tracee は自分で no_new_privs を立て、connect だけを USER_NOTIF にする最小 filter を
  `NEW_LISTENER | WAIT_KILLABLE_RECV` で掛け、listener を SCM_RIGHTS で親へ送ってから、
  JSON の命令を受けて connect などを実行する。RLIMIT_CORE は 0。
- `worker <scenario> <json>`: worker 役。tracee を fork してから supervisor を動かし、
  結果の JSON を 1 行出して終わる (STUCK のように thread を join できない場面用)。

親側 (試験から import する): `Factory`、`Tracee`、`Gate`。
"""
from __future__ import annotations

import ctypes
import errno
import json
import os
import resource
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import warnings

HERE = os.path.abspath(__file__)

_NR_SECCOMP = 317
_NR_CONNECT = 42
_SECCOMP_SET_MODE_FILTER = 1
_F_NEW_LISTENER = 1 << 3
_F_WAIT_KILLABLE_RECV = 1 << 5
_RET_ALLOW = 0x7FFF0000
_RET_USER_NOTIF = 0x7FC00000
_RET_KILL_PROCESS = 0x80000000
_AUDIT_ARCH_X86_64 = 0xC000003E
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_CHILD_SUBREAPER = 36
# 試験の connect で使う未 map の address (page 0)
UNMAPPED_ADDR = 8


# --- 子側 ---------------------------------------------------------------------

def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.connect.restype = ctypes.c_int
    libc.connect.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    return libc


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8),
                ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(_SockFilter))]


def install_connect_notif(libc) -> int:
    """connect だけを USER_NOTIF にする filter を掛けて listener を返す。"""
    prog = [(0x20, 0, 0, 4), (0x15, 1, 0, _AUDIT_ARCH_X86_64), (0x06, 0, 0, _RET_KILL_PROCESS),
            (0x20, 0, 0, 0), (0x15, 0, 1, _NR_CONNECT), (0x06, 0, 0, _RET_USER_NOTIF),
            (0x06, 0, 0, _RET_ALLOW)]
    arr = (_SockFilter * len(prog))(*[_SockFilter(*x) for x in prog])
    fp = _SockFprog(len(prog), ctypes.cast(arr, ctypes.POINTER(_SockFilter)))
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "no_new_privs")
    rc = libc.syscall(ctypes.c_long(_NR_SECCOMP), ctypes.c_uint(_SECCOMP_SET_MODE_FILTER),
                      ctypes.c_uint(_F_NEW_LISTENER | _F_WAIT_KILLABLE_RECV), ctypes.byref(fp))
    if rc < 0:
        raise OSError(ctypes.get_errno(), "seccomp")
    return int(rc)


def sockaddr(family: str, host: str = "", port: int = 0, scope_id: int = 0) -> bytes:
    if family == "inet":
        return (struct.pack("<H", socket.AF_INET) + struct.pack("!H", port)
                + socket.inet_aton(host) + b"\0" * 8)
    if family == "inet6":
        return (struct.pack("<H", socket.AF_INET6) + struct.pack("!H", port) + b"\0" * 4
                + socket.inet_pton(socket.AF_INET6, host) + struct.pack("<I", scope_id))
    if family == "unspec":
        return struct.pack("<H", socket.AF_UNSPEC) + b"\0" * 14
    raise ValueError(family)


def raw_connect(libc, fd: int, sa: bytes, addrlen: int | None = None,
                unmapped: bool = False) -> int:
    alen = len(sa) if addrlen is None else addrlen
    if unmapped:
        ptr = ctypes.c_void_p(UNMAPPED_ADDR)
    else:
        buf = ctypes.create_string_buffer(max(alen, len(sa)))
        ctypes.memmove(buf, sa, len(sa))
        ptr = ctypes.cast(buf, ctypes.c_void_p)
    ctypes.set_errno(0)
    rc = libc.connect(fd, ptr, alen)
    return 0 if rc == 0 else -ctypes.get_errno()


class _TraceeState:
    def __init__(self, chan: socket.socket, libc) -> None:
        self.chan = chan
        self.libc = libc
        self.lock = threading.Lock()
        self.sigcount = 0

    def reply(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        with self.lock:
            self.chan.send(data)

    def do_connect(self, cmd: dict) -> dict:
        fam = cmd.get("family", "inet")
        sock_fam = socket.AF_INET6 if fam == "inet6" else socket.AF_INET
        if "fd" in cmd:
            fd = int(cmd["fd"])
            own = False
        else:
            flags = socket.SOCK_STREAM | (socket.SOCK_NONBLOCK if cmd.get("nonblock") else 0)
            s = socket.socket(sock_fam, flags)
            fd = s.detach()
            own = True
        out: dict = {"tid": threading.get_native_id(), "pid": os.getpid(), "fd": fd}
        try:
            sa = sockaddr(fam, cmd.get("host", "127.0.0.1"), int(cmd.get("port", 0)),
                          int(cmd.get("scope_id", 0)))
            t0 = time.monotonic()
            rc = raw_connect(self.libc, fd, sa, cmd.get("addrlen"), bool(cmd.get("unmapped")))
            out["ms"] = round((time.monotonic() - t0) * 1e3, 3)
            out["rc"] = rc
            try:
                fl = os.get_blocking(fd)
                out["nonblock_after"] = not fl
            except OSError as e:
                out["nonblock_after"] = f"err {e.errno}"
            if rc == -errno.EINPROGRESS:
                _r, w, _x = select.select([], [fd], [], 5)
                s2 = socket.socket(fileno=os.dup(fd))
                out["so_error"] = s2.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                s2.close()
            if cmd.get("twice"):
                out["second"] = raw_connect(self.libc, fd, sa)
        finally:
            if own and not cmd.get("keep"):
                os.close(fd)
        return out

    def run(self, cmd: dict) -> dict:
        op = cmd["op"]
        where = cmd.get("where", "here")
        if op == "connect" and where in ("grandchild", "orphan"):
            return self._forked_connect(cmd, where)
        if op == "connect":
            return self.do_connect(cmd)
        if op == "socket":
            kind = cmd.get("kind", "tcp")
            if kind == "udp":
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            else:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            return {"fd": s.detach()}
        if op == "swap":
            fd = int(cmd["fd"])
            to = cmd["to"]
            if to == "close":
                os.close(fd)
            elif to == "pipe":
                r, w = os.pipe()
                os.dup2(r, fd)
                os.close(r)
                os.close(w)
            elif to == "udp":
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                os.dup2(s.fileno(), fd)
                s.close()
            return {"ok": True}
        if op == "sighandler":
            def h(*_a):
                self.sigcount += 1
            signal.signal(signal.SIGUSR1, h)
            return {"ok": True}
        if op == "sigcount":
            return {"count": self.sigcount}
        if op == "ping":
            return {"pid": os.getpid()}
        return {"error": f"unknown op {op}"}

    def _forked_connect(self, cmd: dict, where: str) -> dict:
        me = os.getpid()
        r, w = os.pipe()
        p1 = os.fork()
        if p1 == 0:
            os.close(r)
            try:
                if where == "orphan":
                    p2 = os.fork()
                    if p2 != 0:
                        os._exit(0)
                    # 親が先に終わって subreaper (tracee) に引き取られるのを待つ
                    end = time.monotonic() + 5
                    while os.getppid() != me and time.monotonic() < end:
                        time.sleep(0.01)
                out = self.do_connect(cmd)
                out["ppid"] = os.getppid()
                os.write(w, json.dumps(out).encode())
            finally:
                os._exit(0)
        os.close(w)
        os.waitpid(p1, 0)
        data = b""
        while True:
            chunk = os.read(r, 65536)
            if not chunk:
                break
            data += chunk
        os.close(r)
        if where == "orphan":
            # 孤児は subreaper の tracee が回収する
            try:
                os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                pass
        out = json.loads(data) if data else {"error": "no result"}
        out["parent"] = me
        return out


def _tracee_main(chan: socket.socket, opts: dict) -> None:
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    libc = _libc()
    if opts.get("subreaper"):
        libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    lfd = install_connect_notif(libc)
    socket.send_fds(chan, [b"L"], [lfd])
    os.close(lfd)
    st = _TraceeState(chan, libc)
    while True:
        try:
            data = chan.recv(65536)
        except OSError:
            break
        if not data:
            break
        cmd = json.loads(data)
        if cmd.get("op") == "exit":
            break

        def work(cmd=cmd):
            try:
                out = st.run(cmd)
            except BaseException as e:  # noqa: BLE001
                out = {"error": repr(e)}
            out["id"] = cmd.get("id")
            try:
                st.reply(out)
            except OSError:
                pass

        if cmd.get("sync"):
            work()
        else:
            threading.Thread(target=work, daemon=True).start()
    os._exit(0)


def _factory_main(ctl_fd: int) -> None:
    ctl = socket.socket(fileno=ctl_fd)
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # tracee は自動で回収する
    while True:
        try:
            data = ctl.recv(65536)
        except OSError:
            break
        if not data:
            break
        opts = json.loads(data)
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        pid = os.fork()
        if pid == 0:
            ctl.close()
            a.close()
            try:
                _tracee_main(b, opts)
            finally:
                os._exit(1)
        b.close()
        socket.send_fds(ctl, [json.dumps({"pid": pid}).encode()], [a.fileno()])
        a.close()
    os._exit(0)


# --- 親側 ---------------------------------------------------------------------

class Tracee:
    def __init__(self, pid: int, chan: socket.socket, listener: int) -> None:
        self.pid = pid
        self.chan = chan
        self.listener = listener  # handoff するまで呼び出し側の所有
        self._next = 0
        self._results: dict[int, dict] = {}
        self.killed = False

    def start(self, op: str, **kw) -> int:
        self._next += 1
        cmd = {"op": op, "id": self._next, **kw}
        self.chan.send(json.dumps(cmd).encode())
        return self._next

    def result(self, cid: int, timeout: float = 5.0) -> dict | None:
        end = time.monotonic() + timeout
        while cid not in self._results:
            left = end - time.monotonic()
            if left <= 0:
                return None
            r, _, _ = select.select([self.chan], [], [], left)
            if not r:
                return None
            data = self.chan.recv(65536)
            if not data:
                return None
            obj = json.loads(data)
            self._results[obj["id"]] = obj
        return self._results.pop(cid)

    def call(self, op: str, timeout: float = 5.0, **kw) -> dict:
        cid = self.start(op, **kw)
        out = self.result(cid, timeout)
        assert out is not None, f"no reply for {op} {kw}"
        return out

    def kill(self) -> None:
        if not self.killed:
            self.killed = True
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def wait_gone(self, timeout: float = 5.0) -> bool:
        """thread group 全体が終わって回収されるまで待つ (leader の zombie 化だけでは
        別 thread の通知がまだ残っている)。factory が SIGCHLD を無視して自動回収する。"""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not os.path.exists(f"/proc/{self.pid}"):
                return True
            time.sleep(0.005)
        return False

    def close(self) -> None:
        self.kill()
        self.chan.close()
        if self.listener >= 0:
            os.close(self.listener)
            self.listener = -1

    def take_listener(self) -> int:
        fd, self.listener = self.listener, -1
        return fd


class Factory:
    def __init__(self) -> None:
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.ctl = a
        self.proc = subprocess.Popen(
            [sys.executable, "-I", HERE, "factory", str(b.fileno())], pass_fds=(b.fileno(),),
            stdin=subprocess.DEVNULL)
        b.close()

    def spawn(self, **opts) -> Tracee:
        self.ctl.send(json.dumps(opts).encode())
        self.ctl.settimeout(10)
        msg, fds, _f, _a = socket.recv_fds(self.ctl, 4096, 1, socket.MSG_CMSG_CLOEXEC)
        pid = json.loads(msg)["pid"]
        chan = socket.socket(fileno=fds[0])
        chan.settimeout(None)
        chan.setblocking(True)
        chan.settimeout(10)
        _m, lfds, _f, _a = socket.recv_fds(chan, 16, 1, socket.MSG_CMSG_CLOEXEC)
        chan.settimeout(None)
        return Tracee(pid, chan, lfds[0])

    def close(self) -> None:
        self.ctl.close()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)


class Gate:
    """指定の観測点で止める barrier。`count` 回目まで止め、`release` で一斉に通す。

    `only` を与えると、その notif id (0 は id を持たない点) だけで止める。"""

    def __init__(self, point: str, count: int = 1, hold: float = 10.0) -> None:
        self.point = point
        self.count = count
        self.hold = hold
        self.cond = threading.Condition()
        self.hits = 0
        self.ids: list[int] = []
        self.released = threading.Event()

    def __call__(self, point: str, nid: int) -> None:
        if point != self.point:
            return
        with self.cond:
            if self.hits >= self.count:
                return
            self.hits += 1
            self.ids.append(nid)
            self.cond.notify_all()
        self.released.wait(self.hold)

    def wait(self, n: int | None = None, timeout: float = 5.0) -> bool:
        want = self.count if n is None else n
        with self.cond:
            return self.cond.wait_for(lambda: self.hits >= want, timeout)

    def release(self) -> None:
        self.released.set()


# --- worker 役 (STUCK など join できない場面) --------------------------------

def blackhole() -> tuple[socket.socket, list[socket.socket]]:
    """accept しない backlog 0 の listener を埋め、次の SYN を落とさせる。"""
    bl = socket.socket()
    bl.bind(("127.0.0.1", 0))
    bl.listen(0)
    fillers = []
    for _ in range(4):
        f = socket.socket()
        f.setblocking(False)
        f.connect_ex(bl.getsockname())
        fillers.append(f)
    time.sleep(0.2)
    return bl, fillers


def _worker_main(scenario: str, params: dict) -> None:
    warnings.simplefilter("ignore")
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    # thread を作る前に tracee を fork する
    tp = os.fork()
    if tp == 0:
        a.close()
        try:
            _tracee_main(b, {})
        finally:
            os._exit(1)
    b.close()
    _m, lfds, _f, _x = socket.recv_fds(a, 16, 1, socket.MSG_CMSG_CLOEXEC)
    tracee = Tracee(tp, a, lfds[0])
    out: dict = {"scenario": scenario}
    try:
        out.update(_run_worker_scenario(scenario, params, tracee))
    except BaseException as e:  # noqa: BLE001
        out["exception"] = repr(e)
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    try:
        os.kill(tp, signal.SIGKILL)
        os.waitpid(tp, 0)
    except OSError:
        pass
    os._exit(0)


def _listener_open(fd: int) -> bool:
    try:
        return os.readlink(f"/proc/self/fd/{fd}") == "anon_inode:seccomp notify"
    except OSError:
        return False


def _pending(fd: int) -> bool:
    p = select.poll()
    p.register(fd, select.POLLIN)
    return any(ev & select.POLLIN for _f, ev in p.poll(0))


def _run_worker_scenario(scenario: str, params: dict, tracee: Tracee) -> dict:
    from agentic_fx.core import connect_supervisor as CS

    out: dict = {}
    lfd = tracee.take_listener()
    if scenario == "stuck_handler":
        # 複製の connect は handler の期限で打ち切られるので、止まった handler は commit の
        # 後の観測点で保持して作る (worker の終了まで返さない)
        bl, _fillers = blackhole()
        port = bl.getsockname()[1]
        faults_seen: list = []
        never = threading.Event()

        def hold(point: str, nid: int) -> None:
            if point == "after_commit":
                never.wait(60)

        sup = CS.ConnectSupervisor(CS.Destinations(None, port), on_fault=faults_seen.append,
                                   faults=CS.Faults(at=hold))
        sup.handoff(lfd)
        cid = tracee.start("connect", port=port)
        stalled = sup.wait_terminal(HANDLER_WAIT)
        out["terminal_after_watchdog"] = stalled
        out["state"] = sup.state
        f = sup.failure
        out["reason"] = f.reason if f else None
        out["on_fault"] = [x.reason for x in faults_seen]
        t0 = time.monotonic()
        out["stop_result"] = sup.stop(time.monotonic() + 1.0)
        out["stop_ms"] = round((time.monotonic() - t0) * 1e3, 1)
        out["listener_open"] = _listener_open(lfd)
        out["first_result"] = tracee.result(cid, 0.2)
        # 新しい process (tracee の子) の connect は応答されない
        cid2 = tracee.start("connect", port=port, where="grandchild")
        out["second_result"] = tracee.result(cid2, 0.5)
        out["listener_pending"] = _pending(lfd)
        out["records"] = list(sup.records)
        return out
    if scenario == "blocking_blackhole":
        # SYN を捨てる listener への blocking connect。handler は connect の期限で打ち切って
        # EPERM を返し、STUCK にはならず代行を続ける
        bl, _fillers = blackhole()
        port = bl.getsockname()[1]
        faults_seen: list = []
        sup = CS.ConnectSupervisor(CS.Destinations(None, port), on_fault=faults_seen.append)
        sup.handoff(lfd)
        t0 = time.monotonic()
        cid = tracee.start("connect", port=port)
        out["first_result"] = tracee.result(cid, CS.HANDLER_DEADLINE_SEC + 1)
        out["first_ms"] = round((time.monotonic() - t0) * 1e3, 1)
        # watchdog の期限を越えて待っても障害にならない
        out["terminal_after_watchdog"] = sup.wait_terminal(CS.HANDLER_DEADLINE_SEC + 0.5)
        out["state_after_wait"] = sup.state
        f = sup.failure
        out["reason"] = f.reason if f else None
        out["on_fault"] = [x.reason for x in faults_seen]
        # 新しい process (tracee の子) の blocking connect も同じく期限で拒否する
        cid2 = tracee.start("connect", port=port, where="grandchild")
        out["second_result"] = tracee.result(cid2, CS.HANDLER_DEADLINE_SEC + 1)
        out["handler_reasons"] = [x.get("reason") for x in sup.records
                                  if x.get("by") == "handler"]
        out["stop_result"] = sup.stop(time.monotonic() + 1.0)
        out["listener_open"] = _listener_open(lfd)
        return out
    if scenario in ("pre_register_stuck", "cleanup_deadline"):
        port = int(params["port"])
        gate_hold = threading.Event()
        first: dict = {}

        def at(point: str, nid: int) -> None:
            if point == "handler" and not first:
                first["id"] = nid
                gate_hold.wait(params.get("hold", 4.0))
            if scenario == "pre_register_stuck" and point == "after_recv" and first and \
                    nid != first.get("id"):
                raise RuntimeError("injected before register")

        sup = CS.ConnectSupervisor(CS.Destinations(None, port), faults=CS.Faults(at=at))
        sup.handoff(lfd)
        c1 = tracee.start("connect", port=port)
        deadline_wait = time.monotonic() + 2
        while not first and time.monotonic() < deadline_wait:
            time.sleep(0.005)
        if scenario == "pre_register_stuck":
            c2 = tracee.start("connect", port=port)
            out["terminal"] = sup.wait_terminal(3.5)
            out["state"] = sup.state
            out["reason"] = sup.failure.reason if sup.failure else None
            out["listener_open"] = _listener_open(lfd)
            out["second_result"] = tracee.result(c2, 0.3)
        else:
            t0 = time.monotonic()
            out["stop_result"] = sup.stop(time.monotonic() + float(params["deadline"]))
            out["stop_ms"] = round((time.monotonic() - t0) * 1e3, 1)
            out["state"] = sup.state
            out["listener_open"] = _listener_open(lfd)
            out["reason"] = sup.failure.reason if sup.failure else None
        out["first_result_early"] = tracee.result(c1, 0.1)
        return out
    raise ValueError(scenario)


# STUCK の判定は watchdog (2 秒) と drain (2 秒) の後
HANDLER_WAIT = 6.0


def run_worker(scenario: str, params: dict | None = None, timeout: float = 30.0) -> dict:
    proc = subprocess.Popen(
        [sys.executable, "-I", HERE, "worker", scenario, json.dumps(params or {})],
        stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    lines = out.decode().strip().splitlines()
    assert lines, f"worker {scenario} printed nothing (rc={proc.returncode})"
    return json.loads(lines[-1])


if __name__ == "__main__":
    # 子は core を残さない (起動直後、他の何よりも先に)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    warnings.simplefilter("ignore")
    if sys.argv[1] == "factory":
        _factory_main(int(sys.argv[2]))
    elif sys.argv[1] == "worker":
        _worker_main(sys.argv[2], json.loads(sys.argv[3]))
    else:
        raise SystemExit(f"unknown mode {sys.argv[1]}")
