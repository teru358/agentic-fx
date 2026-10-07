"""鍵を env / argv / 子 env / log / activity に出さないことを実 process で確かめる子。

usage: _keyleak_child.py <root>   (HOME は親が tmp に向けて渡す)
結果は stdout に JSON で出す。鍵そのものは出力しない。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from agentic_fx.activity import ActivityLog
from agentic_fx.ops import keys
from agentic_fx.ops.api_server import ApiServer
from agentic_fx.ops.contracts import Principal
from agentic_fx.ops.service import OpsService
from agentic_fx.store import db


def main() -> int:
    root = Path(sys.argv[1])
    (root / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=root / "logs" / "agentic.log", level=logging.DEBUG)
    conn = db.connect(root / "data" / "agentic.db")
    db.init_db(conn)
    activity = ActivityLog(root / "logs" / "activity.log")
    service = OpsService(conn, wall_clock=lambda: datetime.now(timezone.utc),
                         policy_path=root / "policy" / "directives.md",
                         activity_log=activity, activity_path=root / "logs" / "activity.log")
    directory = keys.key_dir(root)
    keys.ensure_initialized(directory, root=root)
    server = ApiServer(service, keys.load_keyset(directory), root / "data" / "run" / "api.sock")
    server.start()
    tokens = [keys.read_token(directory, p) for p in Principal]
    statuses = []
    for token in tokens + ["0" * 64]:
        transport = httpx.HTTPTransport(uds=str(server.socket_path))
        with httpx.Client(transport=transport, base_url="http://afx",
                          headers={"Authorization": f"Bearer {token}"}) as client:
            statuses.append(client.get("/v1/whoami", headers={"Content-Length": "0"}).status_code)
            statuses.append(client.post("/v1/policy", json={"text": "x"},
                                        headers={"Idempotency-Key": token[:8]}).status_code)
    worker = subprocess.run([sys.executable, "-c",
                             "import os,sys;sys.stdout.write(repr(dict(os.environ)))"],
                            capture_output=True, text=True, timeout=30)
    server.stop(5.0)
    service.shutdown(join_timeout=2.0)
    logging.shutdown()
    places = {
        "os.environ": repr(dict(os.environ)).encode(),
        "/proc/self/environ": Path("/proc/self/environ").read_bytes(),
        "/proc/self/cmdline": Path("/proc/self/cmdline").read_bytes(),
        "worker_env": worker.stdout.encode(),
        "log": (root / "logs" / "agentic.log").read_bytes(),
        "activity": (root / "logs" / "activity.log").read_bytes(),
    }
    leaks = sorted(name for name, blob in places.items()
                   if any(token.encode() in blob for token in tokens))
    print(json.dumps({"leaks": leaks, "statuses": statuses, "log_bytes": len(places["log"]),
                      "activity_bytes": len(places["activity"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
