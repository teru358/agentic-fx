"""実 daemon の UDS の手前に立つ中継 (試験用)。

usage: _uds_relay.py <instance_root> <target_socket> <drop_flag>

`<instance_root>/data/run/api.sock` で待ち受け、`<instance_root>/data/instance.lock` を保持する
(client は lock の保持者と接続相手の pid の一致で相手を確かめる)。要求を実 daemon の socket へ
そのまま写し、応答を返す。`<drop_flag>` の file があるとき、改善の開始 (`POST /v1/improve/runs`)
だけは要求を daemon へ届けた後、応答を client へ返さずに接続を閉じて flag を消す
(daemon は受け付けたが client は応答を失う状況)。

起動したら標準出力に `ready` を 1 行出す。
"""
from __future__ import annotations

import fcntl
import os
import re
import socket
import sys
import threading
from pathlib import Path


def _read_request(conn: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return data
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    match = re.search(rb"(?im)^content-length:\s*(\d+)\s*$", head)
    want = int(match.group(1)) if match else 0
    while len(body) < want:
        chunk = conn.recv(65536)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body


def _roundtrip(target: str, raw: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
        upstream.connect(target)
        upstream.sendall(raw)
        out = b""
        while chunk := upstream.recv(65536):
            out += chunk
        return out


def _serve_one(conn: socket.socket, target: str, drop: Path) -> None:
    with conn:
        raw = _read_request(conn)
        if not raw:
            return  # 要求を送らない接続 (server 認証だけ)
        answer = _roundtrip(target, raw)
        first = raw.split(b"\r\n", 1)[0]
        if first.startswith(b"POST /v1/improve/runs ") and drop.exists():
            drop.unlink()
            return
        conn.sendall(answer)


def main() -> int:
    root, target, drop = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    run = root / "data" / "run"
    run.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(root / "data", 0o700)
    os.chmod(run, 0o700)
    lock = os.open(root / "data" / "instance.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    path = run / "api.sock"
    if path.exists():
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(16)
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    while True:
        conn, _ = server.accept()
        threading.Thread(target=_serve_one, args=(conn, target, drop), daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
