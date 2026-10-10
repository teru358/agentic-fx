"""server 認証 oracle 用の偽 daemon。受け取った bytes を接続ごとに記録する。

usage: _auth_oracle_server.py <socket_path> <record_file> <lock_dir|-> <answer|silent>
                              [orphan]

- lock_dir を渡すと、その dir の instance.lock を自分で取る (本物の保持者を装う)。
- answer なら要求の頭を読んだ後に whoami の成功応答を返す (鍵が届けば client は rc=0)。
- orphan なら listen と lock 取得をした process は fork 後すぐ exit し、子が fd を
  引き継いで accept を続ける。SO_PEERCRED の pid と lock 保持者の pid はどちらも
  exit した親になる (親を回収すれば SO_PEERPIDFD は ESRCH)。
各接続の受信 bytes を 16 進で 1 行ずつ record_file に追記する。
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

_BODY = json.dumps({"ok": True, "data": {"authenticated_principal": "operator",
                                         "scopes": []}}).encode()
_ANSWER = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
           b"Content-Length: " + str(len(_BODY)).encode() + b"\r\nConnection: close\r\n\r\n"
           + _BODY)


def _serve(listener: socket.socket, record: Path, answer: bool) -> None:
    while True:
        conn, _ = listener.accept()
        conn.settimeout(5)
        data = b""
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                if b"\r\n\r\n" in data:
                    if answer:
                        conn.sendall(_ANSWER)
                    break
        except OSError:
            pass
        conn.close()
        with record.open("a") as file:
            file.write(data.hex() + "\n")


def main() -> int:
    path, record, lock_dir, mode = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4]
    orphan = len(sys.argv) > 5 and sys.argv[5] == "orphan"
    lock = None
    if lock_dir != "-":
        from agentic_fx.store.instance_lock import acquire_instance_lock
        lock = acquire_instance_lock(Path(lock_dir))
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    os.chmod(path, 0o600)
    listener.listen(8)
    if orphan:
        child = os.fork()
        if child:
            print(f"ready {child}", flush=True)
            os._exit(0)
    else:
        print(f"ready {os.getpid()}", flush=True)
    _serve(listener, record, mode == "answer")
    del lock
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
