"""偽の UDS server。受け取った bytes を接続ごとに記録し、応答は返さない。

usage: _fake_uds_server.py <socket_path> <record_file> [hold_lock_dir]
hold_lock_dir を渡すと、その dir の instance.lock を自分で取る (本物の保持者を装う)。
各接続の受信 bytes を 16 進で 1 行ずつ record_file に追記する。
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path


def main() -> int:
    path, record = sys.argv[1], Path(sys.argv[2])
    lock = None
    if len(sys.argv) > 3:
        from agentic_fx.store.instance_lock import acquire_instance_lock
        lock = acquire_instance_lock(Path(sys.argv[3]))
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    os.chmod(path, 0o600)
    listener.listen(8)
    print("ready", flush=True)
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
                # 要求の頭が届いたら応答せずに切る (client は応答喪失として扱う)。
                if b"\r\n\r\n" in data:
                    break
        except OSError:
            pass
        conn.close()
        with record.open("a") as file:
            file.write(data.hex() + "\n")
    del lock


if __name__ == "__main__":
    raise SystemExit(main())
