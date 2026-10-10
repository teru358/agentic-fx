"""cleanup protocol を話す worker 役 (製品の WorkerRunner が起こす実 process)。

usage: python _cleanup_worker_helper.py <mode> <out.jsonl>

handshake を読み、`mission_worker._accept_mission_nonce` で nonce を受け、`ready` を返し、
`go` を待ってから mode ごとの停止点で振る舞う。観測は out.jsonl に 1 行 1 JSON で追記し
(fsync 付き)、SIGKILL された後でも読めるようにする。

mode:
- `request`: cleanup 要求 → deadline 受領 → ACK → cleanup の操作 → result → exit
- `silent`: 要求を出さず、届いた deadline に ACK して cleanup → result → exit
- `exit`: go の直後に何も言わず exit (EOF)
- `ack_dup`: `request` と同じで、正規の ACK の後にもう一度同じ ACK を送る
- `ack_mismatch`: `request` と同じで、正規の ACK の後に値の違う ACK を送る
- `ack_early`: go の直後 (deadline 発行前) に ACK を送り、その後は `request` と同じ
- `F1`: 要求の前に GIL を握って止まる
- `F2`: 要求の後・受領の前に GIL を握って止まる
- `F3`: ACK と cleanup 開始の後に GIL を握って止まる
- `F3b`: ACK と cleanup 開始の後に blocking syscall で止まる (watchdog が動ける)
- `stdin_full`: go の後は stdin を読まず、大きな応答の `tool_rpc` を送って blocking syscall で
  止まる (親の stdin の pipe が満杯になり、応答を書く dispatcher が lock を持ったまま詰まる)
- `request_before_ready`: ready を返す前に cleanup 要求を出し、ACK の後に exit する
- `request_then_ready`: cleanup 要求の直後に ready を返し、go を待たず ACK の後に exit する
- `request_then_result`: cleanup 要求の直後に (deadline を待たずに) result を送る
- `rpc_race`: go の後に `tool_rpc` を送り、少し置いてから cleanup 要求 (親の書き手が並ぶ)
"""
from __future__ import annotations

import ctypes
import json
import os
import resource
import sys
import tempfile
import threading
import time
from pathlib import Path

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

from agentic_fx import mission_worker as MW  # noqa: E402
from agentic_fx.core import mission_cgroup as MCG  # noqa: E402
from agentic_fx.core import mission_cleanup as MC  # noqa: E402
from agentic_fx.core import mission_protocol as MP  # noqa: E402
from agentic_fx.tools import mcp_spool  # noqa: E402

MODE, OUT = sys.argv[1], sys.argv[2]
_out_lock = threading.Lock()
_send_lock = threading.Lock()
_seq = [0]
_stdout = sys.stdout.buffer


def note(event: str, **kw) -> None:
    line = json.dumps({"event": event, "t": time.monotonic(), "pid": os.getpid(), **kw})
    with _out_lock:
        fd = os.open(OUT, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)


def send(frame: dict) -> None:
    with _send_lock:
        _seq[0] += 1
        frame = {**frame, "seq": _seq[0]}
        _stdout.write(MP.encode_frame(frame))
        _stdout.flush()
    note("sent", type=frame["type"])


def hang_gil() -> None:
    ctypes.PyDLL(None).sleep(60)


def hang_syscall() -> None:
    r, _w = os.pipe()
    os.read(r, 1)


def main() -> None:
    handshake = MP.read_frame(sys.stdin.buffer)
    note("handshake", nonce=handshake.get(MP.HANDSHAKE_NONCE_FIELD))
    try:
        nonce = MW._accept_mission_nonce(handshake)
    except MP.ProtocolError as e:
        send({"type": "ready", "ok": False, "error": f"ProtocolError: {e}"})
        note("nonce_rejected", error=str(e))
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = mcp_spool.SpoolRoot.create(Path(tmp), MW._mission_spool_nonce())
        spool_dir = root.path.name
        root.close()
    note("nonce", spool=spool_dir, cgroup=MCG.cgroup_name(nonce.hex), launch=nonce.raw.hex())

    go = threading.Event()
    rpc_result = threading.Event()
    channel = MC.WorkerCleanupChannel(send)

    def reader() -> None:
        while True:
            try:
                frame = MP.read_frame(sys.stdin.buffer)
            except MP.ProtocolError as e:
                note("reader_error", error=str(e))
                return
            if frame is None:
                note("stdin_eof")
                return
            note("received", type=frame.get("type"), deadline=frame.get("deadline"),
                 seq=frame.get("seq"))
            if frame.get("type") == "go":
                go.set()
                if MODE == "stdin_full":
                    note("stdin_abandoned")
                    return
            elif frame.get("type") == "tool_rpc_result":
                rpc_result.set()
            elif frame.get("type") == MP.CLEANUP_DEADLINE:
                channel.deliver(frame)

    threading.Thread(target=reader, daemon=True).start()
    if MODE == "request_before_ready":
        channel.request("startup_failed")
        deadline = channel.wait_deadline(10)
        note("cleanup_started", deadline=deadline.value if deadline else None)
        os._exit(0)
    if MODE == "request_then_ready":
        channel.request("startup_failed")
        send({"type": "ready", "ok": True})
        deadline = channel.wait_deadline(10)
        note("cleanup_started", deadline=deadline.value if deadline else None)
        os._exit(0)
    send({"type": "ready", "ok": True})
    if not go.wait(10):
        note("no_go")
        return
    note("go")

    if MODE == "exit":
        os._exit(0)
    if MODE == "F1":
        note("hang", where="before_request")
        hang_gil()
    if MODE == "stdin_full":
        send({"type": "tool_rpc", "rpc_id": "r1", "name": "big", "args": {}})
        note("hang", where="stdin_full")
        hang_syscall()
    if MODE == "request_then_result":
        channel.request("mission_end")
        send({"type": "result", "status": "completed", "output": {"mode": MODE}})
    if MODE == "rpc_race":
        send({"type": "tool_rpc", "rpc_id": "r1", "name": "probe", "args": {}})
        time.sleep(0.3)
        channel.request("mission_end")
    if MODE == "ack_early":
        send({"type": MP.CLEANUP_ACK, "deadline": time.monotonic() + 5})
    if MODE in ("request", "F2", "F3", "F3b", "ack_dup", "ack_mismatch", "ack_early"):
        channel.request("mission_end")
    if MODE == "F2":
        note("hang", where="before_receipt")
        hang_gil()
    deadline = channel.wait_deadline()
    note("cleanup_started", deadline=deadline.value)
    if MODE == "rpc_race" and not rpc_result.wait(10):
        note("no_rpc_result")
    if MODE == "ack_dup":
        send({"type": MP.CLEANUP_ACK, "deadline": deadline.value})
    if MODE == "ack_mismatch":
        send({"type": MP.CLEANUP_ACK, "deadline": deadline.value + 1.0})
    if MODE == "F3":
        note("hang", where="after_ack_gil")
        hang_gil()
    if MODE == "F3b":
        note("hang", where="after_ack_syscall")
        hang_syscall()
    send({"type": "result", "status": "completed", "output": {"mode": MODE}})
    note("exit")
    os._exit(0)


if __name__ == "__main__":
    main()
