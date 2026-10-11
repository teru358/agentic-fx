"""bot が呼ぶ操作 API を返す偽 daemon (実 UDS、instance lock の保持者を装う、接続ごとに thread)。

usage: _bot_daemon.py <socket_path> <lock_dir> <script_json> <record_file>

要求ごとに script_json を読み直す:
  {"events": [...], "high_watermark": n | null,
   "approvals": {"<id>": {"status": "pending", "payload_sha256": "<64 hex>"}},
   "detail_delay": 秒 (approval detail の応答前に待つ),
   "decide": {"status": 202, "job_id": "<32 hex>" | null, "drop": bool,
              "settles": bool (受けた決定で承認を approved にする)},
   "job": {"state": "done"}}
応答の形は本体 daemon (agentic_fx.ops.service) と同じ: detail は ``status``、job は ``state``。
各要求の method / path / query / actor header / 受信時刻を record_file に 1 行ずつ JSON で追記する。
"""
from __future__ import annotations

import fcntl
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

_LOCK = threading.Lock()
# 決定を受けて正本では pending でなくなった承認 ID (decide.settles のとき)
_DECIDED: set[str] = set()


def _reply(status: int, body: dict) -> bytes:
    raw = json.dumps(body).encode()
    reason = {200: "OK", 202: "Accepted", 404: "Not Found", 409: "Conflict"}.get(status, "X")
    return (f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n").encode() + raw


def _ok(data: dict, status: int = 200) -> bytes:
    return _reply(status, {"ok": True, "data": data})


def _answer(method: str, target: str, headers: dict, script: Path, record: Path) -> bytes | None:
    split = urlsplit(target)
    path, query = split.path, parse_qs(split.query)
    with _LOCK, record.open("a", encoding="ascii") as file:
        file.write(json.dumps({"method": method, "path": path,
                               "query": {k: v[0] for k, v in query.items()},
                               "actor": headers.get("x-afx-asserted-actor"),
                               "t": time.time()}) + "\n")
    conf = json.loads(script.read_text(encoding="utf-8"))
    approvals = {k: ({**v, "status": "approved"} if k in _DECIDED else v)
                 for k, v in conf.get("approvals", {}).items()}
    parts = path.strip("/").split("/")
    if path == "/v1/events":
        after = int(query.get("after", ["0"])[0])
        limit = int(query.get("limit", ["100"])[0])
        events = sorted((e for e in conf.get("events", []) if e["id"] > after),
                        key=lambda e: e["id"])[:limit]
        high = conf.get("high_watermark")
        if high is None:
            high = max((e["id"] for e in conf.get("events", [])), default=0)
        return _ok({"events": events, "high_watermark": high})
    if path == "/v1/approvals":
        rows = [{"id": int(k), "kind": "plugin", "name": "p", "content_hash": "h",
                 "created_at": "2026-10-11T00:00:00Z"}
                for k, v in sorted(approvals.items(), key=lambda kv: int(kv[0]))
                if v.get("status") == "pending"]
        return _ok({"approvals": rows, "open_journals": []})
    if len(parts) == 3 and parts[:2] == ["v1", "approvals"] and method == "GET":
        time.sleep(float(conf.get("detail_delay", 0)))
        item = approvals.get(parts[2])
        if item is None:
            return _reply(404, {"ok": False, "error": {"code": "not_found"}})
        return _ok({"id": int(parts[2]), "kind": "plugin", "status": item["status"],
                    "parent_facts": {"note": "free text that must not be rendered"},
                    "agent_claims": "claim text that must not be rendered",
                    "facts_lines": [], "in_sample": None, "holdout": None,
                    "payload_sha256": item["payload_sha256"], "reason": None,
                    "decided_by": None, "decided_at": None, "asserted_actor": None})
    if len(parts) == 4 and parts[:2] == ["v1", "approvals"] and method == "POST":
        decide = conf.get("decide", {"status": 202, "job_id": "a" * 32})
        if decide.get("drop"):
            return None
        if decide.get("settles"):
            with _LOCK:
                _DECIDED.add(parts[2])
        data = {"job_id": decide["job_id"], "state": "queued"} if decide.get("job_id") else {}
        return _ok(data, decide.get("status", 202))
    if len(parts) == 3 and parts[:2] == ["v1", "jobs"]:
        job = conf.get("job", {"state": "done"})
        return _ok({"id": parts[2], "kind": "approve", "state": job["state"], "result": None,
                    "result_code": None})
    return _reply(404, {"ok": False, "error": {"code": "not_found"}})


def _serve(conn: socket.socket, script: Path, record: Path) -> None:
    conn.settimeout(30)
    data = b""
    try:
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode("ascii").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        while len(body) < length:
            chunk = conn.recv(65536)
            if not chunk:
                break
            body += chunk
        answer = _answer(method, target, headers, script, record)
        if answer is not None:
            conn.sendall(answer)
    except OSError:
        pass
    finally:
        conn.close()


def main() -> int:
    path, lock_dir, script, record = sys.argv[1:5]
    lock = open(Path(lock_dir) / "instance.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    os.chmod(path, 0o600)
    listener.listen(64)
    print(f"ready {os.getpid()}", flush=True)
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=_serve, args=(conn, Path(script), Path(record)),
                         daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
