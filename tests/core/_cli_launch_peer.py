"""protocol を話す偽 launcher。worker 側の判定の異常系を試すために、決めた順で
正常・異常な frame を送る。

`python <this file> <scenario> '<config json>'` で `launch_cli(launcher_cmd=...)`
から起こされる (config は本物の launcher と同じ形)。kernel の応答は代役にしない:
listener が要る場面では自分に全 ALLOW の filter を NEW_LISTENER で掛けて本物の
seccomp notify fd を作る。
"""
from __future__ import annotations

import ctypes
import json
import os
import resource
import socket
import struct
import sys
import time

from agentic_fx.core import cli_launch_protocol as P
from agentic_fx.core import landlock_nested
from agentic_fx.core import seccomp_cli_bpf

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

SCENARIO = sys.argv[1]
CFG = json.loads(sys.argv[2])
NONCE = bytes.fromhex(CFG["nonce"])
os.set_inheritable(CFG["status_fd"], False)
SOCK = socket.socket(fileno=CFG["status_fd"])

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


def send(data: bytes, fds=(), creds: bool = False) -> None:
    anc = []
    if fds:
        anc.append((socket.SOL_SOCKET, socket.SCM_RIGHTS,
                    b"".join(struct.pack("<i", fd) for fd in fds)))
    if creds:
        anc.append((socket.SOL_SOCKET, socket.SCM_CREDENTIALS,
                    struct.pack("<iII", os.getpid(), os.getuid(), os.getgid())))
    SOCK.sendmsg([data], anc)


def recv() -> bytes:
    data, _anc, _flags, _ = SOCK.recvmsg(256, 256)
    return data


def listener() -> int:
    class F(ctypes.Structure):
        _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8),
                    ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]

    class Prog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(F))]

    arr = (F * 1)(F(0x06, 0, 0, 0x7FFF0000))  # RET ALLOW
    prog = Prog(1, ctypes.cast(arr, ctypes.POINTER(F)))
    assert _libc.prctl(38, 1, 0, 0, 0) == 0
    fd = _libc.syscall(ctypes.c_long(317), ctypes.c_uint(1), ctypes.c_uint(8),
                       ctypes.byref(prog))
    assert fd >= 0, ctypes.get_errno()
    return int(fd)


def prepared(**override) -> bytes:
    c = seccomp_cli_bpf.compile_policy()
    fields = dict(profile_id=c.profile_id, policy_hash=c.policy_hash, bpf_hash=c.bpf_hash,
                  filter_flags=c.filter_flags, landlock_abi=landlock_nested.probe_abi())
    fields.update(override)
    # exec する artifact の identity は渡された fd のもの (この偽 launcher は copy せず fd を exec する)
    st = os.fstat(CFG["cli_fd"])
    return P.encode_prepared(NONCE, P.Prepared(**fields), (st.st_dev, st.st_ino))


def hello(pid: int | None = None, nonce: bytes = NONCE) -> bytes:
    return P.encode_hello(nonce, os.getpid() if pid is None else pid)


def handshake() -> None:
    send(hello())
    assert recv()[:P.HEADER_LEN] == P.encode_joined(NONCE)


def block() -> None:
    time.sleep(60)


def exec_cli() -> None:
    os.set_inheritable(CFG["cli_fd"], True)
    os.execve(CFG["cli_fd"], CFG["argv"], dict(os.environ))


s = SCENARIO
if s == "short":
    send(hello()[:10]); block()
elif s == "long":
    send(hello() + b"\0"); block()
elif s == "magic":
    send(b"XFXL" + hello()[4:]); block()
elif s == "version":
    send(hello()[:4] + struct.pack("<H", 1) + hello()[6:]); block()
elif s == "nonce":
    send(hello(nonce=bytes(16))); block()
elif s == "unknown_type":
    send(hello()[:6] + struct.pack("<H", 99) + hello()[8:]); block()
elif s == "zero_length":
    send(b""); block()
elif s == "hello_pid":
    send(hello(pid=os.getpid() + 1)); block()
elif s == "joined_first":
    send(P.encode_joined(NONCE)); block()
elif s == "prepared_first":
    fd = listener(); send(prepared(), fds=[fd]); os.close(fd); block()
elif s == "hello_twice":
    handshake(); send(hello()); block()
elif s == "go_before_prepared":
    handshake(); send(P.encode_go(NONCE)); block()
elif s == "hello_with_fd":
    r, w = os.pipe(); send(hello(), fds=[r]); block()
elif s == "prepared_no_fd":
    handshake(); send(prepared()); block()
elif s == "prepared_two_fds":
    handshake(); fd = listener(); r, w = os.pipe(); send(prepared(), fds=[fd, r]); block()
elif s == "prepared_pipe":
    handshake(); r, w = os.pipe(); send(prepared(), fds=[r]); block()
elif s == "prepared_creds":
    handshake(); fd = listener(); send(prepared(), fds=[fd], creds=True); os.close(fd); block()
elif s == "prepared_bad_hash":
    handshake(); fd = listener(); send(prepared(policy_hash="00" * 32), fds=[fd]); block()
elif s == "die_before_hello":
    os._exit(0)
elif s == "silent":
    block()
elif s == "no_prepared":
    handshake(); block()
elif s == "magic_with_fd":
    r, w = os.pipe(); send(b"XFXL" + hello()[4:], fds=[r]); block()
elif s in ("go_send_fails", "exec_failed", "exec_timeout", "exe_mismatch",
           "stderr_error_but_valid", "hello_after_go", "exec_replaced_path"):
    handshake()
    fd = listener()
    if s == "go_send_fails":
        # PREPARED の前に受信側を閉じ、worker の GO 送信が必ず失敗するようにする
        SOCK.shutdown(socket.SHUT_RD)
    send(prepared(), fds=[fd])
    os.close(fd)
    if s == "go_send_fails":
        block()
    assert recv()[:P.HEADER_LEN] == P.encode_go(NONCE)
    if s == "exec_failed":
        send(P.encode_exec_failed(NONCE, 2)); block()
    elif s == "exec_timeout":
        block()
    elif s == "exe_mismatch":
        SOCK.close(); block()
    elif s == "hello_after_go":
        send(hello()); block()
    elif s == "exec_replaced_path":
        # 渡された fd でなく、同じ path に置き換えた別の file を exec する
        os.rename(os.environ["AFX_TEST_REPLACEMENT"], CFG["argv"][0])
        os.execve(CFG["argv"][0], CFG["argv"], dict(os.environ))
    elif s == "stderr_error_but_valid":
        sys.stderr.write("ERROR: setup failed, sandbox unavailable\n"); sys.stderr.flush()
        exec_cli()
elif s == "stderr_success_rc0":
    sys.stderr.write("setup ok, cli started\n"); sys.stderr.flush()
    os._exit(0)
else:
    raise SystemExit(f"unknown scenario {s}")
