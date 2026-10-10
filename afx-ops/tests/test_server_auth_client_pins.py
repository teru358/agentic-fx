"""server 認証と actor header の判定のうち、前後の検査に隠れて単体では観測されにくいものを固定する。"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import socket
import subprocess
import sys

import pytest

from afx_ops import server_auth
from afx_ops.api_client import ApiError, _encode
from afx_ops.server_auth import ServerAuthError


def test_verify_peer_refuses_a_peer_of_another_uid(tmp_path):
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with pytest.raises(ServerAuthError) as info:
            server_auth.verify_peer(a, uid=os.getuid() + 1, lock_path=tmp_path / "lock")
        assert info.value.reason == "peer_uid"
    finally:
        a.close()
        b.close()


def test_lock_holders_without_dev_fallback_does_not_count_a_dev_mismatch(tmp_path):
    lock = tmp_path / "instance.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        ino = os.stat(lock).st_ino
        locks = tmp_path / "locks"
        locks.write_text(f"1: FLOCK  ADVISORY  WRITE {os.getpid()} ff:ff:{ino} 0 EOF\n")
        # 共有契約は dev 食い違いを「その pid が lock file を開いている」で救済する
        assert server_auth.lock_holders(lock, proc_locks=locks) == {os.getpid()}
        assert server_auth.lock_holders(lock, proc_locks=locks, dev_fallback=False) == set()
    finally:
        os.close(fd)


def test_check_pidfd_refuses_a_pidfd_of_another_process():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    libc = ctypes.CDLL(None, use_errno=True)
    pidfd = libc.syscall(ctypes.c_long(434), ctypes.c_int(child.pid), ctypes.c_uint(0))  # pidfd_open
    assert pidfd >= 0, ctypes.get_errno()
    try:
        with pytest.raises(ServerAuthError) as info:
            server_auth.check_pidfd(a, pidfd)
        assert info.value.reason == "peer_gone"
    finally:
        os.close(pidfd)
        child.kill()
        child.wait()
        a.close()
        b.close()


class _NoPidfdSocket:
    def __init__(self, err: int) -> None:
        self._err = err

    def getsockopt(self, *args):
        raise OSError(self._err, os.strerror(self._err))


@pytest.mark.parametrize(("err", "reason"), [(errno.ENOPROTOOPT, "ops_requires_so_peerpidfd"),
                                             (errno.EINVAL, "ops_requires_so_peerpidfd"),
                                             (errno.ESRCH, "peer_gone"),
                                             (errno.ENODATA, "peer_gone"),
                                             (errno.EMFILE, "pidfd_transient"),
                                             (errno.ENFILE, "pidfd_transient"),
                                             (errno.ENOBUFS, "pidfd_transient"),
                                             (errno.ENOMEM, "pidfd_transient")])
def test_fetch_pidfd_has_no_fallback_when_so_peerpidfd_fails(err, reason):
    with pytest.raises(ServerAuthError) as info:
        server_auth.fetch_pidfd(_NoPidfdSocket(err))
    assert info.value.reason == reason


class _LooksLikeActor:
    value = "discord:" + "1" * 17


def test_encoder_refuses_an_object_that_only_looks_like_an_actor():
    with pytest.raises(ApiError) as info:
        _encode("GET", "/v1/status", "t", None, _LooksLikeActor())
    assert info.value.reason == "invalid_actor"


@pytest.mark.parametrize(("rows", "reason"), [
    (["own"], None),
    (["own", "other_dev"], None),
    (["other_dev", "own"], None),
    (["other_dev"], "instance_lock_dev_mismatch"),
    ([], None),
])
def test_check_lock_device_identifies_the_lock_by_device_and_inode(tmp_path, rows, reason):
    lock = tmp_path / "instance.lock"
    lock.write_text("")
    st = os.stat(lock)
    own = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    text = {"own": own, "other_dev": f"fe:fe:{st.st_ino}"}
    locks = tmp_path / "locks"
    locks.write_text("".join(f"{i + 1}: FLOCK  ADVISORY  WRITE {100 + i} {text[r]} 0 EOF\n"
                             for i, r in enumerate(rows)))
    if reason is None:
        assert server_auth.check_lock_device(lock, proc_locks=locks) is None
    else:
        with pytest.raises(ServerAuthError) as info:
            server_auth.check_lock_device(lock, proc_locks=locks)
        assert info.value.reason == reason
