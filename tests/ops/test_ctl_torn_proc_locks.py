"""/proc/locks は読む度に行が抜けることがある。保持者の行が一度抜けても保持者なしとしない。"""
from __future__ import annotations

import os
import socket

import pytest

from agentic_fx.ops import client


class _SequencedLocks:
    """read_text の度に次の内容を返す /proc/locks の代役。"""

    def __init__(self, contents):
        self.contents = contents
        self.reads = 0

    def read_text(self, **_kwargs):
        text = self.contents[min(self.reads, len(self.contents) - 1)]
        self.reads += 1
        if text is None:
            raise OSError("read failed")
        return text


def _lock(tmp_path):
    path = tmp_path / "instance.lock"
    path.touch()
    st = os.stat(path)
    return path, f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"


def test_lock_holders_retries_when_the_holder_row_is_missing_from_a_read(tmp_path):
    path, device = _lock(tmp_path)
    row = f"1: FLOCK  ADVISORY  WRITE 4242 {device} 0 EOF\n"
    locks = _SequencedLocks(["", "", row])
    assert client.lock_holders(path, proc_locks=locks) == {4242}


def test_lock_holders_gives_up_after_a_bounded_number_of_reads(tmp_path):
    path, _device = _lock(tmp_path)
    locks = _SequencedLocks([""])
    assert client.lock_holders(path, proc_locks=locks) == set()
    assert locks.reads == client.LOCK_READ_ATTEMPTS


def test_lock_holders_does_not_reread_once_a_holder_is_seen(tmp_path):
    path, device = _lock(tmp_path)
    locks = _SequencedLocks([f"1: FLOCK  ADVISORY  WRITE 4242 {device} 0 EOF\n"])
    assert client.lock_holders(path, proc_locks=locks) == {4242}
    assert locks.reads == 1


def test_rereads_are_spaced_by_a_short_sleep(tmp_path, monkeypatch):
    path, _device = _lock(tmp_path)
    naps = []
    monkeypatch.setattr(client.time, "sleep", naps.append)
    client.lock_holders(path, proc_locks=_SequencedLocks([""]))
    assert naps == [client.LOCK_REREAD_INTERVAL] * (client.LOCK_READ_ATTEMPTS - 1)
    assert 0 < client.LOCK_REREAD_INTERVAL <= 0.005


def _verify(tmp_path, contents):
    path, _device = _lock(tmp_path)
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        return client.verify_peer(left, uid=os.getuid(), lock_path=path,
                                  proc_locks=_SequencedLocks(contents))
    finally:
        left.close()
        right.close()


def _rows(tmp_path, *pids):
    _path, device = _lock(tmp_path)
    return "".join(f"1: FLOCK  ADVISORY  WRITE {pid} {device} 0 EOF\n" for pid in pids)


def test_verify_peer_refuses_when_the_reads_show_another_holder_after_empty(tmp_path):
    other = _rows(tmp_path, os.getpid() + 1)
    with pytest.raises(client.ServerAuthError) as info:
        _verify(tmp_path, ["", other])
    assert info.value.reason == "not_lock_holder"


def test_verify_peer_refuses_when_the_peer_and_another_holder_both_appear(tmp_path):
    both = _rows(tmp_path, os.getpid(), os.getpid() + 1)
    with pytest.raises(client.ServerAuthError) as info:
        _verify(tmp_path, ["", both])
    assert info.value.reason == "not_lock_holder"


def test_verify_peer_refuses_when_every_read_is_empty(tmp_path):
    with pytest.raises(client.ServerAuthError) as info:
        _verify(tmp_path, [""])
    assert info.value.reason == "not_lock_holder"


def test_verify_peer_refuses_when_a_later_read_fails(tmp_path):
    with pytest.raises(client.ServerAuthError) as info:
        _verify(tmp_path, ["", None])
    assert info.value.reason == "not_lock_holder"


def test_verify_peer_accepts_the_peer_seen_after_an_empty_read(tmp_path):
    assert _verify(tmp_path, ["", _rows(tmp_path, os.getpid())]) == os.getpid()
