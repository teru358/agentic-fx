#!/usr/bin/env python3
"""I-1 是正 実プロセス pin 用の claude CLI フェイク (trade profile 専用)。

`fake_claude.py` (unit レベル契約テスト用) とは別に、実際に
`--mcp-config` が指す MCP server (shim) を設定どおりの command/args で
起動し、`initialize` → `tools/list` を発行して結果を
`<workdir>/observed_tools.json` へ書く。「`ready` 受信時点で MCP の spool
が処理されている」を、CLI が shim 経由で
ツール一覧を取得できることで実測する。

argv/env の記録は `fake_claude.py` と同じ形式 (`observed_argv.json` /
`observed_env.json`)。
"""
import json
import os
import subprocess
import sys
from pathlib import Path


def _mcp_server_command(argv: list[str]) -> list[str]:
    mcp_config_path = None
    for i, a in enumerate(argv):
        if a == "--mcp-config" and i + 1 < len(argv):
            mcp_config_path = argv[i + 1]
            break
    if mcp_config_path is None:
        raise SystemExit("no --mcp-config in argv")
    cfg = json.loads(Path(mcp_config_path).read_text())
    server = cfg["mcpServers"]["afx"]
    return [server["command"], *server["args"]]


def _rpc(proc: subprocess.Popen, payload: dict) -> dict:
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def main() -> int:
    argv = sys.argv[1:]
    workdir = Path.cwd()
    (workdir / "observed_argv.json").write_text(json.dumps(argv))
    (workdir / "observed_env.json").write_text(json.dumps(dict(os.environ)))

    shim = subprocess.Popen(_mcp_server_command(argv), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    try:
        init_resp = _rpc(shim, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"}})
        tools_resp = _rpc(shim, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    finally:
        shim.stdin.close()
        shim.wait(timeout=10)
    tool_names = sorted(
        t["name"] for t in tools_resp.get("result", {}).get("tools", []))
    (workdir / "observed_tools.json").write_text(json.dumps({
        "initialize_ok": "error" not in init_resp,
        "tool_names": tool_names,
    }))

    init_event = {
        "type": "system", "subtype": "init",
        "cwd": str(workdir), "model": "claude-haiku-4-5",
        "tools": [], "mcp_servers": [{"name": "afx", "status": "connected"}],
        "slash_commands": [],
    }
    print(json.dumps(init_event))
    result_event = {"type": "result", "subtype": "success",
                    "num_turns": 1, "stop_reason": "tool_use",
                    "structured_output": {}}
    print(json.dumps(result_event))
    return 0


if __name__ == "__main__":
    sys.exit(main())
