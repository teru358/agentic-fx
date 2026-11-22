"""/proc/locks は 1 回の read で 1 page ぶんしか返さず、行が多く入れ替わる最中は読む度に
行が抜けることがある。保持者の行が一度抜けたことを「保持者がいない」とみなさない。"""
from __future__ import annotations

import os
import socket

import pytest

from afx_ops import server_auth
from afx_ops.server_auth import ServerAuthError


class _SequencedLocks:
    """open の度に次の内容の file を指す path (読む度に中身が変わる /proc/locks の代役)。"""

    def __init__(self, tmp_path, contents):
        self.paths = []
        for index, text in enumerate(contents):
            path = tmp_path / f"locks{index}"
            if text is not None:
                path.write_text(text)
            self.paths.append(path)
        self.opened = 0

    def __fspath__(self):
        path = self.paths[min(self.opened, len(self.paths) - 1)]
        self.opened += 1
        return str(path)


def _row(pid, device):
    return f"1: FLOCK  ADVISORY  WRITE {pid} {device} 0 EOF\n"


@pytest.fixture
def lock(tmp_path):
    path = tmp_path / "instance.lock"
    path.touch()
    st = os.stat(path)
    device = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    return path, device


def test_lock_holders_retries_when_the_holder_row_is_missing_from_a_read(tmp_path, lock):
    path, device = lock
    locks = _SequencedLocks(tmp_path, ["", "", _row(4242, device)])
    assert server_auth.lock_holders(path, proc_locks=locks) == {4242}


def test_lock_holders_gives_up_after_a_bounded_number_of_reads(tmp_path, lock):
    path, _device = lock
    locks = _SequencedLocks(tmp_path, [""])
    assert server_auth.lock_holders(path, proc_locks=locks) == set()
    assert locks.opened == server_auth.LOCK_READ_ATTEMPTS


def test_lock_holders_does_not_reread_once_a_holder_is_seen(tmp_path, lock):
    path, device = lock
    locks = _SequencedLocks(tmp_path, [_row(4242, device)])
    assert server_auth.lock_holders(path, proc_locks=locks) == {4242}
    assert locks.opened == 1


def test_check_lock_device_retries_when_only_a_foreign_device_row_is_seen(tmp_path, lock):
    path, device = lock
    inode = device.rsplit(":", 1)[1]
    foreign = f"1: FLOCK  ADVISORY  WRITE 99 ff:ff:{inode} 0 EOF\n"
    locks = _SequencedLocks(tmp_path, [foreign, foreign, _row(4242, device)])
    assert server_auth.check_lock_device(path, proc_locks=locks) is None


def test_check_lock_device_still_refuses_a_stable_device_mismatch(tmp_path, lock):
    path, device = lock
    inode = device.rsplit(":", 1)[1]
    foreign = f"1: FLOCK  ADVISORY  WRITE 99 ff:ff:{inode} 0 EOF\n"
    locks = _SequencedLocks(tmp_path, [foreign])
    with pytest.raises(ServerAuthError) as info:
        server_auth.check_lock_device(path, proc_locks=locks)
    assert info.value.reason == "instance_lock_dev_mismatch"


def test_rereads_are_spaced_by_a_short_sleep(tmp_path, lock, monkeypatch):
    path, _device = lock
    naps = []
    monkeypatch.setattr(server_auth.time, "sleep", naps.append)
    server_auth.lock_holders(path, proc_locks=_SequencedLocks(tmp_path, [""]))
    assert naps == [server_auth.LOCK_REREAD_INTERVAL] * (server_auth.LOCK_READ_ATTEMPTS - 1)
    assert 0 < server_auth.LOCK_REREAD_INTERVAL <= 0.005


def _verify(tmp_path, path, contents):
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        return server_auth.verify_peer(left, uid=os.getuid(), lock_path=path,
                                       proc_locks=_SequencedLocks(tmp_path, contents))
    finally:
        left.close()
        right.close()


def test_verify_peer_refuses_when_the_reads_show_another_holder_after_empty(tmp_path, lock):
    path, device = lock
    with pytest.raises(ServerAuthError) as info:
        _verify(tmp_path, path, ["", _row(os.getpid() + 1, device)])
    assert info.value.reason == "not_lock_holder"


def test_verify_peer_refuses_when_the_peer_and_another_holder_both_appear(tmp_path, lock):
    path, device = lock
    both = _row(os.getpid(), device) + _row(os.getpid() + 1, device)
    with pytest.raises(ServerAuthError) as info:
        _verify(tmp_path, path, ["", both])
    assert info.value.reason == "not_lock_holder"


def test_verify_peer_refuses_when_every_read_is_empty(tmp_path, lock):
    path, _device = lock
    with pytest.raises(ServerAuthError) as info:
        _verify(tmp_path, path, [""])
    assert info.value.reason == "not_lock_holder"


def test_verify_peer_refuses_when_a_later_read_fails(tmp_path, lock):
    path, _device = lock
    with pytest.raises(ServerAuthError) as info:
        _verify(tmp_path, path, ["", None])
    assert info.value.reason == "not_lock_holder"


def test_verify_peer_accepts_the_peer_seen_after_an_empty_read(tmp_path, lock):
    path, device = lock
    assert _verify(tmp_path, path, ["", _row(os.getpid(), device)]) == os.getpid()
