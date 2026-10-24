"""設定した address で待ち受ける隔離済みの実 process (本機の実 IPv4 と loopback、外へは出ない)。"""
from __future__ import annotations

import errno
import re
import socket
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE.parent / "isolation") not in sys.path:
    sys.path.insert(0, str(HERE.parent / "isolation"))

from _afx_isolation_support import (  # noqa: E402
    Interactive,
    needs_landlock,
    needs_strace,
    needs_x86_64,
)
from afx_ops import interfaces  # noqa: E402
from afx_ops.isolation.accept_loop import ACCEPT_SYSCALL  # noqa: E402
from test_runtime_isolated import (  # noqa: E402,F401
    assert_finished,
    daemon,
    free_port,
    parse,
    root,
    spec,
)

pytestmark = [needs_x86_64, needs_landlock]
EACCES, EPERM = errno.EACCES, errno.EPERM


@pytest.fixture
def lan() -> str:
    found = interfaces.list_interfaces()
    if not found:
        pytest.skip("この host に global / link の IPv4 が無い")
    return found[0].address


def talk(address: str, port: int, raw: bytes, *, source: str | None = None) -> bytes:
    """1 接続で raw を送り、閉じられるまでの応答を読む。"""
    client = socket.socket()
    try:
        client.settimeout(10)
        if source is not None:
            client.bind((source, 0))
        client.connect((address, port))
        client.sendall(raw)
        data = b""
        try:
            while chunk := client.recv(65536):
                data += chunk
        except ConnectionResetError:
            pass
        return data
    finally:
        client.close()


def request(host: str, path: str = "/", method: str = "GET") -> bytes:
    return f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()


def test_a_lan_bind_serves_only_its_own_host_and_nothing_on_loopback(root, daemon, child_env, lan):
    port = free_port()
    after = [
        {"op": "socket", "params": {"family": socket.AF_INET, "type": socket.SOCK_STREAM}},
        {"op": "socket", "params": {"family": socket.AF_INET6, "type": socket.SOCK_STREAM}},
        {"op": "bind", "params": {"host": lan}},
        {"op": "bind", "params": {"host": "0.0.0.0"}},
        {"op": "listen", "params": {}},
        {"op": "listen", "params": {"listener": True}},
        {"op": "web_fds", "params": {}},
    ]
    web = Interactive(spec(root, port, requests=2, web_bind=lan, web_allow=[f"{lan}/32"],
                           params={"after": after}), child_env)
    try:
        web.wait_line("SERVING")
        # listener は設定の address だけで、loopback の同じ port には届かない
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=5)
        ok = talk(lan, port, request(f"{lan}:{port}"))
        wrong = talk(lan, port, request(f"127.0.0.1:{port}"))
        done = web.finish()
    finally:
        web.kill()
    result = assert_finished(done)
    assert parse(ok)[0] == 200 and parse(wrong)[0] == 400
    # Host が合わない要求は daemon に届かない
    assert [r["target"] for r in daemon.requests()] == ["/v1/status"]
    inet, inet6, bind_lan, bind_any, listen, relisten, fds = result["after"]
    assert inet == inet6 == [-1, EACCES]
    assert bind_lan == bind_any == [-1, EPERM]
    assert listen == relisten == [-1, EPERM]
    # 隔離後に保持している fd は {0,1,2,listener} だけで、子 process も残っていない
    assert fds["fds"] == sorted({0, 1, 2, fds["listener"]}) == fds["kept"]
    assert fds["children"] == 0


def test_a_peer_outside_allow_gets_no_bytes_on_a_lan_bind(root, daemon, child_env, lan):
    port = free_port()
    web = Interactive(spec(root, port, requests=1, web_bind=lan, web_allow=["10.255.255.0/24"]),
                      child_env)
    try:
        web.wait_line("SERVING")
        received = talk(lan, port, request(f"{lan}:{port}"))
    finally:
        web.kill()
    assert received == b""
    assert daemon.requests() == []


def test_a_non_default_loopback_address_is_a_valid_bind_with_its_own_host(root, daemon,
                                                                         child_env):
    port = free_port()
    web = Interactive(spec(root, port, requests=3, web_bind="127.0.0.2",
                           web_allow=["127.0.0.1/32"]), child_env)
    try:
        web.wait_line("SERVING")
        # allow は 127.0.0.1 だけ。送信元 127.0.0.3 は黙って閉じられる
        denied = talk("127.0.0.2", port, request(f"127.0.0.2:{port}"), source="127.0.0.3")
        by_address = talk("127.0.0.2", port, request(f"127.0.0.2:{port}"), source="127.0.0.1")
        by_name = talk("127.0.0.2", port, request(f"LocalHost:{port}"), source="127.0.0.1")
        default_name = talk("127.0.0.2", port, request(f"127.0.0.1:{port}"), source="127.0.0.1")
        done = web.finish()
    finally:
        web.kill()
    assert_finished(done)
    assert denied == b""
    assert parse(by_address)[0] == 200 and parse(by_name)[0] == 200
    assert parse(default_name)[0] == 400


@needs_strace
def test_lan_runtime_network_syscalls_are_the_fixed_accept_and_no_denied_call(
        root, daemon, child_env, tmp_path, lan):
    port = free_port()
    trace = tmp_path / "lan.trace"
    web = Interactive(spec(root, port, requests=1, web_bind=lan, web_allow=[f"{lan}/32"]),
                      child_env,
                      prefix=("strace", "-f", "-qq", "-e", "trace=%network", "-o", str(trace)))
    try:
        web.wait_line("SERVING")
        reply = talk(lan, port, request(f"{lan}:{port}"))
        done = web.finish()
    finally:
        web.kill()
    assert_finished(done)
    assert parse(reply)[0] == 200
    raw = trace.read_text().splitlines()
    # `ip` の子 process (隔離前に 1 回だけ列挙する) は同じ trace に載るので、accept した process だけを見る
    main = next(line.split()[0] for line in raw if re.search(r" accept4?\(", line))
    lines = [re.sub(r"^\d+ +", "", line) for line in raw if line.split()[0] == main]
    assert {m.group(1) for line in lines if (m := re.match(r"(accept4?)\(", line))} == {
        ACCEPT_SYSCALL}
    assert any(line.startswith("getpeername(") for line in lines)
    assert not any(line.startswith(("sendmsg(", "recvmsg(")) for line in lines)
    assert [line for line in lines
            if re.search(r"= -1 E(PERM|ACCES|NOSYS|AFNOSUPPORT)\b", line)] == []


def test_a_lan_connection_queued_before_isolation_is_served_only_after_ready(root, daemon,
                                                                            child_env, lan):
    """M-T1-PRE-READY-ACCEPT: READY 前に backlog を処理する変異は落ちる。"""
    port = free_port()
    web = Interactive(spec(root, port, web_bind=lan, web_allow=[f"{lan}/32"],
                           pause_after_bind=True, params={"after": [{"op": "listener_view"}]}),
                      child_env)
    client = None
    try:
        web.wait_line("BOUND")
        client = socket.create_connection((lan, port), timeout=5)
        client.sendall(request(f"{lan}:{port}"))
        client.settimeout(0.5)
        with pytest.raises(TimeoutError):
            client.recv(64)
        assert daemon.requests() == [] and "SERVING" not in web.lines
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
    assert result["after"][0]["address"] == [lan, port]


def test_a_duplicate_or_missing_host_is_400_through_the_real_http_stack(root, daemon, child_env):
    """aiohttp の `raw_headers` から adapter までの配線で、重複 / 欠落 / 大小違いの Host を見る。"""
    port = free_port()
    good = f"127.0.0.1:{port}"
    raws = [
        f"GET / HTTP/1.1\r\nHost: {good}\r\nHost: {good}\r\nConnection: close\r\n\r\n".encode(),
        f"GET / HTTP/1.1\r\nHost: {good}\r\nhost: 10.0.0.1\r\nConnection: close\r\n\r\n".encode(),
        b"GET / HTTP/1.1\r\nConnection: close\r\n\r\n",
        f"HEAD / HTTP/1.1\r\nHost: {good}\r\nHOST: {good}\r\nConnection: close\r\n\r\n".encode(),
        f"GET / HTTP/1.1\r\nhOsT:   {good}\t\r\nConnection: close\r\n\r\n".encode(),
    ]
    web = Interactive(spec(root, port, requests=len(raws)), child_env)
    try:
        web.wait_line("SERVING")
        replies = [talk("127.0.0.1", port, raw) for raw in raws]
        done = web.finish()
    finally:
        web.kill()
    assert_finished(done)
    statuses = [parse(reply)[0] for reply in replies]
    assert statuses == [400, 400, 400, 400, 200]
    assert replies[3].split(b"\r\n\r\n", 1)[1] == b""
    assert [r["target"] for r in daemon.requests()] == ["/v1/status"]
