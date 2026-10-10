"""resolver が固定した address 列への、順次の upstream 接続。

host 名を受け取って解決まで行う接続 API は使わない。address 文字列は数値 literal と
して解釈できるものだけを socket(AF_INET / AF_INET6) + connect に渡し、ここで名前解決が
起きる余地を残さない (解決と接続の間で答えが入れ替わる TOCTOU を作らないため)。
"""

from __future__ import annotations

import errno
import ipaddress
import select
import socket
import time
from collections.abc import Sequence

PER_ADDRESS_TIMEOUT_SEC = 5.0  # address 一つに掛ける上限
TOTAL_TIMEOUT_SEC = 15.0  # address 列全体に掛ける上限

STATUS_BAD_GATEWAY = 502
STATUS_GATEWAY_TIMEOUT = 504


class UpstreamError(Exception):
    """全 address で接続できなかったときの失敗。status は proxy が client に返す値。"""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"{status} {reason}")
        self.status = status
        self.reason = reason


class UpstreamStopped(Exception):
    """接続待ちの間に停止が要求された。"""


def _family_of(addr: str) -> int | None:
    if "%" in addr:
        return None
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None
    return socket.AF_INET if ip.version == 4 else socket.AF_INET6


def connect_fixed(
    targets: Sequence[tuple[str, int]],
    *,
    per_address_timeout: float = PER_ADDRESS_TIMEOUT_SEC,
    total_timeout: float = TOTAL_TIMEOUT_SEC,
    stop_fd: int | None = None,
) -> socket.socket:
    """targets を順に試し、最初に繋がった socket を blocking のまま返す。

    各 address の待ちは per_address_timeout と全体の残りの小さい方。stop_fd が読めるように
    なったら接続中の socket を閉じて UpstreamStopped を送出する。全部失敗したときは、
    全試行が期限切れ (全体期限で未試行が残った場合を含む) なら 504、それ以外は 502。
    """
    deadline = time.monotonic() + total_timeout
    saw_error = False
    for addr, port in targets:
        remaining_total = deadline - time.monotonic()
        if remaining_total <= 0:
            break
        family = _family_of(addr)
        if family is None:
            # literal でない値は解決せずに捨てる
            saw_error = True
            continue
        sock = socket.socket(family, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        handed_over = False
        try:
            sock.setblocking(False)
            rc = sock.connect_ex((addr, port))
            if rc not in (0, errno.EINPROGRESS):
                saw_error = True
                continue
            if rc == errno.EINPROGRESS:
                # 各 address の待ちの終わりは、per-address 上限と全体 deadline の早い方の絶対時刻
                wait_end = min(time.monotonic() + per_address_timeout, deadline)
                outcome = _wait_connected(sock, wait_end, stop_fd)
                if outcome == "stopped":
                    raise UpstreamStopped()
                if outcome == "timeout":
                    continue
                err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err != 0:
                    saw_error = True
                    continue
            if time.monotonic() >= deadline:
                # 期限の後に完了した接続は成功扱いにしない (close は finally)
                break
            sock.setblocking(True)
            handed_over = True
            return sock
        finally:
            if not handed_over:
                sock.close()
    if saw_error:
        raise UpstreamError(STATUS_BAD_GATEWAY, "upstream connect failed")
    raise UpstreamError(STATUS_GATEWAY_TIMEOUT, "upstream connect timed out")


def _wait_connected(sock: socket.socket, end: float, stop_fd: int | None) -> str:
    """'ready' / 'timeout' / 'stopped' を返す。end は monotonic の絶対時刻で、poll 前に残りを再計算する。"""
    poller = select.poll()
    poller.register(sock.fileno(), select.POLLOUT)
    if stop_fd is not None:
        poller.register(stop_fd, select.POLLIN)
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return "timeout"
        events = poller.poll(max(1, int(remaining * 1000 + 0.999)))
        for fd, _ev in events:
            if fd == stop_fd:
                return "stopped"
        for fd, _ev in events:
            if fd == sock.fileno():
                return "ready"
