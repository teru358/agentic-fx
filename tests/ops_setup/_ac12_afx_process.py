"""AC-12 の試験用に afx の代わりに常駐する process (unit の ExecStart から起動される)。

usage: python _ac12_afx_process.py <instance_root> [<mode>]

mode は ``<instance_root>/afx_mode.json`` (``{"mode": ..., "delay": 秒, "key_dir": ...}``) か
2 番目の引数。無ければ ``none``。

- none: instance lock を握って tick するだけ (UDS を出さない)。
- stub: 遅れて ``srv.sock`` を出し、接続ごとに受け取った byte 数を ``token_bytes`` に追記する。
- api: 遅れて本物の ``ApiServer`` を ``data/run/api.sock`` に出す (鍵 dir の鍵で認証する)。
- rogue: instance lock を握らず、遅れて ``data/run/api.sock`` に生の socket を出し、
  接続ごとの受信 byte 数を ``rogue_bytes`` に、接続数を ``rogue_connections`` に追記する。
"""
from __future__ import annotations

import json
import os
import resource
import socket
import sys
import time
from pathlib import Path

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

root = Path(sys.argv[1])
spec_file = root / "afx_mode.json"
spec = json.loads(spec_file.read_text()) if spec_file.exists() else {}
mode = sys.argv[2] if len(sys.argv) > 2 else spec.get("mode", "none")
delay = float(spec.get("delay", 0.0))


class _StubOps:
    """ApiServer が呼ぶ面だけを持つ。web が読む status は固定値を返す。"""

    def status(self, principal, *, deadline=None):
        return {"mode": "paper", "autopilot": "on", "equity": 1000.5,
                "kill_switch": {"latched": False, "generation": 3}}

    def record_rejection(self, category):
        return None

    def flush_rejections(self):
        return None


def _record(path: Path, text: str) -> None:
    with open(path, "a") as handle:
        handle.write(text + "\n")


def _raw_server(path: Path, record: Path, connections: Path | None) -> socket.socket:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(8)
    server.settimeout(0.1)
    return server


def _serve_raw_once(server: socket.socket, record: Path, connections: Path | None) -> None:
    try:
        conn, _ = server.accept()
    except socket.timeout:
        return
    conn.settimeout(1)
    try:
        data = conn.recv(4096)
    except socket.timeout:
        data = b""
    if connections is not None:
        _record(connections, "1")
    _record(record, str(len(data)))
    conn.close()


lock = None
if mode != "rogue":
    from agentic_fx.store.instance_lock import acquire_instance_lock
    lock = acquire_instance_lock(root / "data")

started = time.monotonic()
count = 0
server = None
api = None
while True:
    count += 1
    tmp = root / "tick.tmp"
    tmp.write_text(f"{os.getpid()} {count}\n")
    os.replace(tmp, root / "tick")
    if server is None and api is None and mode != "none" \
            and time.monotonic() - started >= delay:
        if mode == "stub":
            server = _raw_server(root / "srv.sock", root / "token_bytes", None)
        elif mode == "rogue":
            server = _raw_server(root / "data" / "run" / "api.sock",
                                 root / "rogue_bytes", root / "rogue_connections")
        elif mode == "api":
            from agentic_fx.ops.api_server import ApiServer
            from agentic_fx.ops.keys import load_keyset
            api = ApiServer(_StubOps(), load_keyset(Path(spec["key_dir"])),
                            root / "data" / "run" / "api.sock")
            api.start()
    if server is not None:
        _serve_raw_once(server,
                        root / ("rogue_bytes" if mode == "rogue" else "token_bytes"),
                        root / "rogue_connections" if mode == "rogue" else None)
    else:
        time.sleep(0.1)
