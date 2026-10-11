"""試験用の ops client process。afx-ops の公開要求経路を別 process で動かす。

usage: python -I _ops_client.py <spec.json>

spec は {"principal", "socket", "lock", "key_file", "ready_file", "proc_locks"?,
"seccomp_no_pidfd"?, "landlock_no_proc_locks"?, "steps": [...]}。各 step の結果を
JSON 1 行で stdout に出す。step が ApiError で落ちたら ``run_guarded`` が固定 reason で
非零終了する (``expect_error`` の step は reason を出して続ける)。最後は ``os._exit``。
"""
from __future__ import annotations

import ctypes
import encodings.ascii  # noqa: F401  隔離後に codec を読み込まないよう先に載せる
import json
import os
import platform
import struct
import sys
import time
from pathlib import Path

from afx_ops import api_client, server_auth
from afx_ops.api_client import (Actor, ApiError, BotClient, DecisionJob, Endpoint,
                                NotifyClient, WebClient, _Request)

CLIENTS = {"ops-bot": BotClient, "ops-web": WebClient, "ops-notify": NotifyClient}


def _emit(**fields) -> None:
    sys.stdout.write(json.dumps(fields) + "\n")
    sys.stdout.flush()


def _wait_file(path: str, limit: float = 30.0) -> None:
    deadline = time.monotonic() + limit
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise SystemExit("barrier timeout")
        time.sleep(0.02)


# ---------------------------------------------------------------- 実 kernel の拒否を作る

def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    libc.syscall.restype = ctypes.c_long
    return libc


def _seccomp_no_pidfd() -> None:
    """getsockopt(SOL_SOCKET, 77) だけ ENOPROTOOPT を返させる (x86_64)。"""
    libc = _libc()
    ld, jeq, ret = 0x20, 0x15, 0x06
    allow, errno_ret = 0x7FFF0000, 0x00050000 | 92
    # jf は不一致のときに最後の allow (index 9) へ飛ぶ距離。
    prog = [(ld, 0, 0, 4), (jeq, 0, 7, 0xC000003E),          # arch == x86_64
            (ld, 0, 0, 0), (jeq, 0, 5, 55),                   # nr == getsockopt
            (ld, 0, 0, 24), (jeq, 0, 3, 1),                   # level == SOL_SOCKET
            (ld, 0, 0, 32), (jeq, 0, 1, server_auth.SO_PEERPIDFD),
            (ret, 0, 0, errno_ret), (ret, 0, 0, allow)]
    raw = b"".join(struct.pack("HBBI", *ins) for ins in prog)
    buf = ctypes.create_string_buffer(raw)
    fprog = struct.pack("HxxxxxxQ", len(prog), ctypes.addressof(buf))
    fbuf = ctypes.create_string_buffer(fprog)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "no_new_privs")
    if libc.prctl(22, 2, ctypes.addressof(fbuf), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "seccomp")


def _landlock_no_proc_locks(extra_roots: list[str]) -> None:
    """READ_FILE を自分の /proc/<pid> と runtime の dir にだけ許す (/proc/locks は不可)。"""
    libc = _libc()
    read_file = 1 << 2
    attr = ctypes.create_string_buffer(struct.pack("Q", read_file))
    ruleset = libc.syscall(444, attr, ctypes.c_size_t(8), ctypes.c_uint32(0))
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    roots = [f"/proc/{os.getpid()}", "/usr", "/lib", "/lib64", "/etc", sys.prefix,
             sys.base_prefix] + extra_roots
    for root in roots:
        if not os.path.exists(root):
            continue
        fd = os.open(root, os.O_PATH | os.O_CLOEXEC)
        rule = ctypes.create_string_buffer(struct.pack("=Qi", read_file, fd))
        if libc.syscall(445, ctypes.c_int(ruleset), ctypes.c_int(1), rule,
                        ctypes.c_uint32(0)) != 0:
            raise OSError(ctypes.get_errno(), "landlock_add_rule")
        os.close(fd)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "no_new_privs")
    if libc.syscall(446, ctypes.c_int(ruleset), ctypes.c_uint32(0)) != 0:
        raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    os.close(ruleset)


# ---------------------------------------------------------------- step

def _call(client, step: dict):
    name = step["call"]
    if name in ("approve", "retry"):
        actor = Actor(step["actor"]) if "actor" in step else step.get("raw_actor")
        decision = getattr(client, name)(actor, 7, payload_sha256="a" * 64)
        return {"status": decision.reply.status,
                "job": decision.job.job_id if decision.job else None}
    if name in ("poll", "cancel"):
        job = DecisionJob(step["job"], Actor(step["actor"]))
        return {"status": getattr(client, name)(job).status}
    reply = getattr(client, name)()
    return {"status": reply.status}


def _run(spec: dict, client, endpoint: Endpoint) -> None:
    for step in spec["steps"]:
        op = step["op"]
        try:
            if op == "call":
                _emit(op=op, ok=True, **_call(client, step))
            elif op == "ready":
                api_client.wait_ready(endpoint, interval=step.get("interval", 0.25),
                                      limit=step.get("limit", 30.0))
                _emit(op=op, ok=True)
            elif op == "shared":
                # 共有 verifier だけを通し (固有 admission なし)、通れば鍵を送る。
                sock = server_auth.connect_verified(endpoint.socket_path,
                                                    lock_path=endpoint.lock_path,
                                                    uid=endpoint.uid,
                                                    proc_locks=endpoint.proc_locks)
                with sock:
                    sock.sendall(api_client._encode("GET", "/v1/whoami", client._token,
                                                    None, None))
                    got = sock.recv(65536)
                _emit(op=op, ok=got.startswith(b"HTTP/1.1 200"))
            elif op == "open_wait_send":
                with _Request.open(endpoint) as request:
                    Path(step["opened"]).touch()
                    _wait_file(step["go"])
                    request.send(api_client._encode("GET", "/v1/whoami", client._token,
                                                    None, None))
                    request.receive()
                _emit(op=op, ok=True)
            elif op == "touch":
                Path(step["path"]).touch()
            elif op == "wait":
                _wait_file(step["path"])
            elif op == "proc_locks_readable":
                try:
                    Path("/proc/locks").read_bytes()
                    _emit(op=op, readable=True)
                except PermissionError:
                    _emit(op=op, readable=False)
            else:
                raise SystemExit(f"unknown step {op}")
        except ApiError as err:
            if not step.get("expect_error"):
                raise
            _emit(op=op, ok=False, reason=err.reason, token_sent=err.token_sent)
        except server_auth.ServerAuthError as err:
            _emit(op=op, ok=False, reason=err.reason, token_sent=False)


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text())
    endpoint = Endpoint(Path(spec["socket"]), Path(spec["lock"]),
                        uid=spec.get("uid", os.getuid()),
                        proc_locks=Path(spec.get("proc_locks", "/proc/locks")))
    holder = {}

    def body() -> None:
        # 鍵は隔離の前に読み終える。
        holder["client"] = CLIENTS[spec["principal"]].from_key_files(
            endpoint, key_file=Path(spec["key_file"]), ready_file=Path(spec["ready_file"]),
            _for_tests=True)
        if spec.get("seccomp_no_pidfd"):
            if platform.machine() != "x86_64":
                raise SystemExit("x86_64 only")
            _seccomp_no_pidfd()
        if spec.get("landlock_no_proc_locks"):
            _landlock_no_proc_locks(spec.get("landlock_roots", []))
        _run(spec, holder["client"], endpoint)

    rc = api_client.run_guarded(body)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)


if __name__ == "__main__":
    main()
