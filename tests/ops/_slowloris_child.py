"""API socket に slowloris 接続 (1 byte/秒の要求行) を張り続ける子。

usage: _slowloris_child.py <socket> <connections> <stop_file>
<connections> 本を同時に保ち、server が 408 で切ったら張り直す。<stop_file> が現れたら終わる。
全本が最初に繋がったら "ready"、終わるときに張った接続の総数を出す。
"""
from __future__ import annotations

import resource
import select
import socket
import sys
import time
from pathlib import Path

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
REQUEST = b"GET /v1/status HTTP/1.1\r\nHost: afx\r\n"


def main() -> int:
    path, count, stop = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
    conns: list[list] = []  # [sock, 送った byte 数]
    opened = 0

    def connect() -> None:
        nonlocal opened
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(path)
        conns.append([sock, 0])
        opened += 1

    for _ in range(count):
        connect()
    print("ready", flush=True)
    while not stop.exists():
        for entry in list(conns):
            sock, sent = entry
            readable, _, _ = select.select([sock], [], [], 0)
            closed = bool(readable)
            if not closed:
                try:
                    sock.send(REQUEST[sent % len(REQUEST):][:1])
                    entry[1] = sent + 1
                except OSError:
                    closed = True
            if closed:
                sock.close()
                conns.remove(entry)
                connect()
        time.sleep(1.0)
    for sock, _ in conns:
        sock.close()
    print(opened, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
