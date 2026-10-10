"""web の listener: 隔離前の pre-bind と、隔離後の自前 accept loop。

listener は隔離前に `127.0.0.1` へ bind + listen し、この時点では accept しない
(接続は backlog に溜まる)。隔離後は bind / listen を seccomp が拒むので、aiohttp の
`create_server` / `SockSite` (listener に listen を再実行する) は使えない。自己検査の
成功後に `AcceptLoop` が trace で固定した accept syscall (`accept4`) で受け、
accepted socket を callback に渡す (呼出し元が `loop.connect_accepted_socket` へ渡す)。
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import socket
import sys
from collections.abc import Callable

from afx_ops.isolation.profiles import WEB_ACCEPT_SYSCALL

LOOPBACK = "127.0.0.1"
ACCEPT_SYSCALL = WEB_ACCEPT_SYSCALL
# fd が尽きて accept できない間の再試行間隔。listener は読取り可能のままなので、待たずに
# 呼び直すと loop が空回りする
ACCEPT_BACKOFF = 0.05
# 連続した fd 枯渇の上限 (約 10 秒)。枯渇が続く process は新しい接続を受けられないまま
# 動き続けるので、listener を閉じて非零終了し、systemd の再起動で fd を取り戻す
ACCEPT_EXHAUSTED_LIMIT = 200
RC_ACCEPT_EXHAUSTED = 72
_FD_EXHAUSTED = (errno.EMFILE, errno.ENFILE)


class ListenerError(Exception):
    """pre-bind した listener が想定と違う。`reason` は固定語。"""

    def __init__(self, detail: str) -> None:
        super().__init__(f"listener_invalid: {detail}")
        self.reason = "listener_invalid"


def prebind_listener(port: int, *, backlog: int = 64) -> socket.socket:
    """`127.0.0.1:<port>` に bind + listen した close-on-exec の TCP listener を返す。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
    try:
        # systemd の再起動で、前の process の接続が TIME_WAIT に残っていても bind できるように
        # する。Linux では listen 中の socket がある port は SO_REUSEADDR でも取れない
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LOOPBACK, port))
        sock.listen(backlog)
        check_listener(sock, port)
    except BaseException:
        sock.close()
        raise
    return sock


def check_listener(sock: socket.socket, port: int) -> None:
    """listener が loopback の想定 port で listen 中の stream socket であることを確かめる。

    port 0 は「任意の一時 port」で、実際の port は問わない。
    """
    if sock.family != socket.AF_INET:
        raise ListenerError("family")
    if sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
        raise ListenerError("type")
    if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1:
        raise ListenerError("not_listening")
    host, actual = sock.getsockname()
    if host != LOOPBACK or (port and actual != port):
        raise ListenerError("address")
    if not fcntl.fcntl(sock.fileno(), fcntl.F_GETFD) & fcntl.FD_CLOEXEC:
        raise ListenerError("cloexec")


class AcceptLoop:
    """listener が読めるようになるたびに、溜まった接続を全部受けて callback に渡す。

    fd が尽きた (EMFILE / ENFILE) ときは reader を外して `ACCEPT_BACKOFF` 後に付け直す。
    枯渇が `exhausted_limit` 回続いたら (間に 1 本でも受けられれば数え直す) listener を
    閉じ、固定語 `accept_exhausted` を出して `on_exhausted` を呼ぶ (既定は process の
    非零終了)。callback が例外を出した接続は閉じて次の接続へ進む (1 本の失敗で fd を
    漏らさない)。どちらも回数を数え、stderr に固定語を出す (fd 枯渇は続く間は最初の 1 回だけ)。
    """

    def __init__(self, listener: socket.socket,
                 on_accept: Callable[[socket.socket], None], *,
                 exhausted_limit: int = ACCEPT_EXHAUSTED_LIMIT,
                 on_exhausted: Callable[[], None] | None = None) -> None:
        self._listener = listener
        self._on_accept = on_accept
        self._loop: asyncio.AbstractEventLoop | None = None
        self._resume: asyncio.TimerHandle | None = None
        self._exhausted = False
        self._consecutive = 0
        self._limit = exhausted_limit
        self._on_exhausted = on_exhausted if on_exhausted is not None else _exit_exhausted
        self.resource_errors = 0
        self.callback_errors = 0
        listener.setblocking(False)

    def accept_ready(self) -> int:
        """今受けられる接続を全部受ける。受けた数を返す。"""
        count = 0
        while True:
            try:
                # CPython の socket.accept は accept4(…, SOCK_CLOEXEC) を呼ぶ
                conn, _addr = self._listener.accept()
            except (BlockingIOError, InterruptedError):
                return count
            except ConnectionAbortedError:
                continue
            except OSError as exc:
                if exc.errno not in _FD_EXHAUSTED:
                    raise
                self._back_off(exc.errno)
                return count
            self._exhausted = False
            self._consecutive = 0
            count += 1
            try:
                self._on_accept(conn)
            except Exception as exc:  # 1 本の失敗で loop を止めず、受けた fd も残さない
                self.callback_errors += 1
                conn.close()
                _log(f"accept_callback_failed {type(exc).__name__}")

    def _back_off(self, err: int) -> None:
        self.resource_errors += 1
        self._consecutive += 1
        if self._consecutive >= self._limit:
            self._give_up()
            return
        if not self._exhausted:
            self._exhausted = True
            _log(f"accept_backoff {errno.errorcode.get(err, err)}")
        loop = self._loop
        if loop is None or self._resume is not None:
            return
        loop.remove_reader(self._listener.fileno())
        self._resume = loop.call_later(ACCEPT_BACKOFF, self._reattach)

    def _give_up(self) -> None:
        loop = self._loop
        if loop is not None:
            self.detach(loop)
        self._listener.close()
        _log("accept_exhausted")
        self._on_exhausted()

    def _reattach(self) -> None:
        self._resume = None
        if self._loop is not None:
            self._loop.add_reader(self._listener.fileno(), self.accept_ready)

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        loop.add_reader(self._listener.fileno(), self.accept_ready)

    def detach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = None
        if self._resume is not None:
            self._resume.cancel()
            self._resume = None
        loop.remove_reader(self._listener.fileno())


def _exit_exhausted() -> None:
    # reader の callback の中から投げても SystemExit は event loop を抜けて process を終える
    raise SystemExit(RC_ACCEPT_EXHAUSTED)


def _log(text: str) -> None:
    print(f"afx-ops: {text}", file=sys.stderr, flush=True)
