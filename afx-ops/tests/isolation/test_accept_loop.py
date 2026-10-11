"""web の listener: pre-bind の検査、隔離後の自前 accept loop、accept syscall の固定。"""
from __future__ import annotations

import os
import re
import socket
import time

import pytest

from _afx_isolation_support import (
    Interactive,
    layout_of,
    make_root,
    needs_landlock,
    needs_strace,
    needs_x86_64,
    run_canary,
)
from afx_ops.isolation import profiles as P
from afx_ops.isolation.accept_loop import (
    ACCEPT_BACKOFF,
    ACCEPT_EXHAUSTED_LIMIT,
    ACCEPT_SYSCALL,
    RC_ACCEPT_EXHAUSTED,
    AcceptLoop,
    ListenerError,
    check_listener,
    prebind_listener,
)

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spec(root, op, **extra):
    return {"feature": "web", "root": str(root), "op": op, "params": extra.pop("params", {}),
            **extra}


# --- listener の検査 (隔離なし、実 socket) -------------------------------------

def test_prebind_listener_is_loopback_listening_and_cloexec():
    sock = prebind_listener(0)
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
        assert not sock.get_inheritable()
        check_listener(sock, sock.getsockname()[1])
        with pytest.raises(ListenerError):
            check_listener(sock, sock.getsockname()[1] + 1 or 1)
    finally:
        sock.close()


def test_prebind_listener_rebinds_while_the_previous_connection_is_in_time_wait():
    # systemd の再起動を模す: 接続を処理した listener が先に閉じた (server 側が TIME_WAIT)
    first = prebind_listener(0)
    port = first.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    conn, _ = first.accept()
    conn.close()  # server 側から先に閉じる
    client.recv(1)
    client.close()
    first.close()
    second = prebind_listener(port)
    try:
        assert second.getsockname() == ("127.0.0.1", port)
        assert second.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0
    finally:
        second.close()


def test_reuseaddr_does_not_take_over_a_live_listener():
    live = prebind_listener(0)
    try:
        with pytest.raises(OSError):
            prebind_listener(live.getsockname()[1])
    finally:
        live.close()


def _listener(host="127.0.0.1", listen=True, inheritable=False, kind=socket.SOCK_STREAM):
    sock = socket.socket(socket.AF_INET, kind)
    sock.bind((host, 0))
    if listen:
        sock.listen(1)
    sock.set_inheritable(inheritable)
    return sock


@pytest.mark.parametrize("variant", ["any_address", "not_listening", "inheritable", "dgram"])
def test_check_listener_rejects(variant):
    sock = {"any_address": lambda: _listener(host="0.0.0.0"),
            "not_listening": lambda: _listener(listen=False),
            "inheritable": lambda: _listener(inheritable=True),
            "dgram": lambda: _listener(listen=False, kind=socket.SOCK_DGRAM)}[variant]()
    try:
        with pytest.raises(ListenerError) as info:
            check_listener(sock, 0)
        assert info.value.reason == "listener_invalid"
    finally:
        sock.close()


def test_accept_ready_hands_every_pending_connection_to_the_callback():
    listener = prebind_listener(0)
    port = listener.getsockname()[1]
    clients = [socket.create_connection(("127.0.0.1", port)) for _ in range(3)]
    got = []
    try:
        loop = AcceptLoop(listener, got.append)
        deadline = time.monotonic() + 5
        while len(got) < 3 and time.monotonic() < deadline:
            loop.accept_ready()
        assert len(got) == 3
        assert loop.accept_ready() == 0
    finally:
        for c in clients + got:
            c.close()
        listener.close()


def test_a_failing_callback_closes_the_connection_and_the_rest_are_still_taken():
    listener = prebind_listener(0)
    port = listener.getsockname()[1]
    clients = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(2)]
    seen = []

    def explode(conn):
        seen.append(conn.fileno())
        raise RuntimeError("callback failed")
    try:
        time.sleep(0.05)
        before = set(os.listdir(f"/proc/{os.getpid()}/fd"))
        loop = AcceptLoop(listener, explode)
        assert loop.accept_ready() == 2
        assert len(seen) == 2 and loop.callback_errors == 2
        # 受けた接続は閉じている (fd が増えず、相手には EOF が届く)
        assert set(os.listdir(f"/proc/{os.getpid()}/fd")) == before
        assert [c.recv(1) for c in clients] == [b"", b""]
    finally:
        for c in clients:
            c.close()
        listener.close()


_EMFILE_CHILD = r"""
import asyncio, json, os, resource, socket, sys
from afx_ops.isolation.accept_loop import AcceptLoop, prebind_listener

listener = prebind_listener(0)
client = socket.create_connection(("127.0.0.1", listener.getsockname()[1]))
calls, got = [0], []


class Counting(AcceptLoop):
    def accept_ready(self):
        calls[0] += 1
        return super().accept_ready()


async def main():
    loop = asyncio.get_running_loop()
    acc = Counting(listener, got.append)
    _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
    fillers = []
    try:
        while True:
            fillers.append(os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC))
    except OSError:
        pass
    acc.attach(loop)
    await asyncio.sleep(0.5)
    starved = calls[0]
    for fd in fillers:
        os.close(fd)
    await asyncio.sleep(0.3)
    acc.detach(loop)
    return {"starved_calls": starved, "accepted": len(got),
            "resource_errors": acc.resource_errors}

print(json.dumps(asyncio.run(main())))
"""


def test_fd_exhaustion_backs_off_instead_of_spinning_and_recovers():
    import json
    import resource
    import subprocess
    import sys

    def no_core():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    done = subprocess.run([sys.executable, "-c", _EMFILE_CHILD], capture_output=True,
                          text=True, timeout=30, preexec_fn=no_core)
    assert done.returncode == 0, done.stderr[-2000:]
    got = json.loads(done.stdout.strip().splitlines()[-1])
    # 0.5 秒の間、50 ms 間隔の再試行なら十数回に収まる (busy loop なら数千回)
    assert 1 <= got["starved_calls"] <= 20, got
    assert got["resource_errors"] >= 1
    # fd が空けば同じ接続を受けられる
    assert got["accepted"] == 1, got


class _Scripted:
    """accept の結果を順に返す listener の代わり (close の回数を数える)。"""

    def __init__(self, script):
        self._script = list(script)
        self.closed = 0

    def setblocking(self, flag):
        pass

    def fileno(self):
        return -1

    def accept(self):
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def close(self):
        self.closed += 1


def _emfile():
    import errno
    return OSError(errno.EMFILE, "Too many open files")


def test_the_retry_limit_covers_about_ten_seconds_of_exhaustion():
    assert ACCEPT_EXHAUSTED_LIMIT * ACCEPT_BACKOFF == pytest.approx(10.0)


def test_consecutive_fd_exhaustion_up_to_the_limit_closes_the_listener_and_gives_up():
    listener = _Scripted([_emfile(), _emfile(), _emfile()])
    gave_up = []
    acc = AcceptLoop(listener, lambda c: None, exhausted_limit=3,
                     on_exhausted=lambda: gave_up.append(listener.closed))
    acc.accept_ready()
    acc.accept_ready()
    assert gave_up == [] and listener.closed == 0
    acc.accept_ready()
    # listener を閉じてから終了の処理へ渡す
    assert gave_up == [1]
    assert acc.resource_errors == 3


def test_a_successful_accept_resets_the_exhaustion_count():
    conn = socket.socket()
    try:
        listener = _Scripted([_emfile(), _emfile(), (conn, ("127.0.0.1", 1)), _emfile(),
                              _emfile()])
        gave_up = []
        acc = AcceptLoop(listener, lambda c: None, exhausted_limit=3,
                         on_exhausted=lambda: gave_up.append(True))
        acc.accept_ready()
        acc.accept_ready()
        acc.accept_ready()  # 受けてから次の EMFILE
        acc.accept_ready()
        assert gave_up == [] and listener.closed == 0
    finally:
        conn.close()


_EXHAUSTED_CHILD = r"""
import asyncio, os, resource, socket
from afx_ops.isolation.accept_loop import AcceptLoop, prebind_listener

listener = prebind_listener(0)
client = socket.create_connection(("127.0.0.1", listener.getsockname()[1]))


async def main():
    loop = asyncio.get_running_loop()
    acc = AcceptLoop(listener, lambda c: None, exhausted_limit=5)
    _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
    try:
        while True:
            os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        pass
    acc.attach(loop)
    await asyncio.sleep(5)
    print("still running", flush=True)

asyncio.run(main())
"""


def test_fd_exhaustion_beyond_the_limit_ends_the_process_with_a_fixed_reason():
    import resource
    import subprocess
    import sys

    def no_core():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    done = subprocess.run([sys.executable, "-c", _EXHAUSTED_CHILD], capture_output=True,
                          text=True, timeout=30, preexec_fn=no_core)
    assert done.returncode == RC_ACCEPT_EXHAUSTED, done.stderr[-2000:]
    assert "still running" not in done.stdout
    assert done.stderr.strip().splitlines()[-1] == "afx-ops: accept_exhausted"


# --- 隔離下 --------------------------------------------------------------------

@needs_x86_64
@needs_landlock
def test_web_cannot_listen_again_after_isolation(tmp_path, child_env):
    root = make_root(tmp_path)
    got = run_canary(_spec(root, "listen", params={"listener": True}), child_env)
    assert got.rc == 0, got.stderr[-2000:]
    assert got.result == [-1, 1]  # EPERM: aiohttp の create_server / SockSite は使えない


@needs_x86_64
@needs_landlock
def test_connection_before_isolation_waits_until_self_check(tmp_path, child_env):
    root = make_root(tmp_path)
    port = _free_port()
    web = Interactive(_spec(root, "serve", web_port=port, pause_after_bind=True), child_env)
    client = None
    try:
        web.wait_line("BOUND")
        # 隔離前: connect と送信は backlog に入るだけで、handler には届かない
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(b"early")
        client.settimeout(0.5)
        with pytest.raises(TimeoutError):
            client.recv(64)
        assert not any(line in ("ACCEPTED", "HANDLED") for line in web.lines)
        web.send()
        client.settimeout(10)
        assert client.recv(64) == b"handled:early"
        done = web.finish()
        assert done.rc == 0, done.stderr[-2000:]
        lines = [line for line in done.stdout.splitlines()
                 if line in ("BOUND", "ISOLATED", "SERVING", "ACCEPTED", "HANDLED")]
        assert lines == ["BOUND", "ISOLATED", "SERVING", "ACCEPTED", "HANDLED"]
    finally:
        if client is not None:
            client.close()
        web.kill()


@needs_x86_64
@needs_landlock
def test_web_connection_during_authentication_wait_is_accepted_only_by_the_body(tmp_path,
                                                                               child_env):
    from _ops_harness import start, stop
    root = make_root(tmp_path)
    layout = layout_of(root)
    port = _free_port()
    web = Interactive(_spec(root, "serve", web_port=port,
                            wait={"interval": 0.05, "limit": 20.0}), child_env)
    client = None
    daemon = None
    try:
        # 隔離と自己検査は済み、server 認証を待っている間に接続して送る
        web.wait_line("WAITING")
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(b"waiting")
        client.settimeout(0.5)
        with pytest.raises(TimeoutError):
            client.recv(64)
        assert not any(line in ("ISOLATED", "ACCEPTED", "HANDLED") for line in web.lines)
        daemon = start(layout)
        client.settimeout(10)
        assert client.recv(64) == b"handled:waiting"
        done = web.finish()
        assert done.rc == 0, done.stderr[-2000:]
        lines = [line for line in done.stdout.splitlines()
                 if line in ("CHECKED-NOHOLD", "WAITING", "ISOLATED", "SERVING", "ACCEPTED",
                             "HANDLED")]
        assert lines == ["CHECKED-NOHOLD", "WAITING", "ISOLATED", "SERVING", "ACCEPTED",
                         "HANDLED"]
    finally:
        if client is not None:
            client.close()
        web.kill()
        stop(daemon)


@needs_x86_64
@needs_landlock
@needs_strace
def test_accept_syscall_is_fixed_by_trace(tmp_path, child_env):
    root = make_root(tmp_path)
    port = _free_port()
    trace = tmp_path / "accept.trace"
    web = Interactive(_spec(root, "serve", web_port=port), child_env,
                      prefix=("strace", "-f", "-qq", "-e", "trace=accept,accept4",
                              "-o", str(trace)))
    try:
        web.wait_line("SERVING")
        with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
            client.sendall(b"one")
            assert client.recv(64) == b"handled:one"
        done = web.finish()
        assert done.rc == 0, done.stderr[-2000:]
    finally:
        web.kill()
    used = {m.group(1) for line in trace.read_text().splitlines()
            if (m := re.match(r"^\d+\s+(accept4?)\(", line))}
    assert used == {ACCEPT_SYSCALL}
    table = P.feature_filter("web").table
    assert table.action_of(ACCEPT_SYSCALL) == "ALLOW"
    other = ({"accept", "accept4"} - {ACCEPT_SYSCALL}).pop()
    assert table.action_of(other) == "EPERM"
    for feature in ("bot", "notify"):
        t = P.feature_filter(feature).table
        assert t.action_of("accept") == t.action_of("accept4") == "EPERM"
