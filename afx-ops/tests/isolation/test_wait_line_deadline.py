"""Interactive.wait_line は出力が来ないとき期限で赤になる (readline で固まらない)。"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from _afx_isolation_support import Interactive


def _silent() -> Interactive:
    held = object.__new__(Interactive)
    held.proc = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
    held.lines = []
    return held


def test_wait_line_fails_with_timeout_when_nothing_is_printed():
    held = _silent()
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            held.wait_line("SERVING", timeout=0.5)
        assert time.monotonic() - started < 10
    finally:
        held.kill()


def test_wait_line_still_returns_a_line_printed_before_the_deadline():
    held = object.__new__(Interactive)
    held.proc = subprocess.Popen([sys.executable, "-I", "-c",
                                  "print('A', flush=True); print('SERVING', flush=True);"
                                  "import time; time.sleep(60)"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
    held.lines = []
    try:
        assert held.wait_line("SERVING", timeout=20) == "SERVING"
        assert held.lines == ["A", "SERVING"]
    finally:
        held.kill()


def test_wait_line_deadline_also_stops_descendants_holding_the_pipe():
    import os
    import threading

    # 直接の子 (exec 後の sleep) と、stdout を引き継いだ背景の sleep の 2 つが居る
    held = Interactive({}, dict(os.environ), prefix=("sh", "-c", "sleep 300 & exec sleep 300"))
    outcome: list[BaseException] = []

    def wait() -> None:
        try:
            held.wait_line("SERVING", timeout=0.5)
        except BaseException as exc:  # noqa: BLE001 - 結果の型を見る
            outcome.append(exc)

    waiter = threading.Thread(target=wait, daemon=True)
    started = time.monotonic()
    waiter.start()
    waiter.join(10)
    stuck = waiter.is_alive()
    try:
        assert not stuck, "wait_line did not return: a descendant holds the pipe"
        assert time.monotonic() - started < 10
        assert len(outcome) == 1 and isinstance(outcome[0], TimeoutError)
    finally:
        held.kill()


def test_wait_line_deadline_returns_when_the_parent_is_gone_and_only_a_descendant_holds_the_pipe():
    import os
    import threading

    # 親は即 exit して回収され、stdout を握る背景の sleep だけが残る
    held = Interactive({}, dict(os.environ), prefix=("sh", "-c", "sleep 300 & exit 0"))
    held.proc.wait()
    outcome: list[BaseException] = []

    def wait() -> None:
        try:
            held.wait_line("SERVING", timeout=0.5)
        except BaseException as exc:  # noqa: BLE001 - 結果の型を見る
            outcome.append(exc)

    waiter = threading.Thread(target=wait, daemon=True)
    started = time.monotonic()
    waiter.start()
    waiter.join(10)
    stuck = waiter.is_alive()
    try:
        assert not stuck, "wait_line did not return: a descendant holds the pipe"
        assert time.monotonic() - started < 10
        assert len(outcome) == 1 and isinstance(outcome[0], TimeoutError)
    finally:
        held.kill()
