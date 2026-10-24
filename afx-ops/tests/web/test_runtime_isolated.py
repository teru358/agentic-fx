"""隔離済みの実 process で web runtime を回す: 実 UDS の fake daemon、loopback の実 HTTP client。"""
from __future__ import annotations

import errno
import json
import re
import socket
import subprocess
import sys
import time
from asyncio import selector_events
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE.parent / "isolation") not in sys.path:
    sys.path.insert(0, str(HERE.parent / "isolation"))

from _afx_isolation_support import (  # noqa: E402
    Interactive,
    layout_of,
    make_root,
    needs_landlock,
    needs_strace,
    needs_x86_64,
    no_core,
)
from _keydir import token_for  # noqa: E402
from afx_ops.isolation import profiles as P  # noqa: E402
from afx_ops.isolation.accept_loop import ACCEPT_SYSCALL, RC_ACCEPT_EXHAUSTED  # noqa: E402
from afx_ops.web.headers import SECURITY_HEADERS  # noqa: E402

pytestmark = [needs_x86_64, needs_landlock]

WEB_DAEMON = HERE / "_web_daemon.py"
EPERM, EACCES = errno.EPERM, errno.EACCES
WEB_TOKEN = token_for("ops-web").encode()

STATUS = {"mode": "paper", "autopilot": "on", "equity": 1000.5,
          "kill_switch": {"latched": False, "generation": 3}}
APPROVALS = {"approvals": [{"id": 7, "kind": "plugin", "name": "ema_cross",
                            "content_hash": "ab" * 8, "created_at": "2026-10-11T00:00:00Z"}]}
DETAIL = {"id": 7, "kind": "plugin", "status": "pending", "payload_sha256": "cd" * 32}
LOG = {"lines": ["tick ok", "order none"]}
ACTIVITY = {"lines": ["NEWS 1", "TRADE 2"]}
CHANGE = {"id": 7, "change": {"classification": "new", "name": "ema_cross", "kind": "strategy",
                              "candidate_hash": "ef" * 32, "params": {"items": []}},
          "claims": {"summary": "one", "selection_rationale": "two"},
          "diff": {"files": [], "omitted_lines": 0}}
ROUTES = {"/v1/status": STATUS, "/v1/approvals": APPROVALS, "/v1/approvals/{id}": DETAIL,
          "/v1/approvals/{id}/change": CHANGE, "/v1/log": LOG, "/v1/activity": ACTIVITY}


class WebDaemon:
    """`_web_daemon.py` を起こし、応答を差し替え、受けた要求を読む。"""

    def __init__(self, socket_path: Path, data_dir: Path, work: Path) -> None:
        self.script = work / "web-script.json"
        self.record = work / "web-record.jsonl"
        self.set(ROUTES)
        self.proc = subprocess.Popen([sys.executable, "-I", str(WEB_DAEMON), str(socket_path),
                                      str(data_dir), str(self.script), str(self.record)],
                                     stdout=subprocess.PIPE, text=True, preexec_fn=no_core)
        word, _pid = self.proc.stdout.readline().split()
        assert word == "ready"

    def set(self, routes: dict) -> None:
        tmp = self.script.with_suffix(".tmp")
        tmp.write_text(json.dumps(routes))
        tmp.replace(self.script)

    def requests(self) -> list[dict]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(10)
        self.proc.stdout.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def root(tmp_path) -> Path:
    return make_root(tmp_path)


@pytest.fixture
def daemon(root, tmp_path):
    started = WebDaemon(layout_of(root).socket_path, layout_of(root).data, tmp_path)
    yield started
    started.close()


def spec(root, port, *, requests=1, **extra) -> dict:
    params = {"requests": requests, **extra.pop("params", {})}
    return {"feature": "web", "root": str(root), "web_port": port, "op": "web_runtime",
            "params": params, **extra}


def with_host(raw: bytes, port: int) -> bytes:
    """要求の `Host: x` を、待受の address と port の Host に置き換える。"""
    return raw.replace(b"Host: x\r\n", f"Host: 127.0.0.1:{port}\r\n".encode())


def exchange(port: int, raw: bytes, *, timeout: float = 10) -> bytes:
    """1 接続で raw を送り、server が閉じるまでの応答を全部読む。"""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as client:
        client.sendall(with_host(raw, port))
        data = b""
        while chunk := client.recv(65536):
            data += chunk
    return data


def get(path: str, method: str = "GET", extra: bytes = b"") -> bytes:
    return (f"{method} {path} HTTP/1.1\r\nHost: x\r\n".encode() + extra
            + b"Connection: close\r\n\r\n")


def parse(data: bytes) -> tuple[int, dict[str, str], bytes]:
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers, body


def serve(root, port, child_env, raws: list[bytes], **extra):
    """runtime を起こし、raws を 1 本ずつ別接続で送って応答を集め、終わった結果を返す。"""
    web = Interactive(spec(root, port, requests=len(raws), **extra), child_env)
    try:
        web.wait_line("SERVING")
        replies = [exchange(port, raw) for raw in raws]
        done = web.finish()
    finally:
        web.kill()
    return replies, done



def assert_secured(headers: dict[str, str]) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert headers[name.lower()] == value, name
    assert headers["content-type"] == "text/html; charset=utf-8"
    # aiohttp の版を名乗らない
    assert headers["server"] == "afx-ops"


def assert_finished(done) -> dict:
    assert done.rc == 0, done.stderr[-3000:]
    result = done.result
    assert result["rc"] == 0
    # 隔離後に新しい module を読まない (aiohttp.web は隔離前に読込み済み)
    assert result["new_modules"] == []
    assert WEB_TOKEN.decode() not in done.stdout + done.stderr
    return result


# --- route と HEAD ---------------------------------------------------------------

READS = [("/", "/v1/status", b"paper"),
         ("/approvals", "/v1/approvals?status=pending&limit=20", b"ema_cross"),
         ("/approvals/7", "/v1/approvals/7", b"cd" * 32),
         ("/logs", "/v1/log?n=100", b"order none"),
         ("/activity", "/v1/activity?n=100", b"TRADE 2")]


def test_each_route_answers_get_and_head_with_the_web_principal_only(root, daemon, child_env):
    port = free_port()
    raws = [get(path) for path, _, _ in READS] + [get(path, "HEAD") for path, _, _ in READS]
    replies, done = serve(root, port, child_env, raws)
    assert_finished(done)
    gets, heads = replies[:len(READS)], replies[len(READS):]
    for (path, _, shown), got, head in zip(READS, gets, heads):
        status, headers, body = parse(got)
        assert status == 200, path
        assert_secured(headers)
        assert int(headers["content-length"]) == len(body)
        assert shown in body, path
        assert WEB_TOKEN not in body
        h_status, h_headers, h_body = parse(head)
        assert h_status == 200 and h_body == b""
        # HEAD は GET と同じ header (時刻以外) で、本文を返さない
        assert {k: v for k, v in h_headers.items() if k != "date"} == \
            {k: v for k, v in headers.items() if k != "date"}
    seen = daemon.requests()
    # 承認 #N は本体の後に変更内容 endpoint を 1 回読む
    targets = [t for _, t, _ in READS]
    targets.insert(targets.index("/v1/approvals/7") + 1, "/v1/approvals/7/change")
    assert [r["target"] for r in seen] == targets * 2
    for request in seen:
        assert request["method"] == "GET" and request["body"] == 0 and not request["actor"]
        # web の鍵だけで読む (他 principal の鍵も actor も載らない)
        assert b"Authorization: Bearer " + WEB_TOKEN in bytes.fromhex(request["raw"])


# --- 書込みに届かない -------------------------------------------------------------

MUTATIONS = {
    "post_status": (b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 16\r\n\r\n"
                    b"decision=approve", 405),
    "put_detail": (b"PUT /approvals/7 HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}", 405),
    "patch_list": (b"PATCH /approvals HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n", 405),
    "delete_detail": (b"DELETE /approvals/7 HTTP/1.1\r\nHost: x\r\n\r\n", 405),
    "options_logs": (b"OPTIONS /logs HTTP/1.1\r\nHost: x\r\n\r\n", 405),
    "post_activity": (b"POST /activity HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n", 405),
    "post_decide": (b"POST /decide HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}", 404),
    "get_decide": (b"GET /decide HTTP/1.1\r\nHost: x\r\n\r\n", 404),
    "post_detail_decide": (b"POST /approvals/7/decide HTTP/1.1\r\nHost: x\r\n"
                           b"Content-Length: 0\r\n\r\n", 404),
    "post_approve_api": (b"POST /v1/approvals/7/approve HTTP/1.1\r\nHost: x\r\n"
                         b"Content-Length: 0\r\n\r\n", 404),
    "get_jobs": (b"GET /jobs HTTP/1.1\r\nHost: x\r\n\r\n", 404),
    "post_jobs": (b"POST /jobs HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n", 404),
    "get_events": (b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n", 404),
    "form_query": (b"GET /approvals/7?action=approve&payload_sha256=ab HTTP/1.1\r\nHost: x\r\n\r\n",
                   400),
    "actor_header": (b"GET / HTTP/1.1\r\nHost: x\r\nX-Afx-Asserted-Actor: someone\r\n\r\n", 400),
    "body_on_get": (b"GET /approvals/7 HTTP/1.1\r\nHost: x\r\nContent-Length: 15\r\n\r\n"
                    b"action=approve!", 400),
}


def test_mutations_and_unknown_routes_never_reach_the_daemon(root, daemon, child_env):
    port = free_port()
    names = sorted(MUTATIONS)
    replies, done = serve(root, port, child_env, [MUTATIONS[name][0] for name in names])
    assert_finished(done)
    for name, reply in zip(names, replies):
        status, headers, _body = parse(reply)
        assert status == MUTATIONS[name][1], name
        assert_secured(headers)
        if status == 405:
            assert headers["allow"] == "GET, HEAD", name
        else:
            assert "allow" not in headers, name
    # 変更要求・actor・決定の bytes は daemon に 1 件も届かない
    assert daemon.requests() == []


# --- parser と大きさの上限 --------------------------------------------------------

def _headers(count: int, size: int) -> bytes:
    return b"".join(b"X-Pad-%d: " % i + b"a" * size + b"\r\n" for i in range(count))


BOUNDS = {
    "not_http": (b"HELLO\r\n\r\n", 400),
    "non_ascii_method": (b"G\xc3\x89T / HTTP/1.1\r\nHost: x\r\n\r\n", 400),
    "absolute_form": (b"GET http://127.0.0.1/ HTTP/1.1\r\nHost: x\r\n\r\n", 400),
    "traversal": (b"GET /approvals/../logs HTTP/1.1\r\nHost: x\r\n\r\n", 400),
    "encoded_traversal": (b"GET /%2e%2e/logs HTTP/1.1\r\nHost: x\r\n\r\n", 400),
    "http_1_0": (b"GET / HTTP/1.0\r\nHost: x\r\n\r\n", 400),
    "no_host": (b"GET / HTTP/1.1\r\n\r\n", 400),
    "chunked": (b"GET / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n", 400),
    "long_request_line": (b"GET /" + b"a" * 3000 + b" HTTP/1.1\r\nHost: x\r\n\r\n", 413),
    "long_header": (b"GET / HTTP/1.1\r\nHost: x\r\n" + _headers(1, 9000) + b"\r\n", 413),
    "large_header_block": (b"GET / HTTP/1.1\r\nHost: x\r\n" + _headers(20, 500) + b"\r\n", 413),
    "many_small_headers": (b"GET / HTTP/1.1\r\nHost: x\r\n" + _headers(2000, 1) + b"\r\n", 413),
    "large_body": (b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 70000\r\n\r\n", 413),
    "large_post": (b"POST /decide HTTP/1.1\r\nHost: x\r\nContent-Length: 70000\r\n\r\n", 413),
}


def test_malformed_and_oversized_requests_get_fixed_answers_without_the_daemon(root, daemon,
                                                                                child_env):
    port = free_port()
    names = sorted(BOUNDS)
    replies, done = serve(root, port, child_env, [BOUNDS[name][0] for name in names])
    assert_finished(done)
    for name, reply in zip(names, replies):
        status, headers, body = parse(reply)
        assert status == BOUNDS[name][1], name
        assert_secured(headers)
        # 固定の本文だけで、要求の中身や例外文を映さない
        assert b"a" * 64 not in body and b"Traceback" not in body and b"HELLO" not in body, name
    assert daemon.requests() == []
    # parser の拒否を aiohttp の log (要求の中身を含み得る) として出さない
    assert [line for line in done.stderr.splitlines() if line.strip()] == []


def test_header_block_padded_with_whitespace_is_cut_by_its_wire_size(root, daemon, child_env):
    # aiohttp は値の前後の空白を捨てるので、再構成後は小さい。wire の大きさで数える
    padded = b"".join(b"X-Pad-%d:" % i + b" " * 3500 + b"a" + b" " * 100 + b"\r\n"
                      for i in range(3))
    assert 8192 < len(padded) < 3 * 4096
    split = b"GET / HTTP/1.1\r\nHost: x\r\n" + padded
    whole = split + b"\r\n"
    port = free_port()
    web = Interactive(spec(root, port, requests=3), child_env)
    try:
        web.wait_line("SERVING")
        one = exchange(port, whole)
        # 終端が来ない (header が続いている) うちに上限を超えても切る
        with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
            client.sendall(with_host(split, port))
            two = b""
            while chunk := client.recv(65536):
                two += chunk
        served = exchange(port, get("/"))
        done = web.finish()
    finally:
        web.kill()
    assert_finished(done)
    for reply in (one, two):
        status, headers, body = parse(reply)
        assert status == 413
        assert_secured(headers)
        assert b"a" * 64 not in body
    assert parse(served)[0] == 200
    assert [r["target"] for r in daemon.requests()] == ["/v1/status"]


def test_a_compact_header_block_at_the_wire_limit_is_served(root, daemon, child_env):
    # `Name:value` (区切りに空白なし) で wire が上限ちょうどの要求は、再構成後も上限内
    port = free_port()
    head = f"GET / HTTP/1.1\r\nHost:127.0.0.1:{port}\r\n".encode()
    lines = []
    for i in range(40):
        lines.append(b"X-Pad-%02d:" % i + b"a" * 190 + b"\r\n")
    block = head + b"".join(lines)
    block += b"X-Fill:" + b"b" * (8192 - len(block) - 4 - len(b"X-Fill:")) + b"\r\n"
    raw = block + b"\r\n"
    assert len(raw) == 8192
    replies, done = serve(root, port, child_env, [raw])
    assert_finished(done)
    assert parse(replies[0])[0] == 200
    assert [r["target"] for r in daemon.requests()] == ["/v1/status"]


def test_a_header_flood_beyond_one_request_is_cut_without_reaching_the_daemon(root, daemon,
                                                                             child_env):
    from afx_ops.web.runtime import MAX_CONNECTION_BYTES
    port = free_port()
    flood = _headers(MAX_CONNECTION_BYTES // 12 + 100, 1)
    assert len(flood) > MAX_CONNECTION_BYTES
    web = Interactive(spec(root, port, requests=2), child_env)
    try:
        web.wait_line("SERVING")
        with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
            client.sendall(with_host(b"GET / HTTP/1.1\r\nHost: x\r\n", port))
            try:
                client.sendall(flood)
                cut = client.recv(65536)
            except (ConnectionResetError, BrokenPipeError):
                cut = b""
        served = exchange(port, get("/"))
        done = web.finish()
    finally:
        web.kill()
    assert_finished(done)
    # 応答を返さずに切る (header が溜まり続ける前に)。次の接続は普通に読める
    assert cut == b""
    assert parse(served)[0] == 200
    assert [r["target"] for r in daemon.requests()] == ["/v1/status"]


def test_stopping_while_the_daemon_hangs_returns_within_the_bound(root, daemon, child_env):
    from afx_ops.api_client import HTTP_TIMEOUT
    daemon.set({**ROUTES, "_delay": 60})
    port = free_port()
    web = Interactive(spec(root, port), child_env)
    try:
        web.wait_line("SERVING")
        with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
            client.sendall(with_host(get("/"), port))
            deadline = time.monotonic() + 10
            while not daemon.requests() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert daemon.requests(), "daemon was not asked"
        # 接続を閉じて runtime が止まる。worker は daemon の応答待ち
        started = time.monotonic()
        done = web.finish(timeout=60)
        elapsed = time.monotonic() - started
    finally:
        web.kill()
    assert_finished(done)
    # worker の呼出し自体の期限 (HTTP_TIMEOUT) を待たずに、割り込んで抜ける
    assert elapsed < HTTP_TIMEOUT / 2, elapsed


# --- 表示 ---------------------------------------------------------------------------

SECRET_MARK = "afx-hostile-secret-51d0"
HOSTILE = {
    "/v1/status": {"mode": "<script>alert(1)</script>", "equity": "\"' onmouseover=alert(2)",
                   "secret_token": SECRET_MARK,
                   "health": {"status": "<b>bold</b>", "token": SECRET_MARK},
                   "orders": ["<img src=x onerror=alert(3)>"] * 500,
                   "mission": {"status": [[{"state": "<i>" + "z" * 5000}]]}},
    "/v1/approvals": {"approvals": [{"id": 7, "name": "</title><script>x</script>",
                                     "reason": SECRET_MARK}] * 80},
    "/v1/approvals/{id}": {"id": 7, "status": "<svg onload=alert(4)>", "agent_claim": SECRET_MARK,
                           "exception": f"Traceback {SECRET_MARK}"},
    "/v1/log": {"lines": ["<script>" + "y" * 5000, SECRET_MARK + "\x00\x1b[31m"]},
    "/v1/activity": {"lines": ["</pre><script>z</script>"]},
}


def test_hostile_values_from_the_daemon_are_escaped_and_clipped(root, daemon, child_env):
    daemon.set({**HOSTILE, "/v1/log": {"lines": ["<script>" + "y" * 5000]}})
    port = free_port()
    replies, done = serve(root, port, child_env, [get(path) for path, _, _ in READS])
    assert_finished(done)
    for (path, _, _), reply in zip(READS, replies):
        status, headers, body = parse(reply)
        assert status == 200, path
        assert_secured(headers)
        lowered = body.lower()
        for raw in (b"<script", b"<img", b"<svg", b"<b>", b"<i>", b"\"' onmouseover"):
            assert raw not in lowered, (path, raw)
        assert SECRET_MARK.encode() not in body, path
        assert b"Traceback" not in body and WEB_TOKEN not in body
        assert len(body) <= 64 * 1024
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in parse(replies[0])[2]
    assert b"&quot;&#x27; onmouseover=alert(2)" in parse(replies[0])[2]
    assert b"y" * 513 not in parse(replies[3])[2]


# --- AC-10: listener と処理の順序 ----------------------------------------------------

def test_connection_before_isolation_is_served_only_after_isolation(root, daemon, child_env):
    port = free_port()
    web = Interactive(spec(root, port, pause_after_bind=True,
                           params={"after": [{"op": "listener_view"}]}), child_env)
    client = None
    try:
        web.wait_line("BOUND")
        # 隔離前: 接続と要求は backlog に入るだけで、handler にも daemon にも届かない
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(with_host(get("/"), port))
        client.settimeout(0.5)
        with pytest.raises(TimeoutError):
            client.recv(64)
        assert daemon.requests() == []
        assert "SERVING" not in web.lines
        web.send()
        client.settimeout(10)
        data = b""
        while chunk := client.recv(65536):
            data += chunk
        done = web.finish()
    finally:
        if client is not None:
            client.close()
        web.kill()
    result = assert_finished(done)
    assert parse(data)[0] == 200
    assert [r["target"] for r in daemon.requests()] == ["/v1/status"]
    order = [line for line in done.stdout.splitlines() if line in ("BOUND", "ISOLATED", "SERVING")]
    assert order == ["BOUND", "ISOLATED", "SERVING"]
    assert result["after"] == [{"reuseaddr": True, "type": socket.SOCK_STREAM, "acceptconn": 1,
                                "address": ["127.0.0.1", port], "cloexec": True}]


def test_connection_during_the_authentication_wait_is_served_after_ready(root, tmp_path,
                                                                         child_env):
    port = free_port()
    layout = layout_of(root)
    web = Interactive(spec(root, port, wait={"interval": 0.05, "limit": 20.0}), child_env)
    client = None
    started = None
    try:
        # 隔離と自己検査は済み、server 認証を待っている間に接続して送る
        web.wait_line("WAITING")
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(with_host(get("/logs"), port))
        client.settimeout(0.5)
        with pytest.raises(TimeoutError):
            client.recv(64)
        assert "SERVING" not in web.lines
        started = WebDaemon(layout.socket_path, layout.data, tmp_path)
        client.settimeout(10)
        data = b""
        while chunk := client.recv(65536):
            data += chunk
        done = web.finish()
    finally:
        if client is not None:
            client.close()
        web.kill()
        if started is not None:
            started.close()
    assert_finished(done)
    assert parse(data)[0] == 200 and b"order none" in parse(data)[2]
    assert [r["target"] for r in started.requests()] == ["/v1/log?n=100"]
    order = [line for line in done.stdout.splitlines()
             if line in ("CHECKED-NOHOLD", "WAITING", "ISOLATED", "SERVING")]
    assert order == ["CHECKED-NOHOLD", "WAITING", "ISOLATED", "SERVING"]


def test_fd_exhaustion_ends_the_runtime_with_accept_exhausted(root, daemon, child_env):
    port = free_port()
    web = Interactive(spec(root, port, params={"exhaust": True}), child_env)
    client = None
    try:
        web.wait_line("SERVING")
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(with_host(get("/"), port))
        done = web.finish(timeout=60)
    finally:
        if client is not None:
            client.close()
        web.kill()
    # AcceptLoop の SystemExit は runtime と startup.run を素通りして process を終える
    assert done.rc == RC_ACCEPT_EXHAUSTED, done.stderr[-3000:]
    lines = [line for line in done.stderr.splitlines() if line.startswith("afx-ops:")]
    assert lines[-1] == "afx-ops: accept_exhausted"
    assert "afx-ops: body_failed" not in lines
    assert not any(line.startswith("RESULT ") for line in done.stdout.splitlines())
    assert daemon.requests() == []


# --- AC-7: web の kernel 境界 ---------------------------------------------------------

def test_web_runtime_serves_while_new_sockets_bind_listen_and_messages_are_denied(
        root, daemon, child_env):
    port = free_port()
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen(1)
    after = [
        {"op": "socket", "params": {"family": socket.AF_INET, "type": socket.SOCK_STREAM}},
        {"op": "socket", "params": {"family": socket.AF_INET6, "type": socket.SOCK_STREAM}},
        {"op": "socket", "params": {"family": socket.AF_INET, "type": socket.SOCK_DGRAM}},
        {"op": "bind", "params": {"host": "127.0.0.1"}},
        {"op": "bind", "params": {"host": "0.0.0.0"}},
        {"op": "bind", "params": {"host": "::"}},
        {"op": "listen", "params": {}},
        {"op": "listen", "params": {"listener": True}},
        {"op": "listener_misuse", "params": {"port": other.getsockname()[1]}},
        {"op": "msg_raw", "params": {}},
        {"op": "read_file", "params": {"path": str(root / "keys" / "ops-web.token")}},
        {"op": "read_file", "params": {"path": str(root / "keys" / "ops-bot.token")}},
    ]
    try:
        replies, done = serve(root, port, child_env, [get("/")], params={"after": after})
    finally:
        other.close()
    result = assert_finished(done)
    assert parse(replies[0])[0] == 200
    (inet, inet6, dgram, bind4, bind_any, bind6, listen, relisten, misuse, msg,
     own_key, other_key) = result["after"]
    assert inet == inet6 == dgram == [-1, EACCES]       # 新規 AF_INET / AF_INET6 socket
    assert bind4 == bind_any == bind6 == [-1, EPERM]   # 新規 bind
    assert listen == relisten == [-1, EPERM]            # listen (保持した listener も)
    assert misuse == [-1, EPERM, -1, EPERM, -1, EACCES, -1, EPERM]  # 外向き connect 等
    assert msg == [-1, EPERM, -1, EPERM]                # sendmsg / recvmsg
    assert own_key == other_key == [-1, EACCES]         # 鍵の読み直し
    table = P.feature_filter("web").table
    assert table.action_of("sendmsg") == table.action_of("recvmsg") == "EPERM"


def test_the_send_only_transport_switch_still_exists():
    # runtime は asyncio の sendmsg 経路をこの flag で止める。CPython が名前を変えたら気づく
    assert hasattr(selector_events, "_HAS_SENDMSG")


def _slow_read(port: int, raw: bytes) -> bytes:
    slow = socket.socket()
    try:
        # 受信 buffer を小さくして読まずに待ち、server の送信側に残りを持たせる
        slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        slow.connect(("127.0.0.1", port))
        slow.sendall(with_host(raw, port))
        time.sleep(1.0)
        slow.settimeout(10)
        data = b""
        while chunk := slow.recv(4096):
            data += chunk
        return data
    finally:
        slow.close()


BIG_LOG = {"lines": ["x" * 500] * 50}


def test_a_large_response_to_a_slow_reader_completes(root, daemon, child_env):
    daemon.set({**ROUTES, "/v1/log": BIG_LOG})
    port = free_port()
    web = Interactive(spec(root, port), child_env)
    try:
        web.wait_line("SERVING")
        data = _slow_read(port, get("/logs"))
        done = web.finish()
    finally:
        web.kill()
    assert_finished(done)
    status, headers, body = parse(data)
    assert status == 200 and int(headers["content-length"]) == len(body) > 20000


@needs_strace
def test_runtime_network_syscalls_are_the_fixed_accept_and_no_denied_call(root, daemon,
                                                                         child_env, tmp_path):
    daemon.set({**ROUTES, "/v1/log": BIG_LOG})
    port = free_port()
    trace = tmp_path / "web.trace"
    web = Interactive(spec(root, port, requests=2), child_env,
                      prefix=("strace", "-f", "-qq", "-e", "trace=%network", "-o", str(trace)))
    try:
        web.wait_line("SERVING")
        first = exchange(port, get("/"))
        slow = _slow_read(port, get("/logs"))
        done = web.finish()
    finally:
        web.kill()
    assert_finished(done)
    assert parse(first)[0] == 200 and parse(slow)[0] == 200
    lines = [re.sub(r"^\d+ +", "", line) for line in trace.read_text().splitlines()]
    used = {m.group(1) for line in lines if (m := re.match(r"(accept4?)\(", line))}
    assert used == {ACCEPT_SYSCALL}
    assert not any(line.startswith(("sendmsg(", "recvmsg(")) for line in lines)
    denied = [line for line in lines if re.search(r"= -1 E(PERM|ACCES|NOSYS|AFNOSUPPORT)\b",
                                                  line)]
    assert denied == []
    table = P.feature_filter("web").table
    assert table.action_of(ACCEPT_SYSCALL) == "ALLOW"
    assert table.action_of(({"accept", "accept4"} - {ACCEPT_SYSCALL}).pop()) == "EPERM"


# --- 同時接続の上限 -----------------------------------------------------------------

def test_connections_beyond_the_limit_are_closed_and_the_rest_still_served(root, daemon,
                                                                          child_env):
    from afx_ops.web.runtime import MAX_CONNECTIONS
    port = free_port()
    web = Interactive(spec(root, port, requests=MAX_CONNECTIONS + 1), child_env)
    idle = []
    try:
        web.wait_line("SERVING")
        idle = [socket.create_connection(("127.0.0.1", port), timeout=5)
                for _ in range(MAX_CONNECTIONS)]
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", port), timeout=5)
        extra.settimeout(5)
        try:
            refused = extra.recv(64)
        except ConnectionResetError:
            refused = b""
        extra.close()
        for conn in idle:
            conn.close()
        idle = []
        time.sleep(0.3)
        served = exchange(port, get("/"))
        done = web.finish()
    finally:
        for conn in idle:
            conn.close()
        web.kill()
    assert_finished(done)
    assert refused == b""
    assert parse(served)[0] == 200
    assert [r["target"] for r in daemon.requests()] == ["/v1/status"]


def test_a_pipelined_second_request_on_one_connection_is_not_answered(root, daemon, child_env):
    port = free_port()
    raw = b"GET / HTTP/1.1\r\nHost: x\r\n\r\n" * 2
    replies, done = serve(root, port, child_env, [raw])
    assert_finished(done)
    assert replies[0].count(b"HTTP/1.1 ") == 1
