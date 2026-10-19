"""web の read API を返す偽 daemon (実 UDS、instance lock の保持者を装う)。

usage: _web_daemon.py <socket_path> <lock_dir> <script_json> <record_file>

要求ごとに script_json を読み直す: {"<path>": data, ...}。path は `/v1/status` 等で、
`/v1/approvals/<id>` は `/v1/approvals/{id}`、`/v1/approvals/<id>/change` は
`/v1/approvals/{id}/change` の鍵で引く。鍵に無い path は 404 の error。
`"_delay"` があればその秒数だけ待ってから答える。

要求ごとに method、target、header 名 (小文字)、actor header の有無、body の長さ、
受信 bytes 全体の 16 進を record_file に 1 行ずつ JSON で追記する。要求を送らずに
閉じる接続 (server 認証だけの接続) は記録しない。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

_DETAIL = re.compile(r"/v1/approvals/[0-9]+\Z")
_CHANGE = re.compile(r"/v1/approvals/[0-9]+/change\Z")


def _reply(status: int, body: dict) -> bytes:
    raw = json.dumps(body).encode()
    reason = b"OK" if status == 200 else b"Not Found"
    return (b"HTTP/1.1 " + str(status).encode() + b" " + reason
            + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(raw)).encode() + b"\r\nConnection: close\r\n\r\n" + raw)


def _read(conn: socket.socket) -> bytes:
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


def _answer(data: bytes, script: Path, record: Path) -> bytes:
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    method, target = lines[0].split(b" ")[:2]
    names = [line.split(b":", 1)[0].strip().lower().decode("ascii", "replace")
             for line in lines[1:] if b":" in line]
    path = urlsplit(target.decode("ascii")).path
    with record.open("a", encoding="ascii") as file:
        file.write(json.dumps({"method": method.decode("ascii", "replace"),
                               "target": target.decode("ascii", "replace"), "headers": names,
                               "actor": "x-afx-asserted-actor" in names,
                               "body": len(body), "raw": data.hex()}) + "\n")
    conf = json.loads(script.read_text(encoding="utf-8"))
    if conf.get("_delay"):
        time.sleep(conf["_delay"])
    key = path
    if _DETAIL.fullmatch(path):
        key = "/v1/approvals/{id}"
    elif _CHANGE.fullmatch(path):
        key = "/v1/approvals/{id}/change"
    if method != b"GET" or key not in conf or key.startswith("_"):
        return _reply(404, {"ok": False, "error": {"code": "not_found"}})
    return _reply(200, {"ok": True, "data": conf[key]})


def main() -> int:
    path, lock_dir, script, record = sys.argv[1:5]
    lock = open(Path(lock_dir) / "instance.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    os.chmod(path, 0o600)
    listener.listen(16)
    print(f"ready {os.getpid()}", flush=True)
    while True:
        conn, _ = listener.accept()
        conn.settimeout(30)
        try:
            data = _read(conn)
            if b"\r\n\r\n" in data:
                conn.sendall(_answer(data, Path(script), Path(record)))
        except OSError:
            pass
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
