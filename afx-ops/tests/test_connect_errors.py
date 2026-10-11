"""接続段の失敗の分類: 待ってよい一時的な失敗と、即座に止める恒久的な失敗を分ける。

socket・権限・fd 枯渇は実 kernel の応答で作る (mode 0 の socket file、RLIMIT_NOFILE を
下げた子 process)。
"""
from __future__ import annotations

import json
import os
import resource
import socket
import subprocess
import sys
import time

import pytest
from _ops_harness import pidfd_supported

from afx_ops import api_client
from afx_ops.api_client import ApiError, Endpoint


def _socket_dir(tmp_path):
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    return run


def test_a_socket_without_permission_fails_wait_ready_at_once(tmp_path):
    run = _socket_dir(tmp_path)
    path = run / "afx.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(4)
    path.chmod(0)  # connect は EACCES (同 uid でも socket file の書込み権限が要る)
    slept = []
    try:
        began = time.monotonic()
        with pytest.raises(ApiError) as info:
            api_client.wait_ready(Endpoint(path, run / "instance.lock"), interval=0.05,
                                  limit=10.0, sleep=slept.append)
        elapsed = time.monotonic() - began
    finally:
        server.close()
    assert info.value.reason == "socket_unusable"
    assert not info.value.token_sent
    assert slept == [] and elapsed < 2


def test_a_refused_connection_is_still_waited_for(tmp_path):
    run = _socket_dir(tmp_path)
    path = run / "afx.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()  # listen していない socket file (ECONNREFUSED)
    slept = []
    with pytest.raises(ApiError) as info:
        api_client.wait_ready(Endpoint(path, run / "instance.lock"), interval=0.05,
                              limit=0.3, sleep=lambda s: (slept.append(s), time.sleep(s)))
    assert info.value.reason == "readiness_timeout"
    assert len(slept) >= 2


def test_wait_ready_retries_a_transient_pidfd_failure(monkeypatch, tmp_path):
    outcomes = [ApiError("pidfd_transient"), ApiError("pidfd_transient"), None]

    class _Opened:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    def fake_open(endpoint, *, timeout):
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        return _Opened()
    monkeypatch.setattr(api_client._Request, "open", staticmethod(fake_open))
    slept = []
    api_client.wait_ready(Endpoint(tmp_path / "s", tmp_path / "l"), interval=0.01,
                          limit=5.0, sleep=slept.append)
    assert outcomes == [] and len(slept) == 2


_EMFILE_CHILD = r"""
import json, os, resource, socket
from afx_ops import server_auth

a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
_soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
fillers = []
try:
    while True:
        fillers.append(os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC))
except OSError:
    pass
try:
    fd = server_auth.fetch_pidfd(a)
except server_auth.ServerAuthError as err:
    result = err.reason
else:
    os.close(fd)
    result = "ok"
for fd in fillers:
    os.close(fd)
print(json.dumps(result))
"""


@pytest.mark.skipif(not pidfd_supported(), reason="SO_PEERPIDFD の無い kernel (< 6.5)")
def test_fd_exhaustion_while_taking_the_pidfd_is_not_reported_as_an_old_kernel():
    def no_core():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    done = subprocess.run([sys.executable, "-c", _EMFILE_CHILD], capture_output=True,
                          text=True, timeout=30, preexec_fn=no_core)
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout) == "pidfd_transient"


def _full_backlog_listener(path):
    """accept しない listener の backlog を埋めた状態 (次の connect は kernel で待たされる)。"""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(0)
    fillers = []
    while True:
        filler = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        filler.setblocking(False)
        try:
            filler.connect(str(path))
        except BlockingIOError:
            filler.close()
            break
        fillers.append(filler)
    return server, fillers


def test_connect_to_a_listener_that_never_accepts_times_out(tmp_path, monkeypatch):
    run = _socket_dir(tmp_path)
    path = run / "afx.sock"
    server, fillers = _full_backlog_listener(path)
    monkeypatch.setattr(api_client, "HTTP_TIMEOUT", 0.5)
    try:
        began = time.monotonic()
        with pytest.raises(ApiError) as info:
            api_client.wait_ready(Endpoint(path, run / "instance.lock"), interval=0.05,
                                  limit=10.0)
        elapsed = time.monotonic() - began
    finally:
        for sock in fillers + [server]:
            sock.close()
    assert info.value.reason == "timeout"
    assert not info.value.token_sent
    # kernel で期限まで待ってから失敗する (即時の EAGAIN でも無期限の待ちでもない)
    assert 0.4 <= elapsed < 3


def test_sendall_to_a_peer_that_never_reads_times_out(tmp_path):
    from afx_ops import server_auth
    run = _socket_dir(tmp_path)
    path = run / "afx.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    sock = server_auth.connect_unix(path, timeout=0.3)
    conn, _ = server.accept()
    try:
        began = time.monotonic()
        with pytest.raises(TimeoutError):
            sock.sendall(b"x" * (64 * 1024 * 1024))
        assert time.monotonic() - began < 3
    finally:
        for s in (sock, conn, server):
            s.close()


def test_each_readiness_connection_is_cut_at_the_time_left(monkeypatch):
    clock = [0.0]
    cuts = []

    def refused(endpoint, *, timeout=api_client.HTTP_TIMEOUT):
        cuts.append(timeout)
        raise ApiError("no_socket")

    monkeypatch.setattr(api_client._Request, "open", staticmethod(refused))
    with pytest.raises(ApiError) as info:
        api_client.wait_ready(None, limit=2.0, interval=0.25,
                              sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                              now=lambda: clock[0])
    assert info.value.reason == "readiness_timeout"
    assert cuts and cuts[0] == 2.0
    assert all(cut <= 2.0 for cut in cuts)


def test_a_missing_socket_file_is_classified_as_no_socket():
    import errno
    assert api_client._connect_failure(OSError(errno.ENOENT, "missing")) == "no_socket"
