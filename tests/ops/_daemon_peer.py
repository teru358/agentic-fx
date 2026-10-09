"""daemon の子 process として、正しい operator 鍵で whoami を送る。

usage: _daemon_peer.py <root> <out>   結果 {"status": ..., "code": ...} を <out> に書く。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

from agentic_fx.ops import keys
from agentic_fx.ops.contracts import Principal


def main() -> int:
    root, out = Path(sys.argv[1]), Path(sys.argv[2])
    token = keys.read_token(keys.key_dir(root), Principal.OPERATOR)
    transport = httpx.HTTPTransport(uds=str(root / "data" / "run" / "api.sock"))
    with httpx.Client(transport=transport, base_url="http://afx", timeout=15,
                      headers={"Authorization": f"Bearer {token}"}) as client:
        response = client.get("/v1/whoami", headers={"Content-Length": "0"})
    body = response.json()
    error = body.get("error") if isinstance(body, dict) else None
    out.write_text(json.dumps({"status": response.status_code,
                               "code": error.get("code") if isinstance(error, dict) else None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
