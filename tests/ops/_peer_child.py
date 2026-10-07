"""peer 検査の試験で使う子 process。鍵は stdin から受け取り、argv / env に載せない。

usage: _peer_child.py <mode> <socket> [result_file]
- direct: header だけ送り (本文は送らない)、応答の状態行を stdout に出す
- exit_after_send: 要求を送り切ったら応答を待たずに exit する
- double_fork: 二重 fork した孫が親の exit 後に要求し、状態行を result_file に書く
"""
from __future__ import annotations

import os
import socket
import sys
import time


def _request(path: str, token: str, *, with_body: bool) -> bytes:
    if with_body:
        return (f"GET /v1/whoami HTTP/1.1\r\nHost: afx\r\nAuthorization: Bearer {token}\r\n"
                "Content-Length: 0\r\n\r\n").encode()
    # 本文 10 bytes を宣言して送らない: 本文を読みに行く server なら 408 まで待つ。
    return (f"POST /v1/policy HTTP/1.1\r\nHost: afx\r\nAuthorization: Bearer {token}\r\n"
            "Idempotency-Key: k\r\nContent-Length: 10\r\n\r\n").encode()


def _exchange(path: str, token: str, *, with_body: bool) -> str:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(20)
    sock.connect(path)
    sock.sendall(_request(path, token, with_body=with_body))
    data = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    sock.close()
    return data.decode(errors="replace")


def main() -> int:
    mode, path = sys.argv[1], sys.argv[2]
    token = sys.stdin.readline().strip()
    if mode == "direct":
        started = time.monotonic()
        text = _exchange(path, token, with_body=False)
        print(f"{time.monotonic() - started:.3f}")
        print(text)
        return 0
    if mode == "exit_after_send":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(path)
        sock.sendall(_request(path, token, with_body=True))
        os._exit(0)
    if mode == "double_fork":
        result = sys.argv[3]
        if os.fork() == 0:
            if os.fork() == 0:
                middle = os.getppid()
                deadline = time.monotonic() + 10
                while os.getppid() == middle and time.monotonic() < deadline:
                    time.sleep(0.01)
                # 中間の親 (original の子) が exit して孤児になってから接続する
                time.sleep(0.2)
                text = _exchange(path, token, with_body=True)
                with open(result, "w") as file:
                    file.write(text)
                os._exit(0)
            os._exit(0)
        os.wait()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
