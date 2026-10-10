"""server 認証 oracle 用の偽 daemon。受け取った bytes を接続ごとに記録する。

usage: _oracle_daemon.py <socket_path> <record_file> <lock_dir|-> <answer|silent> [orphan]

- lock_dir を渡すと、その dir の instance.lock の FLOCK WRITE を自分で取る
  (本体の instance lock と同じ「保持者を装う」形。本体は import しない)。
- answer なら要求の頭を読んだ後に 200 の応答を返す (鍵が届けば probe は rc=0)。
- silent は何も返さない。
- orphan なら listen と lock 取得をした process は fork 後すぐ exit し、子が fd を
  引き継いで accept を続ける。SO_PEERCRED の pid と lock 保持者の pid はどちらも
  exit した親になる。
各接続の受信 bytes を 16 進で 1 行ずつ record_file に追記する。

追加の flag (orphan の後ろ、又は単独で並べる):
- ``parent_waits``: orphan と同じく子が accept を継ぐが、listen と lock 取得をした親は
  exit せず止まって待つ (試験側が pid 指定で殺す)。
- ``kill_parent_before_reply``: 子が要求の頭を読んだ後、応答の前に親を SIGKILL し、
  親の終了を pidfd で見届けてから応答する。
- ``barrier=<path>``: 要求の頭を読んだ後、path が現れるまで応答を遅らせる。
- ``answer_job``: 応答本文に決定の受理 (job_id と state) を入れる。
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

JOB_ID = "0123456789abcdef0123456789abcdef"


def _answer(body: dict) -> bytes:
    raw = json.dumps(body).encode()
    return (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(raw)).encode() + b"\r\nConnection: close\r\n\r\n"
            + raw)


_ANSWER = _answer({"ok": True})
_ANSWER_JOB = _answer({"ok": True, "data": {"job_id": JOB_ID, "state": "queued"}})


def _before_reply(flags: dict) -> None:
    barrier = flags.get("barrier")
    if barrier:
        deadline = time.monotonic() + 30
        while not os.path.exists(barrier) and time.monotonic() < deadline:
            time.sleep(0.02)
    parent = flags.get("parent")
    if flags.get("kill_parent_before_reply") and parent:
        os.kill(parent, signal.SIGKILL)
        # 親の終了 (zombie 化か消滅) を見届けてから応答する。
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _alive(parent):
            time.sleep(0.01)
        flags["parent"] = None


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as file:
            return file.read().rsplit(") ", 1)[1][0] != "Z"
    except OSError:
        return False


def _serve(listener: socket.socket, record: Path, answer: bool, flags: dict) -> None:
    reply = _ANSWER_JOB if flags.get("answer_job") else _ANSWER
    while True:
        conn, _ = listener.accept()
        conn.settimeout(30)
        data = b""
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                if b"\r\n\r\n" in data:
                    if answer:
                        _before_reply(flags)
                        conn.sendall(reply)
                    break
        except OSError:
            pass
        conn.close()
        with record.open("a") as file:
            file.write(data.hex() + "\n")


def main() -> int:
    path, record, lock_dir, mode = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4]
    extra = sys.argv[5:]
    flags: dict = {"answer_job": "answer_job" in extra,
                   "kill_parent_before_reply": "kill_parent_before_reply" in extra}
    for item in extra:
        if item.startswith("barrier="):
            flags["barrier"] = item.split("=", 1)[1]
    parent_waits = "parent_waits" in extra or flags["kill_parent_before_reply"]
    orphan = "orphan" in extra or parent_waits
    lock = None
    if lock_dir != "-":
        lock = open(Path(lock_dir) / "instance.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    os.chmod(path, 0o600)
    listener.listen(8)
    if orphan:
        listen_pid = os.getpid()
        child = os.fork()
        if child:
            print(f"ready {child} {listen_pid}", flush=True)
            if parent_waits:
                # 子が殺すか試験側が殺すまで、lock と listen の主のまま生きている。
                while True:
                    signal.pause()
            os._exit(0)
        flags["parent"] = listen_pid
    else:
        print(f"ready {os.getpid()} {os.getpid()}", flush=True)
    _serve(listener, record, mode == "answer", flags)
    del lock
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
