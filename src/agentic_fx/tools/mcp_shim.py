"""MCP stdio シム (設計書 §1.6)。

`run_mcp_shim`: CLI が子プロセスとして起動するエントリ。stdio の
JSON-RPC 行を spool directory 経由で mission worker へ渡すだけ — ツール
本体は持たない。CLI の部分木 (seccomp と nested Landlock の下) で動くので、
この module の先頭では spool の client と標準ライブラリの軽いものだけを
import する (`ToolRegistry` 等の worker 側 module を読まない)。

`McpShimDispatcher`: worker 側の JSON-RPC 処理部。spool の dispatcher
(`mcp_spool.SpoolDispatcher`) から呼ばれ、`tools/call` を lock で直列化する
(in-flight 1)。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from agentic_fx.tools import mcp_spool_client

if TYPE_CHECKING:
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
    """JSON-RPC の処理部 (worker 側)。spool の dispatcher から 1 要求ずつ
    `respond` が呼ばれる。"""

    def __init__(self, *, registry: "ToolRegistry", allowed: list[str],
                 after_send: Callable[[], None] | None = None) -> None:
        import threading
        self._registry = registry
        self._allowed = list(allowed)
        self._call_lock = threading.Lock()
        self._after_send = after_send
        # (kind, detail) を spool の ledger に書く先。tool を実際に実行した
        # ことの正本を ledger に置くため、spool の dispatcher が配線する。
        self.record: Callable[[str, str], None] | None = None
        # 成功した initialize / tools/list の観測 (kind, tools/list に返した名前) を
        # 受け取る先。自己試験が「CLI が MCP に繋がった」ことを親で確かめるのに使う。
        self.on_observe: Callable[[str, list[str]], None] | None = None
        self.protocol_version = _SUPPORTED_PROTOCOL_VERSION

    def respond(self, req: dict, publish: Callable[[dict], None]) -> None:
        """要求 1 件を処理して応答を `publish` に渡す。

        `tools/call` は allowed の検査・budget・実行・counter の更新・応答の
        publish・`after_send` を一つの lock 区間で行う。`after_send` (budget
        打ち切りの発火) を応答の publish より後に置くのは、CLI が最後の
        応答を受け取る前に打ち切られないようにするため。"""
        observed: list[tuple[str, list[str]]] = []
        if req.get("method") == "tools/call":
            with self._call_lock:
                resp = self._dispatch(req, observed)
                publish(resp)
                if self._after_send is not None:
                    self._after_send()
        else:
            resp = self._dispatch(req, observed)
            publish(resp)
        # 観測は応答が CLI へ渡る (publish が成功する) 後にだけ通知する
        for kind, names in observed:
            self._observe(kind, names)

    def _observe(self, kind: str, names: list[str]) -> None:
        if self.on_observe is not None:
            try:
                self.on_observe(kind, names)
            except Exception:  # noqa: BLE001 — 観測の失敗で MCP の応答を止めない
                pass

    def _note(self, kind: str, detail: str = "") -> None:
        if self.record is not None:
            self.record(kind, detail)

    def _dispatch(self, req: dict,
                  observed: list[tuple[str, list[str]]] | None = None) -> dict:
        if observed is None:
            observed = []
        rpc_id = req.get("id")
        method = req.get("method")
        if method == "initialize":
            requested = (req.get("params") or {}).get("protocolVersion")
            if requested not in _SUPPORTED_PROTOCOL_VERSIONS:
                self._note("rejected", "unsupported_protocol_version")
                return {"jsonrpc": "2.0", "id": rpc_id,
                        "error": {"code": -32600,
                                 "message": f"unsupported protocolVersion {requested!r}"}}
            self._note("served", "initialize")
            observed.append(("initialize", []))
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {
                "protocolVersion": requested,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "afx", "version": "1"}}}
        if method == "tools/list":
            # openai_tools(allowed) は既に allowed でフィルタ済みの一覧を返す
            # (実 API、統合裁定 R-i7) — dispatcher 側で二重にフィルタしない。
            tools = [_mcp_tool_from_openai_tool(t)
                    for t in self._registry.openai_tools(self._allowed)]
            self._note("served", "tools/list")
            observed.append(("tools/list", [str(t.get("name")) for t in tools]))
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": tools}}
        if method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            if name not in self._allowed:
                self._note("rejected", "tool_not_allowed")
                return {"jsonrpc": "2.0", "id": rpc_id,
                        "error": {"code": -32601, "message": f"tool not allowed: {name!r}"}}
            # execute() は例外を送出せず、失敗時も JSON エラー文字列を
            # 返す。呼び出し側 `respond` が execute + publish を同じ lock
            # 区間に置く。
            self._note("tool_executed", str(name))
            result = self._registry.execute(name, params.get("arguments") or {},
                                            self._allowed)
            observed.append(("tools/call", [str(name)]))
            # result は既に JSON 文字列 (execute の戻り値) — json.dumps で
            # 再エンコードすると二重エンコードになるため、そのまま入れる。
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {
                "content": [{"type": "text", "text": result}]}}
        self._note("rejected", "unknown_method")
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32601, "message": f"unknown method {method!r}"}}


# 応答待ちの期限。settings からは変えられない定数。応答が置かれない要求
# (応答の一時 file を先置きされた、dispatcher が止まった) で CLI を永久に
# 待たせないため、期限で JSON-RPC error を返して次の要求へ進む。正常な tool
# 実行を切らないよう、backtest の RPC 期限の既定 (600 秒) より長く取る。
SHIM_RESPONSE_TIMEOUT_SEC = 900.0

_DISPATCHER_UNAVAILABLE = -32000


def _with_request_id(resp: str, rpc_id: object) -> str:
    """dispatcher は拒否した要求の id を読まずに `id: null` の error を返す。
    CLI が応答を要求と対応づけられるよう、shim が自分の要求の id を補う。"""
    try:
        body = json.loads(resp)
    except ValueError:
        return resp
    if (isinstance(body, dict) and body.get("id") is None
            and "error" in body):
        body["id"] = rpc_id
        return json.dumps(body)
    return resp


def run_mcp_shim(spool_dir: Path | str, *,
                 response_timeout_sec: float = SHIM_RESPONSE_TIMEOUT_SEC) -> None:
    """stdio の JSON-RPC を spool 経由で worker へ渡す。

    応答の一時 file の先置きは自己妨害として扱い、dispatcher は実行も応答も
    しない。shim は `response_timeout_sec` で諦めて error を返す。"""
    dirfd = mcp_spool_client.open_spool_dir(spool_dir)
    try:
        while True:
            line = sys.stdin.readline()
            if not line:
                return
            req = json.loads(line)
            if "id" not in req:
                # JSON-RPC 通知 (id 無し、例: codex の notifications/initialized)
                # には応答してはならない — 実機実測 (2026-08-30 mission #6):
                # 旧実装は dispatcher へ転送して -32601 error を書き戻していた。
                continue
            rid = mcp_spool_client.publish_request(
                dirfd, (json.dumps(req) + "\n").encode())
            try:
                resp = mcp_spool_client.wait_response(
                    dirfd, rid, response_timeout_sec).decode()
            except TimeoutError:
                resp = json.dumps({
                    "jsonrpc": "2.0", "id": req["id"],
                    "error": {"code": _DISPATCHER_UNAVAILABLE,
                              "message": "dispatcher_unavailable"}})
            resp = _with_request_id(resp, req["id"])
            if not resp.endswith("\n"):
                resp += "\n"
            sys.stdout.write(resp)
            sys.stdout.flush()
    finally:
        os.close(dirfd)


if __name__ == "__main__":
    run_mcp_shim(Path(sys.argv[1]))
