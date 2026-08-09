"""shell readline 中断 seam (プラン8, 設計書 §6 codex I3-2)。"""
from __future__ import annotations

import os
import threading
import time

import pytest

from agentic_fx.shell import run_shell


class _FakeCommands:
    def dispatch(self, line: str) -> str:
        return f"echo: {line}"


def test_stop_event_wakes_blocked_shell_promptly(tmp_path):
    """対話モードで stop_event が外部スレッドからセットされたとき、
    input() 相当のブロッキング読み取りが速やかに解ける
    (poll_interval を短く設定し、real select ベースのポーリングで
    実際に解けることを実測する)。
    """
    r_fd, w_fd = os.pipe()  # 読み取り側は「データが来ない」ダミー stdin
    stream = os.fdopen(r_fd, "r")
    stop_event = threading.Event()

    t = threading.Thread(
        target=run_shell,
        args=(_FakeCommands(), stop_event),
        kwargs={"stdin_stream": stream, "poll_interval": 0.05,
               "print_fn": lambda *_: None},
        daemon=True)
    t.start()
    time.sleep(0.1)  # run_shell がポーリングループに入るまで待つ

    start = time.monotonic()
    stop_event.set()
    t.join(timeout=2.0)
    elapsed = time.monotonic() - start

    assert not t.is_alive(), "stop_event セット後、shell スレッドが終了していない"
    assert elapsed < 1.0  # poll_interval=0.05s に対して十分な余裕

    os.close(w_fd)
    stream.close()


def test_line_is_read_when_available(tmp_path):
    """通常経路: stdin にデータが来れば読み取ってコマンドとして処理する。"""
    r_fd, w_fd = os.pipe()
    stream = os.fdopen(r_fd, "r")
    writer = os.fdopen(w_fd, "w")
    stop_event = threading.Event()
    printed: list[str] = []

    t = threading.Thread(
        target=run_shell,
        args=(_FakeCommands(), stop_event),
        kwargs={"stdin_stream": stream, "poll_interval": 0.05,
               "print_fn": printed.append},
        daemon=True)
    t.start()
    time.sleep(0.1)
    writer.write("hello\n")
    writer.flush()
    time.sleep(0.2)

    stop_event.set()
    t.join(timeout=2.0)

    assert "echo: hello" in printed
    writer.close()
    stream.close()


def test_multiple_lines_arriving_together_are_not_stuck(tmp_path):
    """裁定書 F-15 (IM-8) の回帰ピン: ペースト等で複数行が 1 回の書込みで
    同時到着した場合、2 行目以降が「次の新規入力が来るまで」滞留せず、
    追加の書込みなしに両方処理されることを確認する。"""
    r_fd, w_fd = os.pipe()
    stream = os.fdopen(r_fd, "r")
    writer = os.fdopen(w_fd, "w")
    stop_event = threading.Event()
    printed: list[str] = []

    t = threading.Thread(
        target=run_shell,
        args=(_FakeCommands(), stop_event),
        kwargs={"stdin_stream": stream, "poll_interval": 0.05,
               "print_fn": printed.append},
        daemon=True)
    t.start()
    time.sleep(0.1)
    writer.write("hello\nworld\n")  # 2 行を 1 回の書込み・flush でまとめて送る
    writer.flush()
    time.sleep(0.3)  # 追加の書込みは一切行わない — この待ちだけで両方届くはず

    stop_event.set()
    t.join(timeout=2.0)

    assert "echo: hello" in printed
    assert "echo: world" in printed  # 旧稿はここが追加入力なしでは届かなかった
    writer.close()
    stream.close()
