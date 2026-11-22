"""/proc/locks は読む度に行が抜けることがある。保持者の行が一度抜けても保持者なしとしない。"""
from __future__ import annotations

import os

from agentic_fx.ops import client


class _SequencedLocks:
    """read_text の度に次の内容を返す /proc/locks の代役。"""

    def __init__(self, contents):
        self.contents = contents
        self.reads = 0

    def read_text(self, **_kwargs):
        text = self.contents[min(self.reads, len(self.contents) - 1)]
        self.reads += 1
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
