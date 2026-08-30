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

# 裁定 5: 実 CLI の initialize 要求を実測して確定する (Task 13 → [T13-5c])。
# 実測 (2026-08-30、runbook「claude 実ターン」節): claude CLI 2.1.251 は
# "2025-11-25" を送る。単一固定だと backend 間で版が割れた時に片方が必ず
# 落ちるため、実測済み版の allowlist + 要求版 echo とする。codex-mcp-client
# 0.150.1 は "2025-06-18" を送る (tee 捕捉、同 runbook)。未実測の版は
# 従来どおり -32600 で fail closed。
_SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-06-18", "2025-11-25")
_SUPPORTED_PROTOCOL_VERSION = _SUPPORTED_PROTOCOL_VERSIONS[0]


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
        # A-4 検収是正 (r2, B1-r2): bind 済み server socket。`bind()` が
        # 呼び出しスレッドで同期実行され、`self._server` が設定された時点で
        # bind 成否が確定する — `serve_forever` 側の非同期 accept ループと
        # 分離することで、呼び出し元が bind の成否を直接 (例外として)
        # 観測できるようにする。
        self._server: socket.socket | None = None
        # 段 0 申し送り 2: `close()` が意図的に呼ばれたことを示すフラグ。
        # `serve_forever` の accept ループが `close()` 起因の `OSError`
        # (Bad file descriptor 等) を「意図的な停止」として区別するため
        # に使う — フラグを見ずに `except OSError: return` にすると、
        # RLIMIT_NOFILE 枯渇 (EMFILE/ENFILE) 等の**意図しない** accept 失敗
        # まで静かに dispatcher を止めてしまい、CLI 側は以降のツール呼び
        # 出しが connection-refused になるだけで診断情報が残らない。
        self._closing = False

    def bind(self) -> None:
        """`sock_path` へ同期で bind+listen する (B1-r2 是正)。

        例外 (`OSError` とそのサブクラス — `unlink()` の `IsADirectoryError`
        や `bind()` の `FileNotFoundError`/`PermissionError` 等) はそのまま
        呼び出し元へ伝播させる。旧実装は `serve_forever` を daemon thread に
        投げてから `sock_path.exists()` を 3 秒ポーリングする**代理観測**で
        bind 成否を判定していたため、bind 前に何らかのエントリが同名で
        存在すると (例: 他プロセスの残骸、または `sock_path` が既に
        ディレクトリ) 誤って成功と判定していた (検収 B1-r2 の masking
        probe)。`bind()` を同期実行し例外をそのまま伝播させることで、
        代理観測を排除し真の fail closed にする。
        """
        if self._sock_path.exists():
            self._sock_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self._sock_path))
        server.listen(8)
        self._server = server

    def close(self) -> None:
        """dispatcher を停止する (段 0 申し送り 2)。

        `server.close()` で `serve_forever` の `accept()` を脱出させ
        (accept 中のブロッキング呼び出しは close 後に `OSError` を送出する
        ため、`serve_forever` の `finally: server.close()` が二重 close に
        なるが `socket.close()` は冪等なので害はない)、bind した
        socket ファイルを削除する。呼び出し元 (`_run_improve_mission` の
        戻り値を保持する `main()`) が Mission 終了時に呼ぶ想定 —
        `bind()` していない (= `_server is None`) 状態での呼び出しは
        no-op。"""
        self._closing = True
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        try:
            self._sock_path.unlink()
        except FileNotFoundError:
            pass

    def serve_forever(self) -> None:
        if self._server is None:
            self.bind()
        server = self._server
        try:
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    if self._closing:
                        # `close()` が呼び出しスレッドから `server.close()`
                        # した結果の `OSError` (Bad file descriptor 等) —
                        # 意図的な停止なので、スレッドの未処理例外として
                        # pytest/ログに漏らさず静かにループを抜ける。
                        return
                    # `close()` が呼ばれていない accept 失敗 (RLIMIT_NOFILE
                    # 枯渇による EMFILE/ENFILE 等) は意図しない異常なので
                    # 握りつぶさず再送出する — 診断情報 (thread traceback)
                    # を残す。
                    raise
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
            if requested not in _SUPPORTED_PROTOCOL_VERSIONS:
                return {"jsonrpc": "2.0", "id": rpc_id,
                        "error": {"code": -32600,
                                 "message": f"unsupported protocolVersion {requested!r}"}}
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {
                "protocolVersion": requested,
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
