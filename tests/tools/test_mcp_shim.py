"""MCP stdio シム — JSON-RPC 3 メソッドの契約テスト (設計書 §1.6、§8.1-7)。

`McpShimDispatcher` は mission_worker 側 (Unix socket サーバ)。`run_mcp_shim`
は CLI 側の子プロセスエントリ (stdin/stdout の JSON-RPC を socket 越しに
転送するだけ)。ここでは両者を実プロセス/実 socket で結線し、in-flight 1・
直列化・fail closed を実測する。
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_fx.tools.mcp_shim import McpShimDispatcher
from agentic_fx.tools.registry import ToolDef, ToolRegistry
from agentic_fx.config import ImproveToolBudgetSettings
from agentic_fx.tools.mission_counters import MissionToolCounters


def _slow_tool(x: int) -> dict:
    # ToolRegistry.execute は `tool.func(**arguments)` で呼ぶ (実 API、統合
    # 裁定 R-i7)。schema の properties 名に対応するキーワード引数を取る。
    time.sleep(0.3)
    return {"echo": x}


def _never_allowed_tool() -> dict:
    return {"unreachable": True}


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolDef(
        name="slow_echo",
        description="echoes x after a delay",
        parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
        func=_slow_tool))
    # 登録済みだが allowed には入れない — allowed フィルタの観測点
    # (openai_tools の allowed 引数の出所を pin する)。
    reg.register(ToolDef(
        name="never_allowed",
        description="registered but never in allowed",
        parameters={"type": "object"},
        func=_never_allowed_tool))
    return reg


def _start_dispatcher(tmp_path) -> tuple[McpShimDispatcher, Path, threading.Thread]:
    sock_path = tmp_path / "afx.sock"
    dispatcher = McpShimDispatcher(sock_path=sock_path, registry=_make_registry(),
                                   allowed=["slow_echo"])
    t = threading.Thread(target=dispatcher.serve_forever, daemon=True)
    t.start()
    deadline = time.monotonic() + 3.0
    while not sock_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    return dispatcher, sock_path, t


def _rpc(sock_path: Path, payload: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(sock_path))
        s.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode())


def test_initialize_returns_protocol_version_and_capabilities(tmp_path):
    dispatcher, sock_path, _ = _start_dispatcher(tmp_path)
    resp = _rpc(sock_path, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": dispatcher.protocol_version}})
    assert resp["result"]["protocolVersion"] == dispatcher.protocol_version
    assert "tools" in resp["result"]["capabilities"]
    assert "serverInfo" in resp["result"]
    # #47 (`verified-round1.md` 1-B): 中身 (`{"name":"afx","version":"1"}`)
    # まで見る。
    assert resp["result"]["serverInfo"] == {"name": "afx", "version": "1"}


@pytest.mark.parametrize("measured", [
    "2025-11-25",  # claude CLI 2.1.251 (実機実測 2026-08-30)
    "2025-06-18",  # codex-mcp-client 0.150.1 (tee 捕捉 2026-08-30)
])
def test_initialize_accepts_measured_protocol_versions(tmp_path, measured):
    """[T13-5c] 実機実測 (2026-08-30) の版を allowlist に含め、応答は要求版を
    echo する (単一固定だと backend 間で版が割れた時に片方が必ず落ちる)。"""
    dispatcher, sock_path, _ = _start_dispatcher(tmp_path)
    resp = _rpc(sock_path, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": measured}})
    assert resp["result"]["protocolVersion"] == measured
    assert resp["result"]["serverInfo"] == {"name": "afx", "version": "1"}


def test_initialize_rejects_unknown_protocol_version(tmp_path):
    """裁定 5: 未知の版要求には結果を返さず JSON-RPC error (fail closed)。"""
    dispatcher, sock_path, _ = _start_dispatcher(tmp_path)
    resp = _rpc(sock_path, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "9999-99-99"}})
    assert "error" in resp
    assert "result" not in resp
    # #43 (`verified-round1.md` 1-B): code/id の保持まで見る。
    assert resp["error"]["code"] == -32600
    assert resp["id"] == 1


def test_initialize_rejects_missing_protocol_version_key(tmp_path):
    """段 0 M15 pin: `params` は在るが `protocolVersion` キーが無いケース
    (メモリ 6.6 の 3 値問題 — 非空値/不一致値の他に「欠落」を渡す)。
    `requested = (params or {}).get("protocolVersion")` は `None` になり、
    `None != self.protocol_version` で不一致 → fail closed が正。変異
    (`requested is not None and requested != self.protocol_version` に
    書き換えて None を素通しする) だとこのケースだけ `result` が返る。"""
    dispatcher, sock_path, _ = _start_dispatcher(tmp_path)
    resp = _rpc(sock_path, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {}})
    assert "error" in resp
    assert "result" not in resp
    # #43 (`verified-round1.md` 1-B): code/id の保持まで見る。
    assert resp["error"]["code"] == -32600
    assert resp["id"] == 1


def test_initialize_rejects_missing_params_key(tmp_path):
    """段 0 M15 pin: `params` 自体が無いケース (`req.get("params") or {}`
    で `{}` に落ちる) でも `protocolVersion` 欠落として fail closed。"""
    dispatcher, sock_path, _ = _start_dispatcher(tmp_path)
    resp = _rpc(sock_path, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert "error" in resp
    assert "result" not in resp
    # #43 (`verified-round1.md` 1-B): code/id の保持まで見る。
    assert resp["error"]["code"] == -32600
    assert resp["id"] == 1


def test_tools_call_passes_allowed_list_not_all_registry_names_to_execute(tmp_path):
    """段 0 M14 pin: `tools/call` の内側防御 — `registry.execute` の第 3
    引数は `self._allowed` そのものでなければならず、`self._registry.names()`
    (登録済み全ツール名) にすり替えてはならない。今日の配線 (
    `_start_mcp_dispatcher` は `allowed=registry.names()` を渡す) では
    外側の `name not in self._allowed` 検査が先に落とすため機能的には
    等価だが (stage0-bundle-A.md §2.4)、`allowed` を第 3 引数として実際に
    渡していることを spy で確認しておけば、将来 allowed が names() の
    真部分集合になっても (Task 10) 内側防御が生きたまま保たれる。"""
    reg = _make_registry()
    captured: dict = {}
    orig_execute = reg.execute

    def spy_execute(name, arguments, allowed):
        captured["allowed"] = list(allowed)
        return orig_execute(name, arguments, allowed)

    reg.execute = spy_execute

    sock_path = tmp_path / "afx.sock"
    dispatcher = McpShimDispatcher(sock_path=sock_path, registry=reg,
                                   allowed=["slow_echo"])
    t = threading.Thread(target=dispatcher.serve_forever, daemon=True)
    t.start()
    deadline = time.monotonic() + 3.0
    while not sock_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)

    resp = _rpc(sock_path, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                            "params": {"name": "slow_echo", "arguments": {"x": 1}}})
    assert "result" in resp
    assert captured.get("allowed") == ["slow_echo"], (
        "registry.execute へ渡った allowed が self._allowed と一致しない (M14)")
    assert captured["allowed"] != reg.names(), (
        "registry.execute へ registry.names() (全ツール名) が渡っている — "
        "self._allowed とのすり替えが疑われる (M14)")


def test_tools_list_returns_registered_tools_only_from_allowed(tmp_path):
    dispatcher, sock_path, _ = _start_dispatcher(tmp_path)
    resp = _rpc(sock_path, {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                            "params": {}})
    names = [t["name"] for t in resp["result"]["tools"]]
    assert names == ["slow_echo"]


def test_tools_call_executes_registered_handler(tmp_path):
    dispatcher, sock_path, _ = _start_dispatcher(tmp_path)
    resp = _rpc(sock_path, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "slow_echo", "arguments": {"x": 7}}})
    content = resp["result"]["content"]
    assert content[0]["type"] == "text"
    payload = json.loads(content[0]["text"])
    assert payload == {"echo": 7}


def test_after_send_runs_only_after_tool_response_is_sent(tmp_path):
    order = []
    reg = ToolRegistry()
    reg.register(ToolDef("x", "x", {"type": "object"},
                         lambda: order.append("execute") or {"ok": True}))
    dispatcher = McpShimDispatcher(
        sock_path=tmp_path / "unused", registry=reg, allowed=["x"],
        after_send=lambda: order.append("after_send"))

    class FakeSocket:
        def __init__(self):
            self.read = False
        def recv(self, _n):
            if self.read:
                return b""
            self.read = True
            return (b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
                    b'"params":{"name":"x","arguments":{}}}\n')
        def sendall(self, _data):
            order.append("send")
        def close(self):
            order.append("close")

    dispatcher._handle_conn(FakeSocket())
    assert order == ["execute", "send", "after_send", "close"]


def test_concurrent_accepted_response_is_sent_before_abort_event(tmp_path):
    entered = threading.Barrier(2)
    release = threading.Event()
    order = []
    counters = MissionToolCounters(
        budget=ImproveToolBudgetSettings(max_refusal_streak=1))
    reg = ToolRegistry()

    def accepted():
        entered.wait()
        release.wait(2)
        return {"ok": True}

    def refused():
        counters.record_terminal_refusal()
        return {"error": "budget exhausted"}

    reg.register(ToolDef("accepted", "", {"type": "object"}, accepted))
    reg.register(ToolDef("refused", "", {"type": "object"}, refused))
    dispatcher = McpShimDispatcher(
        sock_path=tmp_path / "unused", registry=reg,
        allowed=["accepted", "refused"],
        after_send=lambda: (counters.fire_if_pending(),
                            order.append("event")
                            if counters.abort_event.is_set() else None))

    class FakeSocket:
        def __init__(self, name):
            self.name = name
            self.done = False
        def recv(self, _n):
            if self.done:
                return b""
            self.done = True
            return (json.dumps({"jsonrpc": "2.0", "id": self.name,
                                "method": "tools/call", "params": {
                                    "name": self.name, "arguments": {}}})
                    + "\n").encode()
        def sendall(self, _data):
            order.append(f"send:{self.name}")
        def close(self):
            pass

    first = threading.Thread(
        target=dispatcher._handle_conn, args=(FakeSocket("accepted"),))
    first.start()
    entered.wait()
    second = threading.Thread(
        target=dispatcher._handle_conn, args=(FakeSocket("refused"),))
    second.start()
    release.set()
    first.join(2); second.join(2)
    assert order.index("send:accepted") < order.index("event")
    assert order.index("send:refused") < order.index("event")


def test_tools_call_null_arguments_falls_back_to_empty_dict(tmp_path):
    """#49 (`verified-round1.md` 1-B): `params.get("arguments") or {}` の
    `arguments: null` 経路 (キーは在るが値が JSON null) が未テスト。
    `or {}` を落とす変異は `arguments=None` のまま `registry.execute` に
    渡し、`jsonschema.validate(None, schema)` が型不一致で
    `"invalid arguments: ..."` を返す。`or {}` が効いていれば `{}` として
    schema (properties のみ・required 無し) を通過し、`slow_echo(**{})` の
    `TypeError` (必須位置引数 `x` 欠落) に化ける — このメッセージの違いで
    区別する。"""
    dispatcher, sock_path, _ = _start_dispatcher(tmp_path)
    resp = _rpc(sock_path, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                            "params": {"name": "slow_echo", "arguments": None}})
    content = resp["result"]["content"]
    payload = json.loads(content[0]["text"])
    assert "error" in payload
    assert "invalid arguments" not in payload["error"], (
        f"arguments=null が {{}} にフォールバックしていない: {payload!r}")


def test_tools_call_rejects_disallowed_tool(tmp_path):
    """`never_allowed` は登録済みだが `_start_dispatcher` の allowed には
    入っていない (`_make_registry` 参照) — 「allowed が空」ではなく「allowed
    は非空だがこのツールだけ許可されていない」という現実的なケースを踏む。
    `"result" not in resp` により、dispatcher の事前検査が JSON-RPC トップ
    レベルの error を返すこと (`registry.execute` 内部の allowed 検査が返す
    `result.content` 内の JSON エラー文字列ではないこと) を明示的に pin する。"""
    dispatcher, sock_path, _ = _start_dispatcher(tmp_path)
    resp = _rpc(sock_path, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "never_allowed", "arguments": {}}})
    assert "error" in resp
    assert "result" not in resp


def test_tools_call_is_serialized_in_flight_one(tmp_path):
    """§1.6: `tool_rpc` パイプの in-flight 1 はシム経由でも保たれる
    (mission_worker 側で lock を取って直列化する)。2 本を同時に投げ、
    実行区間が重ならないことをタイムスタンプで確認する。

    #50 (`verified-round1.md` 1-B): `dispatcher._allowed.append` は private
    属性への直接操作 (`_registry.register` は public API なので問題無い)。
    `allowed=` を渡し直して dispatcher を作り直す形に変更する — `timed`
    ツールを allowed に含めた状態で `McpShimDispatcher` を構築する。"""
    spans: list[tuple[float, float]] = []
    lock = threading.Lock()

    def timed_tool() -> dict:
        # arguments={} → execute は `tool.func()` (kwargs 展開が空) で呼ぶ。
        start = time.monotonic()
        time.sleep(0.2)
        end = time.monotonic()
        with lock:
            spans.append((start, end))
        return {"ok": True}

    registry = _make_registry()
    registry.register(ToolDef(  # register() は ToolRegistry の公開 API
        name="timed", description="d", parameters={"type": "object"},
        func=timed_tool))
    sock_path = tmp_path / "afx.sock"
    dispatcher = McpShimDispatcher(sock_path=sock_path, registry=registry,
                                   allowed=["slow_echo", "timed"])
    t = threading.Thread(target=dispatcher.serve_forever, daemon=True)
    t.start()
    deadline = time.monotonic() + 3.0
    while not sock_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)

    results = []

    def call():
        results.append(_rpc(sock_path, {"jsonrpc": "2.0", "id": 5,
                                        "method": "tools/call",
                                        "params": {"name": "timed", "arguments": {}}}))

    # #46 (`verified-round1.md` 1-B): 2 本だけだと `sleep(0.2)` の粒度に
    # 依存し `with self._call_lock:` を外した変異でもタイミング次第で
    # 通ってしまう余地が残る。3 本に増やしペアワイズ全数で非重複を見る。
    threads = [threading.Thread(target=call) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=5)
    assert len(spans) == 3
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            (s_i, e_i), (s_j, e_j) = spans[i], spans[j]
            assert e_i <= s_j or e_j <= s_i, (
                f"overlapping spans (not serialized): {spans}")


def test_run_mcp_shim_forwards_stdio_to_unix_socket(tmp_path):
    """CLI 側の子プロセスエントリ (`python -m agentic_fx.tools.mcp_shim
    <sock>`) が stdin の JSON-RPC 行を socket へ転送し、応答を stdout へ
    書き戻すことを実プロセスで確認する。"""
    dispatcher, sock_path, _ = _start_dispatcher(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentic_fx.tools.mcp_shim", str(sock_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        resp = json.loads(line)
        assert [t["name"] for t in resp["result"]["tools"]] == ["slow_echo"]
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_run_mcp_shim_swallows_notifications(tmp_path):
    """実機実測 (2026-08-30, mission #6 trace): codex は initialize 後に
    `notifications/initialized` 通知 (id 無し) を送るが、旧実装は -32601
    error を応答していた。JSON-RPC で通知に応答してはならない — 通知は
    無応答で飲み込み、次の要求 (id あり) への応答が最初の出力行になること。"""
    dispatcher, sock_path, _ = _start_dispatcher(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentic_fx.tools.mcp_shim", str(sock_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        req = {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}
        proc.stdin.write(json.dumps(note) + "\n" + json.dumps(req) + "\n")
        proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        assert resp["id"] == 7, f"通知への応答が漏れている: {resp}"
        assert "result" in resp
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_call_lock_covers_sendall_so_refusal_cannot_execute_during_send(tmp_path):
    """codex 1 周目 Important (2026-09-08): 「sendall が _call_lock の内側」を決定論的に
    観測する。受理側の sendall を event でブロックしている間、拒否側の tool 関数
    (execute) に **入れない** こと。lock を execute だけに縮める変異 (sendall を
    lock 外へ) では、受理側の送信中に拒否側が execute に入り refused_entered が
    立つ → red。"""
    send_release = threading.Event()
    accepted_sending = threading.Event()
    refused_entered = threading.Event()
    refused_received = threading.Event()   # 拒否側が _handle_conn で要求を読み終えた
    order = []
    counters = MissionToolCounters(
        budget=ImproveToolBudgetSettings(max_refusal_streak=1))
    reg = ToolRegistry()
    reg.register(ToolDef("accepted", "", {"type": "object"}, lambda: {"ok": True}))

    def refused():
        refused_entered.set()
        counters.record_terminal_refusal()
        return {"error": "budget exhausted"}

    reg.register(ToolDef("refused", "", {"type": "object"}, refused))
    dispatcher = McpShimDispatcher(
        sock_path=tmp_path / "unused", registry=reg,
        allowed=["accepted", "refused"],
        after_send=lambda: (counters.fire_if_pending(),
                            order.append("event")
                            if counters.abort_event.is_set() else None))

    class FakeSocket:
        def __init__(self, name):
            self.name = name
            self.done = False
        def recv(self, _n):
            if self.done:
                return b""
            self.done = True
            if self.name == "refused":
                refused_received.set()
            return (json.dumps({"jsonrpc": "2.0", "id": self.name,
                                "method": "tools/call", "params": {
                                    "name": self.name, "arguments": {}}})
                    + "\n").encode()
        def sendall(self, _data):
            if self.name == "accepted":
                accepted_sending.set()
                assert send_release.wait(5), "test harness: release not set"
            order.append(f"send:{self.name}")
        def close(self):
            pass

    first = threading.Thread(target=dispatcher._handle_conn, args=(FakeSocket("accepted"),))
    first.start()
    assert accepted_sending.wait(5)
    second = threading.Thread(target=dispatcher._handle_conn, args=(FakeSocket("refused"),))
    second.start()
    # codex 2 周目 Important: 拒否側スレッドが要求を読み終えて lock 取得に
    # 進んだことを同期してから判定する (スケジューリング遅延で偽 green に
    # ならないように)。
    assert refused_received.wait(5), "test harness: refused thread did not start"
    # 受理側が sendall の途中 (lock 保持中) — 拒否側は execute に入れない
    assert refused_entered.wait(0.5) is False, "sendall 中に別接続の execute が走った (lock が sendall を覆っていない)"
    send_release.set()
    first.join(5); second.join(5)
    assert refused_entered.is_set()
    assert order == ["send:accepted", "send:refused", "event"]
