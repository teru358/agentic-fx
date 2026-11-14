"""event loop と終了処理の watchdog: loop が 10 分応答しない、又は終了が 60 秒で終わらないとき、
loop の外の daemon thread から process を強制終了する。

loop 上の処理に頼らない (loop が止まっていても、blocking な stderr 書込みや thread の join で
終了が詰まっていても動く)。終了は `os._exit` で、SIGKILL と同じ断面になる (state は一意 temp への
fsync と atomic replace で守られている)。終了の直前に固定の ASCII 一行を stderr へ出すが、
書込みで止まらないよう非 blocking にし、書けなければ省く。
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import select
import threading
import time
from collections.abc import Callable

from afx_ops.bot.exits import RC_BOT_LOOP_STALLED, RC_BOT_PERMANENT, RC_BOT_TRANSIENT

# acknowledgement の callback を積む周期と、loop の応答の窓、終了開始からの上限 (秒)
CHECK_INTERVAL = 5.0
STALL_WINDOW = 600.0
SHUTDOWN_LIMIT = 60.0

STALL_LINE = b"afx-ops: loop_stalled\n"
SHUTDOWN_LINE = b"afx-ops: shutdown_timeout\n"


def emit_line(line: bytes, *, fd: int = 2,
              poll: Callable[[int], bool] | None = None) -> None:
    """`line` (PIPE_BUF 未満の固定 ASCII) を fd へ、止まらずに書く。書けなければ捨てる。

    既存の fd flags を保ったまま O_NONBLOCK を足し、POLLOUT を確かめてから 1 回だけ書く。
    fcntl・poll・write のどれが失敗しても例外は出さない。
    """
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    except OSError:
        return
    try:
        if poll is None:
            poller = select.poll()
            poller.register(fd, select.POLLOUT)
            ready = any(events & select.POLLOUT for _fd, events in poller.poll(0))
        else:
            ready = poll(fd)
        if ready:
            os.write(fd, line)
    except (OSError, ValueError):
        pass


class Watchdog:
    """loop の応答 (acknowledgement) と終了開始からの経過を見て、必要なら process を終わらせる。

    thread は `start` で 1 つだけ起動し、終了開始後も止めない。`tick` は 1 回分の確認で、試験は
    thread を使わず直接呼べる。
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 interval: float = CHECK_INTERVAL, window: float = STALL_WINDOW,
                 shutdown_limit: float = SHUTDOWN_LIMIT,
                 exit_fn: Callable[[int], object] = os._exit, fd: int = 2,
                 poll: Callable[[int], bool] | None = None) -> None:
        self._clock = clock
        self._interval = interval
        self._window = window
        self._shutdown_limit = shutdown_limit
        self._exit = exit_fn
        self._fd = fd
        self._poll = poll
        self._loop: asyncio.AbstractEventLoop | None = None
        self._acknowledged = clock()
        self._shutdown_at: float | None = None
        self._shutdown_permanent = False
        self._halt = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 状態

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        """監視する event loop を渡す。応答の起点はここから数え直す。"""
        self._loop = loop
        self._acknowledged = self._clock()

    def begin_shutdown(self, *, permanent: bool = False) -> None:
        """終了の開始。最初の 1 回の時刻と分類だけを保持し、以後は動かさない。

        `permanent` は終了を始めた理由が恒久 (再起動しても同じ) のとき真。終了が期限内に終わらず
        強制終了するときの終了コードを、恒久なら 78、それ以外は 79 にする。
        """
        if self._shutdown_at is None:
            self._shutdown_at = self._clock()
            self._shutdown_permanent = permanent
            # 周期の待ちの途中でも起こし、残り時間を期限として待ち直させる
            self._wake.set()

    def next_wait(self) -> float:
        """次の確認までの秒数。終了開始後は、60 秒の期限までの残りを超えない。"""
        if self._shutdown_at is None:
            return self._interval
        remaining = self._shutdown_limit - (self._clock() - self._shutdown_at)
        return max(0.0, min(self._interval, remaining))

    @property
    def acknowledged_at(self) -> float:
        """loop が最後に応答した時刻 (この watchdog の clock、既定は monotonic)。"""
        return self._acknowledged

    def _acknowledge(self) -> None:
        self._acknowledged = self._clock()

    # ---------------------------------------------------------------- 判定

    def tick(self) -> bool:
        """1 回分の確認。process を終わらせたら True。"""
        now = self._clock()
        if self._shutdown_at is not None:
            if now - self._shutdown_at >= self._shutdown_limit:
                self._terminate(SHUTDOWN_LINE,
                                RC_BOT_PERMANENT if self._shutdown_permanent else RC_BOT_TRANSIENT)
                return True
        elif now - self._acknowledged >= self._window:
            self._terminate(STALL_LINE, RC_BOT_LOOP_STALLED)
            return True
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._acknowledge)
            except RuntimeError:
                pass  # loop は閉じた。終了後の段として監視は続ける
        return False

    def _terminate(self, line: bytes, rc: int) -> None:
        try:
            emit_line(line, fd=self._fd, poll=self._poll)
        finally:
            self._exit(rc)

    # ---------------------------------------------------------------- thread

    def start(self) -> None:
        """daemon thread を 1 つ起動する (隔離後、単一 thread の検査の後)。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="afx-ops-watchdog", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._halt.is_set():
            self._wake.wait(self.next_wait())
            self._wake.clear()
            if self._halt.is_set():
                return
            if self.tick():
                return

    def stop(self) -> None:
        """thread を止める (試験の後始末用。本番は process の終了まで止めない)。"""
        self._halt.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(5)
