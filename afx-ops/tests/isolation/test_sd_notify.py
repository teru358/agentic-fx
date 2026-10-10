"""sd_notify: 隔離前に NOTIFY_SOCKET へ connect し、自己検査の成功後にだけ READY=1 を送る。"""
from __future__ import annotations

import os
import socket

import pytest

from _afx_isolation_support import (
    FEATURES,
    Interactive,
    make_root,
    needs_landlock,
    needs_x86_64,
    run_canary,
)

pytestmark = [needs_x86_64, needs_landlock]


@pytest.fixture
def root(tmp_path):
    return make_root(tmp_path)


class _FakeManager:
    """systemd の通知受け口の代わり (実 AF_UNIX DGRAM)。"""

    def __init__(self, kind: str, tmp_path) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        if kind == "path":
            address = str(tmp_path / "n.sock")
            self.env_value = address
        else:
            address = f"\0afx-ops-notify-test-{os.getpid()}-{id(self)}"
            self.env_value = "@" + address[1:]
        self.sock.bind(address)

    def pending(self) -> list[bytes]:
        got = []
        self.sock.setblocking(False)
        while True:
            try:
                got.append(self.sock.recv(4096))
            except BlockingIOError:
                return got

    def wait(self, timeout: float = 10) -> bytes:
        self.sock.setblocking(True)
        self.sock.settimeout(timeout)
        return self.sock.recv(4096)

    def close(self) -> None:
        self.sock.close()


@pytest.fixture(params=["path", "abstract"])
def manager(request, tmp_path):
    fake = _FakeManager(request.param, tmp_path)
    yield fake
    fake.close()


def _spec(root, feature, op="noop", **extra):
    return {"feature": feature, "root": str(root), "op": op, "params": {},
            "require_notify": True, **extra}


@pytest.mark.parametrize("feature", FEATURES)
def test_ready_arrives_only_after_the_self_check(feature, root, child_env, manager):
    env = {**child_env, "NOTIFY_SOCKET": manager.env_value}
    child = Interactive(_spec(root, feature, "notify_fd_write", hold_after_check=True), env)
    try:
        child.wait_line("CHECKED")
        # 自己検査が通った時点ではまだ何も届いていない
        assert manager.pending() == []
        child.send()
        assert manager.wait() == b"READY=1"
        done = child.finish()
    finally:
        child.kill()
    assert done.rc == 0, done.stderr[-2000:]
    assert done.isolated and done.token_sent
    # READY=1 の後は通知 fd を閉じている (任意の DGRAM 宛先に使わせない)
    assert done.result == [-1, 9]
    assert manager.pending() == []


FAULTS = {
    "skip_landlock": "self_check_landlock",
    "skip_seccomp": "self_check_seccomp",
    "keep_dumpable": "self_check_dumpable",
    "extra_fd": "self_check_fds",
    "extra_thread": "self_check_threads",
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_ready_is_not_sent_when_the_self_check_fails(fault, root, child_env, manager):
    env = {**child_env, "NOTIFY_SOCKET": manager.env_value}
    got = run_canary({**_spec(root, "bot"), "fault": fault}, env)
    assert got.rc == 71
    assert got.stderr.strip().splitlines()[-1] == f"afx-ops: {FAULTS[fault]}"
    assert manager.pending() == []


@pytest.mark.parametrize("value", [None, "", "relative/notify", "/nonexistent/afx-notify"])
def test_missing_or_unusable_notify_socket_is_a_check_failure(value, root, child_env):
    env = dict(child_env)
    if value is not None:
        env["NOTIFY_SOCKET"] = value
    got = run_canary(_spec(root, "bot"), env)
    assert got.rc == 71
    assert got.stderr.strip().splitlines()[-1] == "afx-ops: unit_notify_socket"
    assert not got.isolated and not got.token_sent


def test_notify_fd_is_held_through_isolation_and_closed_after_ready(root, child_env, manager):
    env = {**child_env, "NOTIFY_SOCKET": manager.env_value}
    got = run_canary(_spec(root, "web", "open_fds"), env)
    assert got.rc == 0, got.stderr[-2000:]
    assert manager.pending() == [b"READY=1"]
    # body に残るのは 0/1/2 と listener だけ
    assert len(got.result) == 4 and got.result[:3] == [0, 1, 2]


def test_send_ready_closes_the_notify_fd_even_when_the_send_fails():
    from afx_ops.isolation import startup
    sock, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    fd = sock.fileno()
    peer.close()
    with pytest.raises(startup.StartupError) as info:
        startup.send_ready(sock)
    assert info.value.reason == "notify_failed"
    # 呼出し元が object を握ったままでも fd は閉じている
    assert sock.fileno() == -1
    with pytest.raises(OSError) as err:
        os.fstat(fd)
    assert err.value.errno == 9
