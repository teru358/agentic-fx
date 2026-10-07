"""実 UDS + 実 process で、peer 検査・上限・socket の準備・500・fd・拒否の集約を試す。

AC-3 / 3b / 6 / 14 / 15 / 16 / 17 / 21 / 22 / 29。
"""
from __future__ import annotations

import os
import resource
import socket
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentic_fx.ops import keys
from agentic_fx.ops.api_server import ApiLimits, ApiServer, ApiStartError
from agentic_fx.ops.contracts import Principal

from ._api import _declare_empty_body, api, make_api  # noqa: F401

CHILD = Path(__file__).with_name("_peer_child.py")


def _no_core():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _child(mode: str, socket_path: Path, token: str, *extra: str,
           wait: bool = True) -> subprocess.Popen | subprocess.CompletedProcess:
    args = [sys.executable, str(CHILD), mode, str(socket_path), *extra]
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")}
    if wait:
        return subprocess.run(args, input=token + "\n", capture_output=True, text=True,
                              timeout=30, env=env, preexec_fn=_no_core)
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                            env=env, preexec_fn=_no_core)
    proc.stdin.write(token + "\n")
    proc.stdin.close()
    return proc


def _activity(api) -> str:
    path = api.env.activity_path
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------- AC-3

def test_ac3_service_child_with_correct_key_is_403_before_reading_body(api):
    token = api.token(Principal.APPROVER)
    result = _child("direct", api.socket_path, token)
    elapsed, _, response = result.stdout.partition("\n")
    assert response.startswith("HTTP/1.1 403"), result.stdout + result.stderr
    assert '"code":"peer_rejected"' in response
    # 本文 10 bytes を送っていないのに即答した = 本文を読む前に拒否している
    assert float(elapsed) < 2.0
    assert api.env.conn.execute("SELECT COUNT(*) FROM ops_policies").fetchone()[0] == 0
    assert api.env.conn.execute("SELECT COUNT(*) FROM ops_requests").fetchone()[0] == 0
    api.server.flush_rejections()
    assert "peer_rejected" in _activity(api)
    assert any("peer_rejected" in text for text in api.notifier.sent)
    assert token not in _activity(api) and token not in result.stdout


def test_ac3_double_fork_escapes_the_descendant_check_known_limit(api, tmp_path):
    token = api.token(Principal.OPERATOR)
    result_file = tmp_path / "double_fork.txt"
    _child("double_fork", api.socket_path, token, str(result_file))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not (result_file.exists()
                                               and result_file.read_text()):
        time.sleep(0.05)
    # 既知の限界: 二重 fork した孫は子孫判定を外れて鍵で通る (§14)。
    assert result_file.read_text().startswith("HTTP/1.1 200"), result_file.read_text()


def test_ac3b_peer_that_exited_before_check_is_403(ops_env):
    api = make_api(ops_env, start=False)
    try:
        api.server.open()
        proc = _child("exit_after_send", api.socket_path, api.token(Principal.APPROVER),
                      wait=False)
        assert proc.wait(10) == 0  # 回収済み: pidfd の Pid は -1 になる
        proc.stdout.close()
        seen = []
        original = api.server._process

        def spy(conn, transport, deadline):
            status, payload = original(conn, transport, deadline)
            seen.append((status, payload))
            return status, payload

        api.server._process = spy
        api.server.serve()
        end = time.monotonic() + 5
        while not seen and time.monotonic() < end:
            time.sleep(0.02)
        assert seen and seen[0][0] == 403
        assert seen[0][1]["error"]["code"] == "peer_rejected"
    finally:
        api.server.stop(5.0)


def test_ac3_pidfd_unavailable_refuses_decide_and_local_guard_only(ops_env):
    api = make_api(ops_env, pidfd_supported=False)
    try:
        client = api.client(Principal.APPROVER)
        approval = ops_env.make_plugin_approval("p1", 1.0)
        assert client.get("/v1/status").status_code == 200
        assert client.post(f"/v1/approvals/{approval}/reject", json={}).status_code == 403
        assert client.post("/v1/killswitch/reconcile", json={}).status_code == 403
        assert client.post("/v1/backlog", json={"idea": "x"},
                           headers={"Idempotency-Key": "k"}).status_code == 200
        assert ops_env.approval(approval)["status"] == "pending"
    finally:
        for client in api.clients:
            client.close()
        api.server.stop(5.0)


# ---------------------------------------------------------------- AC-6

def test_ac6_world_readable_key_principal_is_401_other_works(ops_env):
    directory = keys.key_dir(ops_env.root)
    keys.ensure_initialized(directory, root=ops_env.root)
    os.chmod(directory / keys.key_file_name(Principal.OPERATOR), 0o644)
    operator_token = (directory / keys.key_file_name(Principal.OPERATOR)).read_text().strip()
    api = make_api(ops_env)
    try:
        assert api.client(token=operator_token).get("/v1/status").status_code == 401
        assert api.client(Principal.APPROVER).get("/v1/status").status_code == 200
    finally:
        for client in api.clients:
            client.close()
        api.server.stop(5.0)


# ---------------------------------------------------------------- AC-14

def _status_line(sock: socket.socket, timeout: float = 10.0) -> str:
    sock.settimeout(timeout)
    data = b""
    try:
        while b"\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    except OSError:
        pass
    return data.split(b"\r\n", 1)[0].decode()


def test_ac14_fifth_connection_is_503(api):
    idle = [api.raw() for _ in range(4)]
    try:
        time.sleep(0.2)
        fifth = api.raw()
        assert _status_line(fifth, 2.0).startswith("HTTP/1.1 503")
        fifth.close()
    finally:
        for sock in idle:
            sock.close()


def test_ac14_body_over_64k_is_413(api):
    client = api.client(Principal.OPERATOR)
    response = client.post("/v1/policy", content=b"x" * (65536 + 1),
                           headers={"Idempotency-Key": "k",
                                    "Content-Type": "application/json"})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_ac14_chunked_and_missing_length_are_rejected(api):
    token = api.token(Principal.OPERATOR)
    for head in ("POST /v1/policy HTTP/1.1\r\nTransfer-Encoding: chunked\r\n",
                 "POST /v1/policy HTTP/1.1\r\n"):
        sock = api.raw()
        sock.sendall((head + f"Authorization: Bearer {token}\r\n"
                      "Idempotency-Key: k\r\n\r\n").encode())
        line = _status_line(sock)
        sock.close()
        assert line.split()[1] in ("400", "411"), line


def test_ac14_get_without_content_length_is_411_and_zero_is_accepted(api):
    token = api.token(Principal.OPERATOR)
    head = f"GET /v1/whoami HTTP/1.1\r\nHost: afx\r\nAuthorization: Bearer {token}\r\n"
    sock = api.raw()
    sock.sendall((head + "\r\n").encode())
    assert _status_line(sock).split()[1] == "411"
    sock.close()
    sock = api.raw()
    sock.sendall((head + "Content-Length: 0\r\n\r\n").encode())
    assert _status_line(sock).split()[1] == "200"
    sock.close()


def _slowloris(api, results: list, index: int) -> None:
    sock = api.raw()
    started = time.monotonic()
    sock.setblocking(True)
    try:
        for byte in b"GET /v1/status HTTP/1.1\r\nHost: afx\r\n":
            try:
                sock.send(bytes([byte]))
            except OSError:
                break
            readable = _poll(sock, 1.0)
            if readable:
                break
        line = _status_line(sock, 10.0)
    finally:
        sock.close()
    results[index] = (line, time.monotonic() - started)


def _poll(sock: socket.socket, timeout: float) -> bool:
    import select
    readable, _, _ = select.select([sock], [], [], timeout)
    return bool(readable)


def test_ac14_slowloris_gets_408_within_5_5_seconds(api):
    results = [None]
    _slowloris(api, results, 0)
    line, elapsed = results[0]
    assert line.startswith("HTTP/1.1 408"), line
    assert elapsed <= 5.5


def test_ac14_silent_connection_gets_408_within_5_5_seconds(api):
    sock = api.raw()
    started = time.monotonic()
    line = _status_line(sock, 10.0)
    sock.close()
    assert line.startswith("HTTP/1.1 408"), line
    assert time.monotonic() - started <= 5.5


def test_ac14_status_under_four_slowloris_is_503_or_fast(api):
    results = [None] * 4
    threads = [threading.Thread(target=_slowloris, args=(api, results, i)) for i in range(4)]
    for thread in threads:
        thread.start()
    time.sleep(0.3)
    client = api.client(Principal.OPERATOR)
    started = time.monotonic()
    response = client.get("/v1/status")
    assert response.status_code == 503 or time.monotonic() - started <= 5.5
    for thread in threads:
        thread.join(15)
    assert all(r is not None and r[0].startswith("HTTP/1.1 408") for r in results)


def test_ac14_tick_latency_is_not_disturbed_by_slowloris(api, tmp_path):
    conn = sqlite3.connect(tmp_path / "tick.db")
    conn.execute("CREATE TABLE t(x)")

    def tick_latencies(n: int) -> list[float]:
        out = []
        for i in range(n):
            started = time.perf_counter()
            conn.execute("INSERT INTO t VALUES (?)", (i,))
            conn.commit()
            sum(range(2000))
            out.append(time.perf_counter() - started)
            time.sleep(0.002)
        return out

    baseline = sorted(tick_latencies(200))
    results = [None] * 4
    attackers = [threading.Thread(target=_slowloris, args=(api, results, i))
                 for i in range(4)]
    for thread in attackers:
        thread.start()
    time.sleep(0.2)
    loaded = sorted(tick_latencies(200))
    for thread in attackers:
        thread.join(15)
    conn.close()

    def pct(values, q):
        return values[min(len(values) - 1, int(len(values) * q))]

    assert pct(loaded, 0.95) - pct(baseline, 0.95) <= 0.020
    assert pct(loaded, 0.99) - pct(baseline, 0.99) <= 0.050
    assert max(loaded) <= 0.5
    assert statistics.mean(loaded) < 0.5


# ---------------------------------------------------------------- AC-15 / 16

def test_ac15_regular_file_at_socket_path_fails_to_start(ops_env):
    path = ops_env.root / "data" / "run" / "api.sock"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text("not a socket")
    api = make_api(ops_env, start=False)
    with pytest.raises(ApiStartError) as err:
        api.server.start()
    assert err.value.code == "socket_path_occupied"
    assert path.read_text() == "not a socket"


def test_ac16_stale_socket_file_is_removed_and_bound(ops_env):
    path = ops_env.root / "data" / "run" / "api.sock"
    path.parent.mkdir(parents=True, mode=0o700)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()  # 前回の process が unlink せずに落ちた状態
    assert path.exists()
    api = make_api(ops_env)
    try:
        assert api.client(Principal.OPERATOR).get("/v1/whoami").status_code == 200
        import stat as stat_mod
        assert stat_mod.S_IMODE(path.stat().st_mode) == 0o600
        assert stat_mod.S_IMODE(path.parent.stat().st_mode) == 0o700
    finally:
        for client in api.clients:
            client.close()
        api.server.stop(5.0)
    assert not path.exists()


def test_ac16_live_socket_is_not_removed(api, ops_env):
    second = ApiServer(api.service, keys.load_keyset(api.key_dir), api.socket_path)
    with pytest.raises(ApiStartError) as err:
        second.start()
    assert err.value.code == "socket_in_use"
    assert api.client(Principal.OPERATOR).get("/v1/whoami").status_code == 200


# ---------------------------------------------------------------- AC-17

def test_ac17_internal_error_returns_incident_only_and_logs_it(api, caplog):
    def broken(principal):
        raise RuntimeError("secret internal detail /home/x")

    api.service.whoami = broken
    with caplog.at_level("ERROR", logger="agentic_fx.ops.api_server"):
        response = api.client(Principal.OPERATOR).get("/v1/whoami")
    assert response.status_code == 500
    assert "secret internal detail" not in response.text
    error = response.json()["error"]
    assert error["code"] == "internal" and len(error["incident"]) == 16
    records = [r for r in caplog.records if error["incident"] in r.getMessage()]
    assert records and "secret internal detail" in str(records[0].exc_info[1])


# ---------------------------------------------------------------- AC-21

def _fds() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


def test_ac21_open_fds_are_the_same_after_start_100_requests_stop(ops_env):
    # 遅延 import や logging の初回生成を先に済ませる
    warm = make_api(ops_env)
    warm.client(Principal.OPERATOR).get("/v1/whoami")
    for client in warm.clients:
        client.close()
    warm.server.stop(5.0)
    warm.service.shutdown(join_timeout=2.0)
    before = _fds()
    api = make_api(ops_env)
    client = api.client(Principal.OPERATOR)
    for i in range(100):
        path = ("/v1/whoami", "/v1/status", "/v1/events?after=0&limit=5")[i % 3]
        assert client.get(path).status_code == 200
    bad = api.client(token="0" * 64)
    for _ in range(5):
        assert bad.get("/v1/status").status_code == 401
    for c in api.clients:
        c.close()
    assert api.server.stop(5.0)
    api.service.shutdown(join_timeout=2.0)
    assert _fds() == before


# ---------------------------------------------------------------- AC-22

def test_ac22_hundred_401_in_a_minute_is_five_records_one_summary_one_notify(api):
    from datetime import datetime, timezone
    api.env.wall.now = datetime(2026, 10, 5, 0, 0, 1, tzinfo=timezone.utc)
    bad = api.client(token="0" * 64)
    for _ in range(100):
        assert bad.get("/v1/status").status_code == 401
    api.env.wall.now = datetime(2026, 10, 5, 0, 0, 59, tzinfo=timezone.utc)
    api.server.flush_rejections()
    lines = _activity(api).splitlines()
    individual = [l for l in lines if "authentication_failed" in l and "suppressed" not in l]
    assert len(individual) == 5
    assert not [l for l in lines if "suppressed" in l]  # 分境界前は集約しない
    api.env.wall.now = datetime(2026, 10, 5, 0, 1, 0, tzinfo=timezone.utc)
    api.server.flush_rejections()
    summary = [l for l in _activity(api).splitlines() if "suppressed" in l]
    assert len(summary) == 1 and "95 suppressed" in summary[0]
    assert len([t for t in api.notifier.sent if "authentication_failed" in t]) == 1


def test_ac22_shutdown_flushes_suppressed_count(api):
    bad = api.client(token="0" * 64)
    for _ in range(8):
        bad.get("/v1/status")
    api.server.stop(5.0)
    api.service.shutdown(join_timeout=2.0)
    summary = [l for l in _activity(api).splitlines() if "suppressed" in l]
    assert len(summary) == 1 and "3 suppressed" in summary[0]


# ---------------------------------------------------------------- AC-29

def test_ac29_local_guard_is_404_on_a_tcp_listener(api):
    import httpx
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def accept():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            api.server.dispatch(conn, "tcp")

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, headers={
            "Authorization": "Bearer " + api.token(Principal.APPROVER)},
            event_hooks={"request": [_declare_empty_body]})
        for path, body in (("/v1/killswitch/reset", {"expected_generation": 0}),
                           ("/v1/killswitch/reconcile", {}),
                           ("/v1/data/resume", {"acknowledge": True})):
            response = client.post(path, json=body)
            assert response.status_code == 404, path
        assert client.get("/v1/whoami").status_code == 200
        client.close()
    finally:
        stop.set()
        thread.join(5)
        listener.close()


def _tcp_listen_inodes() -> set[str]:
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table) as file:
                next(file)
                for line in file:
                    parts = line.split()
                    if parts[3] == "0A":  # LISTEN
                        inodes.add(parts[9])
        except FileNotFoundError:
            continue
    return inodes


def _own_socket_inodes() -> set[str]:
    out = set()
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith("socket:["):
            out.add(target[len("socket:["):-1])
    return out


def test_ac29_server_creates_no_tcp_listener(api):
    assert api.client(Principal.OPERATOR).get("/v1/whoami").status_code == 200
    assert not (_tcp_listen_inodes() & _own_socket_inodes())
