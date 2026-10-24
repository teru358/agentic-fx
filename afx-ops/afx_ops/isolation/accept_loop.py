"""web の listener: 隔離前の pre-bind と、隔離後の自前 accept loop。

listener は隔離前に設定の address (既定 `127.0.0.1`) へ bind + listen し、この時点では
accept しない (接続は backlog に溜まる)。隔離後は bind / listen を seccomp が拒むので、
aiohttp の `create_server` / `SockSite` (listener に listen を再実行する) は使えない。
自己検査の成功後に `AcceptLoop` が trace で固定した accept syscall (`accept4`) で受け、
accepted socket の peer IPv4 を allow の network と照合してから、許可した接続だけを
callback に渡す (呼出し元が `loop.connect_accepted_socket` へ渡す)。照合に外れた接続は
応答も要求の読取りもせず黙って閉じる。
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import ipaddress
import socket
import sys
from collections.abc import Callable, Sequence

from afx_ops.isolation.profiles import WEB_ACCEPT_SYSCALL

LOOPBACK = "127.0.0.1"
LOOPBACK_NETWORK = ipaddress.IPv4Network("127.0.0.0/8")
ACCEPT_SYSCALL = WEB_ACCEPT_SYSCALL
# fd が尽きて accept できない間の再試行間隔。listener は読取り可能のままなので、待たずに
# 呼び直すと loop が空回りする
ACCEPT_BACKOFF = 0.05
# 連続した fd 枯渇の上限 (約 10 秒)。枯渇が続く process は新しい接続を受けられないまま
# 動き続けるので、listener を閉じて非零終了し、systemd の再起動で fd を取り戻す
ACCEPT_EXHAUSTED_LIMIT = 200
RC_ACCEPT_EXHAUSTED = 72
# readiness callback 1 回で試す accept syscall の上限 (成功数でなく試行数)。残りは次の
# event-loop tick に譲り、同時に ready になった deadline callback を進められるようにする
ACCEPT_BUDGET = 64
# 同じ種類の journal を process lifetime で出す最大行数。以後は counter だけを増やす
JOURNAL_SAMPLE = 10
_FD_EXHAUSTED = (errno.EMFILE, errno.ENFILE)


class ListenerError(Exception):
    """pre-bind した listener が想定と違う。`reason` は固定語。"""

    def __init__(self, detail: str) -> None:
        super().__init__(f"listener_invalid: {detail}")
        self.reason = "listener_invalid"


def prebind_listener(port: int, *, bind: str = LOOPBACK, backlog: int = 64) -> socket.socket:
    """`<bind>:<port>` に bind + listen した close-on-exec の TCP listener を返す。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
    try:
        # systemd の再起動で、前の process の接続が TIME_WAIT に残っていても bind できるように
        # する。Linux では listen 中の socket がある port は SO_REUSEADDR でも取れない
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind, port))
        sock.listen(backlog)
        check_listener(sock, port, bind)
    except BaseException:
        sock.close()
        raise
    return sock


def check_listener(sock: socket.socket, port: int, bind: str = LOOPBACK) -> None:
    """listener が設定の address と port で listen 中の stream socket であることを確かめる。

    port 0 は「任意の一時 port」で、実際の port は問わない。
    """
    if sock.family != socket.AF_INET:
        raise ListenerError("family")
    if sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
        raise ListenerError("type")
    if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1:
        raise ListenerError("not_listening")
    host, actual = sock.getsockname()
    if host != bind or (port and actual != port):
        raise ListenerError("address")
    if not fcntl.fcntl(sock.fileno(), fcntl.F_GETFD) & fcntl.FD_CLOEXEC:
        raise ListenerError("cloexec")


def canonical_peer(raw: object) -> str | None:
    """accepted socket の `getpeername()` が IPv4 の 2 要素 tuple なら、正規化した IPv4。"""
    if type(raw) is not tuple or len(raw) != 2 or type(raw[0]) is not str or type(raw[1]) is not int:
        return None
    try:
        return str(ipaddress.IPv4Address(raw[0]))
    except ValueError:
        return None


class Journal:
    """固定語の journal 行。種類ごとに先頭 `JOURNAL_SAMPLE` 行だけを出す。"""

    def __init__(self, limit: int = JOURNAL_SAMPLE) -> None:
        self._limit = limit
        self._seen: dict[str, int] = {}

    def sampled(self, text: str) -> None:
        count = self._seen.get(text, 0)
        self._seen[text] = count + 1
        if count < self._limit:
            _log(text)

    def always(self, text: str) -> None:
        _log(text)


class AcceptLoop:
    """listener が読めるようになるたびに、溜まった接続を受けて許可した接続だけ callback に渡す。

    readiness callback 1 回で試す accept syscall は `ACCEPT_BUDGET` 回まで (残りは次の
    tick)。accept 直後に peer の IPv4 を `allow` と照合し、外れた接続 (`getpeername()` の
    失敗を含む) は何も読まず何も返さずに閉じて専用 counter だけを増やす。callback の契約は
    `(socket, canonical_peer_ipv4)`。

    fd が尽きた (EMFILE / ENFILE) ときは reader を外して `ACCEPT_BACKOFF` 後に付け直す。
    枯渇が `exhausted_limit` 回続いたら (間に accept の成功が 1 回でもあれば数え直す) listener
    を閉じ、固定語 `accept_exhausted` を出して `on_exhausted` を呼ぶ (既定は process の
    非零終了)。想定外の errno は reader を外して listener を閉じ、`accept_failed` を出して
    `on_failed` を呼ぶ。callback が例外を出した接続は閉じて次の接続へ進む (1 本の失敗で fd を
    漏らさない)。journal は固定語だけで、種類ごとに先頭の数行に抑える。
    """

    def __init__(self, listener: socket.socket,
                 on_accept: Callable[[socket.socket, str], None], *,
                 allow: Sequence[ipaddress.IPv4Network] = (LOOPBACK_NETWORK,),
                 exhausted_limit: int = ACCEPT_EXHAUSTED_LIMIT,
                 on_exhausted: Callable[[], None] | None = None,
                 on_failed: Callable[[], None] | None = None) -> None:
        self._listener = listener
        self._on_accept = on_accept
        self._allow = tuple(allow)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._resume: asyncio.TimerHandle | None = None
        self._exhausted = False
        self._consecutive = 0
        self._limit = exhausted_limit
        self._on_exhausted = on_exhausted if on_exhausted is not None else _exit_exhausted
        self._on_failed = on_failed if on_failed is not None else _exit_exhausted
        self._journal = Journal()
        self.web_peer_rejected_total = 0
        self.web_peer_lookup_failed_total = 0
        self.accept_resource_error_total = 0
        self.accept_callback_failed_total = 0
        self.accept_exhausted_total = 0
        self.accept_failed_total = 0
        listener.setblocking(False)

    @property
    def resource_errors(self) -> int:
        return self.accept_resource_error_total

    @property
    def callback_errors(self) -> int:
        return self.accept_callback_failed_total

    def accept_ready(self) -> int:
        """今受けられる接続を (試行の上限まで) 受ける。accept に成功した数を返す。"""
        count = 0
        for _attempt in range(ACCEPT_BUDGET):
            try:
                # CPython の socket.accept は accept4(…, SOCK_CLOEXEC) を呼ぶ
                conn, _addr = self._listener.accept()
            except BlockingIOError:
                return count
            except (InterruptedError, ConnectionAbortedError):
                continue
            except OSError as exc:
                if exc.errno in _FD_EXHAUSTED:
                    self._back_off()
                else:
                    self._fail()
                return count
            # accept の成功が連続回数を戻す。後続の照合や callback の成否には依らない
            self._exhausted = False
            self._consecutive = 0
            count += 1
            self._deliver(conn)
        return count

    def _deliver(self, conn: socket.socket) -> None:
        try:
            raw = conn.getpeername()
        except OSError:
            raw = None
        peer = canonical_peer(raw)
        if peer is None:
            self.web_peer_lookup_failed_total += 1
            self._journal.sampled("web_peer_lookup_failed")
            conn.close()
            return
        address = ipaddress.IPv4Address(peer)
        if not any(address in network for network in self._allow):
            self.web_peer_rejected_total += 1
            self._journal.sampled("web_peer_rejected")
            conn.close()
            return
        try:
            self._on_accept(conn, peer)
        except Exception:  # 1 本の失敗で loop を止めず、受けた fd も残さない
            self.accept_callback_failed_total += 1
            conn.close()
            self._journal.sampled("accept_callback_failed")

    def _back_off(self) -> None:
        self.accept_resource_error_total += 1
        self._consecutive += 1
        if self._consecutive >= self._limit:
            self._give_up()
            return
        if not self._exhausted:
            self._exhausted = True
            self._journal.sampled("accept_backoff")
        loop = self._loop
        if loop is None or self._resume is not None:
            return
        loop.remove_reader(self._listener.fileno())
        self._resume = loop.call_later(ACCEPT_BACKOFF, self._reattach)

    def _stop_listening(self) -> None:
        loop = self._loop
        if loop is not None:
            self.detach(loop)
        self._listener.close()

    def _give_up(self) -> None:
        self.accept_exhausted_total += 1
        self._stop_listening()
        self._journal.always("accept_exhausted")
        self._on_exhausted()

    def _fail(self) -> None:
        self.accept_failed_total += 1
        self._stop_listening()
        self._journal.always("accept_failed")
        self._on_failed()

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
