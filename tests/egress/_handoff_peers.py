"""proxy の同期 handoff の試験用: tracee 役と外部接続役の子 process、複製の取り方。

子側 (このファイルを script として `sys.executable -I` で起こす。RLIMIT_CORE は 0):

- `tracee`: TCP socket を 1 本作り、fd 番号を 1 行で返して、stdin の命令を待つ。
  親はその fd を `pidfd_getfd` で複製し、supervisor 役として connect する。
- `external <local_port> <proxy_port> <family>`: 指定の local port を SO_REUSEADDR で
  bind して proxy に接続し (family = inet / inet6)、結果を 1 行で返す。`close rst|fin` の
  命令で閉じ、`exit` で終わる。部分木の外から proxy port に来る接続の役。

親側 (試験から import する): `TraceeRole`、`ExternalRole`、`dup_from`、`free_port`。
"""
from __future__ import annotations

import ctypes
import json
import os
import resource
import socket
import struct
import subprocess
import sys

HERE = os.path.abspath(__file__)
LO = "127.0.0.1"
_NR_PIDFD_OPEN = 434
_NR_PIDFD_GETFD = 438


# --- 子側 ---------------------------------------------------------------------

def _say(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _tracee_main() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _say({"fd": s.fileno(), "pid": os.getpid()})
    for line in sys.stdin:
        cmd = line.split()
        if not cmd or cmd[0] == "exit":
            break
        if cmd[0] == "rst":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.close()
            _say({"closed": True})


def _external_main(local_port: int, proxy_port: int, family: str) -> None:
    if family == "inet6":
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        dest: tuple = ("::ffff:127.0.0.1", proxy_port)
        local: tuple = ("::ffff:127.0.0.1", local_port)
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        dest = (LO, proxy_port)
        local = (LO, local_port)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    out: dict = {}
    try:
        s.bind(local)
        s.connect(dest)
        out["connect"] = "ok"
        out["local"] = list(s.getsockname()[:2])
    except OSError as e:
        out["connect"] = e.errno
    _say(out)
    for line in sys.stdin:
        cmd = line.split()
        if not cmd or cmd[0] == "exit":
            break
        if cmd[0] == "close":
            if cmd[1] == "rst":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.close()
            _say({"closed": True})
        elif cmd[0] == "recv":
            # proxy から何か届いたかを見る (届かない・EOF・RST を区別する)
            s.settimeout(float(cmd[1]))
            try:
                data = s.recv(65536)
                _say({"recv": len(data)})
            except TimeoutError:
                _say({"recv": "timeout"})
            except OSError as e:
                _say({"recv": e.errno})


# --- 親側 ---------------------------------------------------------------------

def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def dup_from(pid: int, fdno: int) -> int:
    """他 process の fd を自分の fd table に複製する (supervisor の段 9 と同じ syscall)。"""
    libc = _libc()
    pidfd = libc.syscall(_NR_PIDFD_OPEN, ctypes.c_int(pid), ctypes.c_uint(0))
    if pidfd < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open")
    try:
        d = libc.syscall(_NR_PIDFD_GETFD, ctypes.c_int(pidfd), ctypes.c_int(fdno), ctypes.c_uint(0))
        if d < 0:
            raise OSError(ctypes.get_errno(), "pidfd_getfd")
        return int(d)
    finally:
        os.close(pidfd)


def free_port() -> int:
    s = socket.socket()
    s.bind((LO, 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _no_core() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class _Child:
    def __init__(self, *args: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-I", HERE, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1)
        self.first = self.read()

    def read(self) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        assert line, "子 process が応答しなかった"
        return json.loads(line)

    def send(self, line: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.send("exit")
            except OSError:
                pass
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        for f in (self.proc.stdin, self.proc.stdout):
            if f is not None:
                f.close()


class TraceeRole(_Child):
    """TCP socket を持つ子。`dup()` で親側に複製を作る (所有は呼び出し側)。"""

    def __init__(self) -> None:
        super().__init__("tracee")
        self.pid = int(self.first["pid"])
        self.fd = int(self.first["fd"])

    def dup(self) -> int:
        return dup_from(self.pid, self.fd)

    def rst_own_fd(self) -> None:
        """子が持つ fd を RST で閉じる (複製が残っていれば socket 自体は生きている)。"""
        self.send("rst")
        assert self.read() == {"closed": True}


class ExternalRole(_Child):
    def __init__(self, local_port: int, proxy_port: int, family: str = "inet") -> None:
        super().__init__("external", str(local_port), str(proxy_port), family)
        self.info = self.first

    def close_conn(self, mode: str) -> None:
        self.send(f"close {mode}")
        assert self.read() == {"closed": True}

    def recv(self, timeout: float) -> object:
        self.send(f"recv {timeout}")
        return self.read()["recv"]


if __name__ == "__main__":
    _no_core()
    role = sys.argv[1]
    if role == "tracee":
        _tracee_main()
    elif role == "external":
        _external_main(int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
    else:
        raise SystemExit(f"unknown role {role}")
