"""mission worker の thread 群で動く CONNECT proxy。

listener は outer Landlock の前に作る (create_listener)。accept した接続は、accept 直後の
時刻を固定してから判定の差し込み口 (admit) に渡し、受理されたものだけを tunnel として
数える。tunnel は 1 本ずつ thread が受け持ち、要求の読取り → 解決 → upstream 接続 →
成功応答 → 中継を順に行う。proxy は TLS の中身 (SNI) も HTTP Host 以外の header も
見ない。保証するのは「resolver が固定した address 集合にしか繋がない」ことだけである。

許可 host が空集合の backend (opencode) では proxy を起こさない前提で作っている。
起こすかどうか・CLI env に proxy URL を入れるかは配線側で決める。
"""

from __future__ import annotations

import errno
import os
import select
import socket
import threading
import time
from collections.abc import Callable
from typing import Protocol

from agentic_fx.egress.connect_parser import Outcome, read_connect_request
from agentic_fx.egress.policy import CONNECT_PORT
from agentic_fx.egress.relay import IDLE_TIMEOUT_SEC, RelayEnd, Tunnel, TunnelState, relay
from agentic_fx.egress.resolver import ResolveError
from agentic_fx.egress.upstream import (
    PER_ADDRESS_TIMEOUT_SEC,
    TOTAL_TIMEOUT_SEC,
    UpstreamError,
    UpstreamStopped,
    connect_fixed,
)

LISTEN_HOST = "127.0.0.1"
LISTEN_BACKLOG = 128
MAX_TUNNELS = 32  # accept から CLOSED までを 1 本と数える
SUCCESS_RESPONSE = b"HTTP/1.1 200 Connection established\r\n\r\n"
SUCCESS_SEND_TIMEOUT_SEC = 5.0
# status 行を書いた後に、client の未読データを読み捨てて待つ上限。未読を抱えたまま close すると
# RST になり、client の手元の status 行が捨てられる
LINGER_TIMEOUT_SEC = 1.0
MAX_LINGERING = 32  # 上限超過の 503 を読み捨てて待つ接続の同時数。超えたら待たずに閉じる

# client に status 行だけを書いて閉じる失敗。ここに無い status (許可外 host の 403) は
# 何も書かずに閉じ、proxy の内部状態を返さない。
STATUS_LINES: dict[int, bytes] = {
    502: b"HTTP/1.1 502 Bad Gateway\r\n\r\n",
    503: b"HTTP/1.1 503 Service Unavailable\r\n\r\n",
    504: b"HTTP/1.1 504 Gateway Timeout\r\n\r\n",
}

# accept の一過性の失敗 (fd・メモリ枯渇、接続の中断) は待てば直るので再試行する。
# 連続して失敗し続けた時間が上限を超えたら、直らないものとして proxy_died にする
ACCEPT_BACKOFF_INITIAL_SEC = 0.05
ACCEPT_BACKOFF_MAX_SEC = 1.0
ACCEPT_FAILURE_LIMIT_SEC = 10.0
_TRANSIENT_ACCEPT_ERRNOS = frozenset(
    {errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM, errno.ECONNABORTED, errno.EINTR}
)

PROXY_START_FAILED = "proxy_start_failed"
PROXY_DIED = "proxy_died"


class ProxyStartFailed(Exception):
    reason = PROXY_START_FAILED


class Resolver(Protocol):
    def resolve(self, host: str) -> tuple[str, ...]: ...

    def close(self, deadline: float) -> None: ...


Admit = Callable[[socket.socket], bool]
Connector = Callable[..., socket.socket]
TunnelObserver = Callable[[Tunnel], None]


def reject_all(sock: socket.socket) -> bool:
    """判定が差し込まれていない構成の既定。全部拒否する (fail closed)。"""
    return False


def create_listener() -> socket.socket:
    """127.0.0.1 の ephemeral port に backlog 128 で listen する。失敗は ProxyStartFailed。"""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        sock.bind((LISTEN_HOST, 0))
        sock.listen(LISTEN_BACKLOG)
        return sock
    except OSError as exc:
        if sock is not None:
            sock.close()
        raise ProxyStartFailed(str(exc)) from exc


def _drain_until_eof(sock: socket.socket, stop_fd: int | None) -> None:
    """SHUT_WR の後、相手の EOF か期限まで受信を読み捨てる。停止の合図でも切り上げる。"""
    deadline = time.monotonic() + LINGER_TIMEOUT_SEC
    try:
        sock.shutdown(socket.SHUT_WR)
        poller = select.poll()
        poller.register(sock.fileno(), select.POLLIN)
        if stop_fd is not None:
            poller.register(stop_fd, select.POLLIN)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            events = dict(poller.poll(remaining * 1000))
            if stop_fd is not None and stop_fd in events:
                return
            if sock.fileno() in events:
                if not sock.recv(65536, socket.MSG_DONTWAIT):
                    return
    except (OSError, ValueError):
        return


def _send_status(sock: socket.socket, status: int, stop_fd: int | None = None) -> None:
    line = STATUS_LINES.get(status)
    if line is None:
        return
    try:
        # 数十 byte なので送信 buffer に必ず入る。相手が読まなくても待たない
        sock.send(line, socket.MSG_DONTWAIT | socket.MSG_NOSIGNAL)
    except OSError:
        return
    _drain_until_eof(sock, stop_fd)


class ConnectProxy:
    def __init__(
        self,
        listener: socket.socket,
        resolver: Resolver,
        *,
        admit: Admit = reject_all,
        upstream_port: int = CONNECT_PORT,
        idle_timeout: float = IDLE_TIMEOUT_SEC,
        per_address_timeout: float = PER_ADDRESS_TIMEOUT_SEC,
        total_timeout: float = TOTAL_TIMEOUT_SEC,
        connector: Connector = connect_fixed,
        on_tunnel_closed: TunnelObserver | None = None,
    ) -> None:
        self._listener = listener
        self._resolver = resolver
        self._admit = admit
        self._upstream_port = upstream_port
        self._idle_timeout = idle_timeout
        self._per_address_timeout = per_address_timeout
        self._total_timeout = total_timeout
        self._connector = connector
        self._on_tunnel_closed = on_tunnel_closed
        self._port = listener.getsockname()[1]
        self._lock = threading.Lock()
        self._tunnels: set[Tunnel] = set()
        self._threads: set[threading.Thread] = set()
        self._stopping = False
        self._lingering = 0
        self._failure: str | None = None
        self._stopped = False
        # 停止の合図。書いた後は読まないので、待っている全 thread の poll が起き続ける
        self._stop_fd = os.eventfd(0, os.EFD_CLOEXEC | os.EFD_NONBLOCK)
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="egress-proxy-accept", daemon=True
        )

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://{LISTEN_HOST}:{self._port}"

    @property
    def failure(self) -> str | None:
        """accept thread が停止要求なしに終わったら proxy_died。"""
        with self._lock:
            return self._failure

    def active_tunnels(self) -> int:
        with self._lock:
            return len(self._tunnels)

    def start(self) -> None:
        self._listener.setblocking(False)
        self._accept_thread.start()

    def stop(self, deadline: float) -> bool:
        """monotonic の absolute deadline までに proxy を止める。全 thread を join できたら True。

        順序は listener の close → 全 tunnel の shutdown → resolver.close(deadline) → join。
        """
        with self._lock:
            if self._stopped:
                return True
            self._stopping = True
            tunnels = list(self._tunnels)
        os.eventfd_write(self._stop_fd, 1)
        try:
            self._listener.close()
        except OSError:
            pass
        for t in tunnels:
            t.shutdown_sockets()
        self._resolver.close(deadline)
        # stopping を立てた後は新しい tunnel を登録しないので、ここで集めた thread で全部
        threads = [self._accept_thread] if self._accept_thread.ident is not None else []
        with self._lock:
            threads += list(self._threads)
        for th in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            th.join(remaining)
        alive = any(th.is_alive() for th in threads)
        if not alive:
            # 残った thread が poll している間は閉じない (番号の再利用で別の fd を待たせない)
            os.close(self._stop_fd)
            with self._lock:
                self._stopped = True
        return not alive

    def _accept_loop(self) -> None:
        clean = False
        try:
            poller = select.poll()
            poller.register(self._listener.fileno(), select.POLLIN)
            poller.register(self._stop_fd, select.POLLIN)
            failing_since: float | None = None
            backoff = ACCEPT_BACKOFF_INITIAL_SEC
            while True:
                if self._is_stopping():
                    clean = True
                    return
                events = dict(poller.poll())
                if self._stop_fd in events or self._is_stopping():
                    clean = True
                    return
                try:
                    conn, _ = self._listener.accept()
                except (BlockingIOError, InterruptedError, ConnectionAbortedError):
                    continue
                except OSError as exc:
                    if exc.errno not in _TRANSIENT_ACCEPT_ERRNOS:
                        raise
                    now = time.monotonic()
                    if failing_since is None:
                        failing_since = now
                    if now - failing_since > ACCEPT_FAILURE_LIMIT_SEC:
                        raise
                    # 待ち中も stop の合図で起きる
                    if self._wait_stop(backoff):
                        clean = True
                        return
                    backoff = min(backoff * 2, ACCEPT_BACKOFF_MAX_SEC)
                    continue
                failing_since = None
                backoff = ACCEPT_BACKOFF_INITIAL_SEC
                # 期限の起点。ここで一度だけ取り、要求の読取りまで書き換えない
                accepted_at = time.monotonic()
                self._on_accepted(conn, accepted_at)
        except Exception:  # noqa: BLE001 - 理由を問わず accept thread の異常終了は proxy_died
            if self._is_stopping():
                clean = True
        finally:
            if not clean:
                with self._lock:
                    self._failure = PROXY_DIED

    def _wait_stop(self, timeout: float) -> bool:
        """停止の合図を timeout 秒まで待つ。停止中なら True (stop は _stopping を立ててから合図を書く)。"""
        poller = select.poll()
        poller.register(self._stop_fd, select.POLLIN)
        poller.poll(timeout * 1000)
        return self._is_stopping()

    def _is_stopping(self) -> bool:
        with self._lock:
            return self._stopping

    def _on_accepted(self, conn: socket.socket, accepted_at: float) -> None:
        # 判定は上限より先に行う。supervisor を経ない接続には 503 も含めて何も返さない
        try:
            admitted = self._admit(conn) is True
        except Exception:  # noqa: BLE001 - 判定の失敗は拒否として扱う
            admitted = False
        if not admitted:
            conn.close()
            return
        with self._lock:
            if self._stopping:
                rejected = True
            elif len(self._tunnels) >= MAX_TUNNELS:
                rejected = True
                if self._lingering < MAX_LINGERING:
                    # accept thread を止めないよう、読み捨てて待つのは別 thread に任せる
                    linger = threading.Thread(
                        target=self._run_reject, args=(conn,), name="egress-proxy-reject", daemon=True
                    )
                    try:
                        linger.start()
                        self._threads.add(linger)
                        self._lingering += 1
                        return
                    except RuntimeError:
                        pass
            else:
                tunnel = Tunnel(conn)
                thread = threading.Thread(
                    target=self._run_tunnel,
                    args=(tunnel, accepted_at),
                    name="egress-proxy-tunnel",
                    daemon=True,
                )
                # 登録と start を同じ lock の下で行い、stop が未 start の thread を join しないようにする
                try:
                    thread.start()
                    self._tunnels.add(tunnel)
                    self._threads.add(thread)
                    rejected = False
                except RuntimeError:
                    rejected = True
        if rejected:
            conn.close()

    def _run_reject(self, conn: socket.socket) -> None:
        try:
            _send_status(conn, 503, self._stop_fd)
        finally:
            conn.close()
            with self._lock:
                self._lingering -= 1
                self._threads.discard(threading.current_thread())

    def _run_tunnel(self, tunnel: Tunnel, accepted_at: float) -> None:
        try:
            self._serve(tunnel, accepted_at)
        except Exception:  # noqa: BLE001 - tunnel 1 本の想定外の失敗は fault として閉じる
            tunnel.end = RelayEnd.FAULT
        finally:
            tunnel.close()
            with self._lock:
                self._tunnels.discard(tunnel)
                self._threads.discard(threading.current_thread())
            if self._on_tunnel_closed is not None:
                try:
                    self._on_tunnel_closed(tunnel)
                except Exception:  # noqa: BLE001 - 観測側の失敗で tunnel の後始末を崩さない
                    pass

    def _serve(self, tunnel: Tunnel, accepted_at: float) -> None:
        client = tunnel.client
        result = read_connect_request(client, accepted_at=accepted_at)
        if result.outcome is not Outcome.DONE:
            return  # 拒否・期限切れ・EOF は parser が無応答で閉じている
        host = result.parser.hostname
        assert host is not None
        try:
            addresses = self._resolver.resolve(host)
        except ResolveError as exc:
            _send_status(client, exc.status, self._stop_fd)
            return
        if self._is_stopping():
            return
        # 名前ではなく、固定された address 文字列と定数 port の組だけを接続側に渡す
        targets = [(addr, self._upstream_port) for addr in addresses]
        try:
            upstream = self._connector(
                targets,
                per_address_timeout=self._per_address_timeout,
                total_timeout=self._total_timeout,
                stop_fd=self._stop_fd,
            )
        except UpstreamStopped:
            return
        except UpstreamError as exc:
            _send_status(client, exc.status, self._stop_fd)
            return
        if not tunnel.attach_upstream(upstream):
            upstream.close()
            return
        client.settimeout(SUCCESS_SEND_TIMEOUT_SEC)
        client.sendall(SUCCESS_RESPONSE)
        tunnel.set_state(TunnelState.OPEN)
        relay(
            tunnel,
            preread=result.parser.preread,
            idle_timeout=self._idle_timeout,
            stop_fd=self._stop_fd,
        )
