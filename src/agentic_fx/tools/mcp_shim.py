"""MCP stdio シム (設計書 §1.6)。

`run_mcp_shim`: CLI が子プロセスとして起動するエントリ。stdio の
JSON-RPC 行を Unix ドメインソケット越しに mission_worker (`McpShimDispatcher`)
へ転送するだけ — ツール本体は持たない。

`McpShimDispatcher`: mission_worker 側の専用スレッド。`tools/call` を
lock で直列化する (in-flight 1)。
"""
from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path

from agentic_fx.tools.registry import ToolRegistry

# 裁定 5: 実装計画で実 CLI 2 種の initialize 要求を実測して確定する。
# 未実測のため暫定値を置く — 実測後にこの定数を更新し、コミットメッセージ
# に実測ログの参照先を残すこと。
_SUPPORTED_PROTOCOL_VERSION = "2024-11-05"


def _mcp_tool_from_openai_tool(openai_tool: dict) -> dict:
    fn = openai_tool["function"]
    return {"name": fn["name"], "description": fn["description"],
            "inputSchema": fn["parameters"]}


class McpShimDispatcher:
    def __init__(self, *, sock_path: Path, registry: ToolRegistry,
                 allowed: list[str]) -> None:
        self._sock_path = sock_path
        self._registry = registry
        self._allowed = list(allowed)
        self._call_lock = threading.Lock()
        self.protocol_version = _SUPPORTED_PROTOCOL_VERSION

    def serve_forever(self) -> None:
        if self._sock_path.exists():
            self._sock_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self._sock_path))
        server.listen(8)
        try:
            while True:
                conn, _ = server.accept()
                threading.Thread(target=self._handle_conn, args=(conn,),
                                 daemon=True).start()
        finally:
            server.close()

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            req = json.loads(buf.decode())
            resp = self._dispatch(req)
            conn.sendall((json.dumps(resp) + "\n").encode())
        finally:
            conn.close()

    def _dispatch(self, req: dict) -> dict:
        rpc_id = req.get("id")
        method = req.get("method")
        if method == "initialize":
            requested = (req.get("params") or {}).get("protocolVersion")
            if requested != self.protocol_version:
                return {"jsonrpc": "2.0", "id": rpc_id,
                        "error": {"code": -32600,
                                 "message": f"unsupported protocolVersion {requested!r}"}}
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "afx", "version": "1"}}}
        if method == "tools/list":
            # openai_tools(allowed) は既に allowed でフィルタ済みの一覧を返す
            # (実 API、統合裁定 R-i7) — dispatcher 側で二重にフィルタしない。
            tools = [_mcp_tool_from_openai_tool(t)
                    for t in self._registry.openai_tools(self._allowed)]
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": tools}}
        if method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            if name not in self._allowed:
                return {"jsonrpc": "2.0", "id": rpc_id,
                        "error": {"code": -32601, "message": f"tool not allowed: {name!r}"}}
            with self._call_lock:
                # execute() は例外を送出せず、失敗時も JSON エラー文字列を
                # 返す (実 API、統合裁定 R-i7) — 呼び出し側で try/except しない。
                result = self._registry.execute(name, params.get("arguments") or {},
                                                self._allowed)
            # result は既に JSON 文字列 (execute の戻り値) — json.dumps で
            # 再エンコードすると二重エンコードになるため、そのまま入れる。
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {
                "content": [{"type": "text", "text": result}]}}
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32601, "message": f"unknown method {method!r}"}}


def run_mcp_shim(sock_path: Path) -> None:
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        req = json.loads(line)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(sock_path))
            s.sendall((json.dumps(req) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            sys.stdout.write(buf.decode())
            sys.stdout.flush()


if __name__ == "__main__":
    run_mcp_shim(Path(sys.argv[1]))
