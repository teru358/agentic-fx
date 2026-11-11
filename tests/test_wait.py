from __future__ import annotations

import os
import socket

import pytest

from tests._wait import wait_readable, wait_writable


@pytest.fixture
def high_fds():
    held: list[int] = []
    try:
        while True:
            fd = os.dup(0)
            held.append(fd)
            if fd >= 1100:
                break
        yield
    finally:
        for fd in held:
            os.close(fd)


def test_wait_readable_and_writable_work_above_fd_setsize(high_fds):
    r, w = os.pipe()
    try:
        assert r >= 1024 and w >= 1024
        assert wait_readable(r, 0) is False
        assert wait_writable(w, 0) is True
        os.write(w, b"x")
        assert wait_readable(r, 0) is True
        os.read(r, 1)
        os.close(w)
        assert wait_readable(r, 0) is True  # EOF は読める扱い
    finally:
        os.close(r)


def test_wait_accepts_file_objects_and_times_out():
    a, b = socket.socketpair()
    with a, b:
        assert wait_readable(a, 0.05) is False
        b.send(b"x")
        assert wait_readable(a, 1.0) is True
