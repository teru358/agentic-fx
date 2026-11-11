"""fd 番号が 1024 を超えた状態でも、worker_runner の子スレッド模擬が動くこと。

1 プロセスで多数の test を回すと閉じ忘れの fd で番号が伸びる。待ちが
`select.select` だと ValueError で子スレッドが死ぬので、代表の 3 test を
低い fd 番号を埋め尽くした状態で回す。
"""
from __future__ import annotations

import os

import pytest

from tests.runners import test_worker_runner as base


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
            try:
                os.close(fd)
            except OSError:
                pass


def test_go_frame_after_on_ready_with_high_fd_numbers(
        high_fds, tmp_path, monkeypatch):
    base.test_go_frame_is_written_as_real_frame_only_after_on_ready_returns(
        tmp_path, monkeypatch)


def test_go_frame_sent_when_on_ready_is_none_with_high_fd_numbers(
        high_fds, tmp_path, monkeypatch):
    base.test_go_frame_is_sent_for_improve_profile_even_when_on_ready_is_none(
        tmp_path, monkeypatch)


def test_ready_ok_false_sends_no_go_with_high_fd_numbers(
        high_fds, tmp_path, monkeypatch):
    base.test_improve_ready_ok_false_does_not_call_on_ready_or_send_go(
        tmp_path, monkeypatch)
