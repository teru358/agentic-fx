"""CONNECT tunnel の状態と、client / upstream 間の双方向中継。

一つの tunnel は一つの thread が poll で両方向を回す。片側の EOF は相手側への
shutdown(SHUT_WR) で伝え、逆方向は流し続ける (half-close)。RST・送信失敗・想定外の
例外は fault として両側を閉じる。停止は stop_fd (eventfd) が読めることで知らせる。
"""

from __future__ import annotations

import enum
import select
import socket
import threading
import time
from collections.abc import Callable

IDLE_TIMEOUT_SEC = 300.0  # 両方向とも無通信の上限
RECV_BYTES = 64 * 1024


class TunnelState(enum.Enum):
    CONNECTING = "connecting"
    OPEN = "open"
    HALF_CLOSED = "half_closed"
    CLOSED = "closed"


class RelayEnd(enum.Enum):
    CLOSED = "closed"  # 両方向が EOF で閉じた
    IDLE = "idle"
    FAULT = "fault"
    STOPPED = "stopped"


class Tunnel:
    """accept から CLOSED までの 1 接続。状態の遷移は記録して読めるようにする。"""

    def __init__(self, client: socket.socket) -> None:
        self.client = client
        self.upstream: socket.socket | None = None
        self.end: RelayEnd | None = None
        self._lock = threading.Lock()
        self._state = TunnelState.CONNECTING
        self._transitions: list[TunnelState] = [TunnelState.CONNECTING]
        self._sockets_shut = False

    @property
    def state(self) -> TunnelState:
        with self._lock:
            return self._state

    @property
    def transitions(self) -> tuple[TunnelState, ...]:
        with self._lock:
            return tuple(self._transitions)

    def set_state(self, state: TunnelState) -> None:
        with self._lock:
            if self._state is TunnelState.CLOSED or self._state is state:
                return
            self._state = state
            self._transitions.append(state)

    def attach_upstream(self, upstream: socket.socket) -> bool:
        """停止の shutdown が既に走っていれば False (呼び出し側が upstream を閉じる)。"""
        with self._lock:
            if self._sockets_shut:
                return False
            self.upstream = upstream
            return True

    def shutdown_sockets(self) -> None:
        """別 thread から、blocking 中の recv / send を起こすために両側を shutdown する。

        close はしない。fd を閉じるのは tunnel を回す thread だけにして、番号の再利用で
        無関係な fd を触る競合を作らない。
        """
        with self._lock:
            self._sockets_shut = True
            socks = [self.client, self.upstream]
        for s in socks:
            if s is None:
                continue
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            socks = [self.client, self.upstream]
        for s in socks:
            if s is None:
                continue
            try:
                s.close()
            except OSError:
                pass
        self.set_state(TunnelState.CLOSED)


class _Direction:
    def __init__(self, src: socket.socket, dst: socket.socket, initial: bytes = b"") -> None:
        self.src = src
        self.dst = dst
        self.buf = initial
        self.eof = False  # src から EOF を読んだ
        self.done = False  # EOF を dst へ shutdown(SHUT_WR) で伝え終えた


def relay(
    tunnel: Tunnel,
    *,
    preread: bytes = b"",
    idle_timeout: float = IDLE_TIMEOUT_SEC,
    stop_fd: int | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RelayEnd:
    """OPEN の tunnel を中継し、終わり方を返す。socket の close は呼び出し側が行う。

    preread (要求と同じ recv で届いた bytes) は client から読む最初の bytes として
    upstream へ送り、それを送り終えるまで client からは読まない (順序を崩さない)。
    """
    client, upstream = tunnel.client, tunnel.upstream
    assert upstream is not None
    end = RelayEnd.FAULT
    try:
        client.setblocking(False)
        upstream.setblocking(False)
        c2u = _Direction(client, upstream, preread)
        u2c = _Direction(upstream, client)
        dirs = (c2u, u2c)
        last_activity = clock()
        while True:
            if c2u.done and u2c.done:
                end = RelayEnd.CLOSED
                break
            remaining = last_activity + idle_timeout - clock()
            if remaining <= 0:
                end = RelayEnd.IDLE
                break
            masks: dict[int, int] = {}
            for d in dirs:
                if d.buf:
                    masks[d.dst.fileno()] = masks.get(d.dst.fileno(), 0) | select.POLLOUT
                elif not d.eof:
                    masks[d.src.fileno()] = masks.get(d.src.fileno(), 0) | select.POLLIN
            poller = select.poll()
            for fd, mask in masks.items():
                poller.register(fd, mask)
            if stop_fd is not None:
                poller.register(stop_fd, select.POLLIN)
            events = dict(poller.poll(max(1, int(remaining * 1000 + 0.999))))
            if stop_fd is not None and stop_fd in events:
                end = RelayEnd.STOPPED
                break
            if any(ev & (select.POLLERR | select.POLLNVAL) for ev in events.values()):
                break  # fault
            moved = False
            for d in dirs:
                if d.buf and events.get(d.dst.fileno(), 0) & (select.POLLOUT | select.POLLHUP):
                    n = d.dst.send(d.buf)
                    d.buf = d.buf[n:]
                    moved = moved or n > 0
                elif not d.buf and not d.eof and events.get(d.src.fileno(), 0) & (
                    select.POLLIN | select.POLLHUP
                ):
                    try:
                        data = d.src.recv(RECV_BYTES)
                    except BlockingIOError:
                        continue
                    if data:
                        d.buf = data
                        moved = True
                    else:
                        d.eof = True
                if d.eof and not d.buf and not d.done:
                    d.dst.shutdown(socket.SHUT_WR)
                    d.done = True
                    tunnel.set_state(TunnelState.HALF_CLOSED)
            if moved:
                last_activity = clock()
    except Exception:  # noqa: BLE001 - RST・送信失敗・想定外の例外はすべて fault として両側を閉じる
        end = RelayEnd.FAULT
    tunnel.end = end
    return end
