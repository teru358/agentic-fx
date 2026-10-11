"""`GET /v1/events` を返す偽 daemon (実 UDS、instance lock の保持者を装う)。

usage: _events_daemon.py <socket_path> <lock_dir> <script_json> <record_file>

要求ごとに script_json を読み直す:
  {"events": [...], "high_watermark": n | null, "ignore_after": bool}
既定は `after` より大きい id を id 順に `limit` 件返し、high_watermark は null なら最大 id。
ignore_after が真なら `after` を無視して全部を返す (重複取得の再現)。
各要求の `after` と `limit` を record_file に 1 行ずつ JSON で追記する。
"""
from __future__ import annotations

import fcntl
import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def _reply(body: dict) -> bytes:
    raw = json.dumps(body).encode()
    return (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(raw)).encode() + b"\r\nConnection: close\r\n\r\n" + raw)


def _answer(head: bytes, script: Path, record: Path) -> bytes:
    target = head.split(b"\r\n", 1)[0].split(b" ")[1].decode("ascii")
    query = parse_qs(urlsplit(target).query)
    after = int(query.get("after", ["0"])[0])
    limit = int(query.get("limit", ["100"])[0])
    with record.open("a", encoding="ascii") as file:
        file.write(json.dumps({"path": urlsplit(target).path, "after": after,
                               "limit": limit}) + "\n")
    conf = json.loads(script.read_text(encoding="utf-8"))
    events = conf.get("events", [])
    if not conf.get("ignore_after"):
        events = sorted((e for e in events if e["id"] > after), key=lambda e: e["id"])[:limit]
    high = conf.get("high_watermark")
    if high is None:
        high = max((e["id"] for e in conf.get("events", [])), default=0)
    return _reply({"ok": True, "data": {"events": events, "high_watermark": high}})


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
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            if b"\r\n\r\n" in data:
                conn.sendall(_answer(data, Path(script), Path(record)))
        except OSError:
            pass
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
