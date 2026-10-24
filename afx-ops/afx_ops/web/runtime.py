"""`afx-ops web` の業務処理: pre-bind 済み listener の接続を aiohttp の低レベル server で受け、
read-only 表示の adapter (`afx_ops.web.handle_request`) へ渡す。

起動順 (隔離・自己検査・server 認証・READY=1) は `startup.run` が済ませてから呼ばれる。
listener の accept は `AcceptLoop` だけが行い (隔離後は bind / listen を kernel が拒む)、
受けた socket を `loop.connect_accepted_socket` で aiohttp の protocol に渡す。
accept の fd 枯渇は `AcceptLoop` が `SystemExit(72)` で process を終える。ここでは握らない。

aiohttp は起動口の preload (隔離前) が import する。この module は import 時に aiohttp を
読まない (起動口の import で preload の失敗経路を飛ばさないため)。

要求の検査 (method / path / query / header / body の上限) と表示は adapter が受け持つ。
ここは aiohttp が解釈した要求を adapter が読む bytes に戻し、同期の `WebClient` 呼出しを
含む adapter 全体を上限付きの thread pool で回し、応答を aiohttp の応答へ写すだけである。
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from asyncio import selector_events
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait

from afx_ops.api_client import HTTP_TIMEOUT, WebClient
from afx_ops.isolation.accept_loop import AcceptLoop
from afx_ops.isolation.startup import WEB_PASSWORD, client_for
from afx_ops.web.auth import BasicAuthenticator
from afx_ops.web.headers import response_headers
from afx_ops.web.render import render_error
from afx_ops.web.request import (
    MAX_BODY_BYTES,
    MAX_HEADER_BYTES,
    MAX_REQUEST_LINE,
    HostPolicy,
    handle_request,
    wire_authorization_values,
)

# 同時に開いておく接続の上限。超えた接続は受けてすぐ閉じる (fd と memory を有界にする)
MAX_CONNECTIONS = 16
# daemon への同期呼出しを回す thread の数
MAX_WORKERS = 4
# 1 要求の header 行数の上限 (1 行は MAX_HEADER_BYTES まで)。aiohttp の C parser はこの値を
# 強制しないので、下の受信量の上限が実際の境界になる
MAX_HEADERS = 64
# 1 接続で受け取る bytes の上限 (1 接続 1 要求なので、要求 1 本の上限の和)。header を
# 小さく大量に送る接続は、adapter が大きさを検査する前に aiohttp が全部を溜めるので、
# 受信の段階で切る
MAX_CONNECTION_BYTES = MAX_REQUEST_LINE + MAX_HEADER_BYTES + MAX_BODY_BYTES + 4
# 1 接続の寿命の上限。header を少しずつ送り続ける接続が枠を占有し続けないようにする
CONNECTION_DEADLINE = 30.0
KEEPALIVE_TIMEOUT = 5.0
LINGERING_TIME = 1.0
SERVER_HEADER = "afx-ops"
# 止めるとき、実行中の daemon 呼出しが自然に終わるのを待つ時間。超えたら接続を切って割り込む
SHUTDOWN_GRACE = 2.0

RC_OK = 0

# aiohttp の error log は要求由来の文字列を含む例外を出し得る。handler を持たない logger は
# Python の lastResort が stderr へ出すので、出力しない logger を渡す
_SILENT = logging.getLogger("afx_ops.web.silent")
_SILENT.addHandler(logging.NullHandler())
_SILENT.propagate = False
_SILENT.disabled = True


def run(isolated, *, client=None, on_serving: Callable[[], None] | None = None,
        stop_after: int | None = None) -> int:
    """production は `isolated` だけで呼ばれ、fd 枯渇 (rc 72 の SystemExit) まで戻らない。

    client / on_serving / stop_after は試験だけが渡す。on_serving は accept を始めた直後に
    1 回呼ばれ、stop_after は閉じた接続の数がそれに達したら rc 0 で戻る。
    """
    use_send_only_transport()
    if client is None:
        client = client_for(isolated, WebClient)
    config = isolated.config
    authenticator = (BasicAuthenticator(isolated.secrets[WEB_PASSWORD], user=config.user)
                     if config.auth == "basic" else None)
    asyncio.run(_serve(_aiohttp_parts(), isolated.listener, client, on_serving, stop_after,
                       allow=config.web_allow_networks(),
                       host_policy=HostPolicy(config.web_bind, config.port, config.web_loopback),
                       authenticator=authenticator))
    return RC_OK


def use_send_only_transport() -> None:
    """asyncio の socket transport が sendmsg を使わない経路にする。

    web の seccomp は sendmsg を拒む。transport は送信 buffer が残ると sendmsg で書くので、
    send だけを使う経路にする (transport の生成時に読まれる)。この切替は asyncio の
    private な名前なので、無い版では accept を始める前に起動を失敗させる (fail closed)。
    """
    if not hasattr(selector_events, "_HAS_SENDMSG"):
        raise RuntimeError("web_transport_incompatible")
    selector_events._HAS_SENDMSG = False


class _Call:
    """thread pool に出した daemon 呼出し 1 件。接続が先に切れたときに取り消す対象。"""

    __slots__ = ("future", "ident", "abandoned")

    def __init__(self) -> None:
        self.future: Future | None = None
        # 実行中の worker thread (まだ始まっていない / もう終わったなら None)
        self.ident: int | None = None
        self.abandoned = False


class _Slot:
    """受けた接続 1 本分の枠。transport と寿命の timer、その接続の daemon 呼出しを持つ。

    接続が閉じたとき (切断・期限・abort) は `on_release` を呼ぶ。
    """

    __slots__ = ("transport", "deadline", "on_release", "calls", "peer")

    def __init__(self, on_release: Callable[["_Slot"], None], peer: str) -> None:
        self.peer = peer
        self.transport: asyncio.BaseTransport | None = None
        self.deadline: asyncio.TimerHandle | None = None
        self.on_release = on_release
        self.calls: set[_Call] = set()

    def close(self) -> None:
        if self.deadline is not None:
            self.deadline.cancel()
            self.deadline = None
        if self.transport is not None:
            self.transport.abort()


def _aiohttp_parts():
    """aiohttp に依存する部品 (protocol、server、応答) を作る。"""
    from aiohttp import web, web_protocol
    from aiohttp.http_exceptions import LineTooLong

    class _Protocol(web_protocol.RequestHandler):
        """1 接続の aiohttp protocol。parser が拒んだ要求も固定の応答と header で返す。"""

        def __init__(self, manager: "_Server", slot: _Slot, **kwargs) -> None:
            super().__init__(manager, **kwargs)
            self._slot = slot
            self._received = 0
            # 先頭の header block (終端まで) の wire の bytes。終端が見えたら None にして数えない
            self._head: bytearray | None = bytearray()
            # header block (終端の空行を除く) の wire の bytes。Authorization を OWS の
            # 正規化前のまま認証器へ渡すために残す
            self.wire_head = b""
            # 先頭 5 bytes (method の語)。HEAD への error 応答は本文を持たないので、
            # parser が拒む前の bytes から見分ける
            self._lead: bytearray | None = bytearray()
            self._is_head = False
            self._cut = False

        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            self._slot.transport = transport
            self._slot.deadline = self._loop.call_later(CONNECTION_DEADLINE, transport.abort)
            super().connection_made(transport)

        def data_received(self, data: bytes) -> None:
            self._received += len(data)
            if self._received > MAX_CONNECTION_BYTES:
                # 応答の途中かもしれないので、固定の応答は返さずに切る
                if self.transport is not None:
                    self.transport.abort()
                return
            if self._lead is not None:
                self._lead += data[:5 - len(self._lead)]
                if len(self._lead) >= 5:
                    self._is_head = self._lead == b"HEAD "
                    self._lead = None
            if self._cut:
                return
            if self._head is not None and self._head_too_large(data):
                self._refuse_large_head()
                return
            super().data_received(data)

        def _head_too_large(self, data: bytes) -> bool:
            # aiohttp は値の前後の空白を捨てるので、再構成後の要求では header の大きさを
            # 測れない。wire の bytes を終端 (空行) まで数え、上限を超えたら aiohttp に渡さない
            assert self._head is not None
            self._head += data
            end = self._head.find(b"\r\n\r\n")
            if end < 0:
                return len(self._head) >= MAX_HEADER_BYTES
            self.wire_head = bytes(self._head[:end])
            self._head = None
            return end + 4 > MAX_HEADER_BYTES

        def _refuse_large_head(self) -> None:
            self._cut = True
            self._head = None
            transport = self.transport
            if transport is None:
                return
            body = render_error(413)
            headers = {**response_headers(length=len(body)), "Server": SERVER_HEADER,
                       "Connection": "close"}
            if self._is_head:
                body = b""
            lines = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
            transport.pause_reading()
            transport.write(b"HTTP/1.1 413 Request Entity Too Large\r\n" + lines.encode("ascii")
                            + b"\r\n" + body)
            transport.close()

        def connection_lost(self, exc: BaseException | None) -> None:
            try:
                super().connection_lost(exc)
            finally:
                self._slot.on_release(self._slot)

        def handle_error(self, request, status: int = 500, exc: BaseException | None = None,
                         message: str | None = None) -> web.StreamResponse:
            # aiohttp の既定は例外文を本文と log に出す。固定の本文と安全 header だけにする
            if request.writer.output_size > 0:
                raise ConnectionError("response already started")
            if isinstance(exc, LineTooLong):
                code = 413
            elif 400 <= status < 500:
                code = 400
            else:
                code = 500
            body = render_error(code)
            return _response(code, response_headers(length=len(body)),
                             b"" if self._is_head else body)

    class _Server(web.Server):
        def protocol(self, slot: _Slot) -> _Protocol:
            return _Protocol(self, slot, loop=self._loop, **self._kwargs)

    def _response(status: int, headers: dict[str, str], body: bytes) -> web.Response:
        reply = web.Response(status=status, body=body,
                             headers={**headers, "Server": SERVER_HEADER})
        # 1 接続 1 要求。keep-alive で接続を使い回させない
        reply.force_close()
        return reply

    return _Server, _response


async def _raw_request(request) -> bytes:
    """aiohttp が解釈した要求を、adapter が検査する HTTP/1.1 の bytes に戻す。

    target は decode 前の生の値 (absolute-form や `%` もそのまま adapter が拒む)。body は
    Content-Length が上限以内のときだけ読む (超えたら読まずに長さだけ渡し、adapter が 413
    にする。chunked は Transfer-Encoding header が残るので adapter が拒む)。
    """
    version = request.version
    line = f"{request.method} {request.raw_path} HTTP/{version.major}.{version.minor}"
    head = [line.encode("utf-8", "surrogateescape")]
    # 区切りは空白なしの `:`。wire が `Name: value` でも `Name:value` でも、再構成後が
    # wire より大きくならない (wire の大きさで測った上限を adapter の検査も超えない)
    head += [name + b":" + value for name, value in request.raw_headers]
    length = request.content_length
    body = b""
    if length is not None and 0 < length <= MAX_BODY_BYTES:
        body = await request.content.readexactly(length)
    return b"\r\n".join(head) + b"\r\n\r\n" + body


class _Service:
    def __init__(self, parts, loop: asyncio.AbstractEventLoop, client,
                 executor: ThreadPoolExecutor, stop_after: int | None, *,
                 host_policy: HostPolicy | None = None,
                 authenticator: BasicAuthenticator | None = None) -> None:
        server_cls, self._response = parts
        self._host_policy = host_policy
        self._authenticator = authenticator
        self._loop = loop
        self._client = client
        self._executor = executor
        self._stop_after = stop_after
        self.slots: set[_Slot] = set()
        # 実行中の daemon 呼出し (thread 側の future)。止めるときに終わりを待つ
        self.calls: set[Future] = set()
        self.tasks: set[asyncio.Task] = set()
        self.closed = 0
        self.refused = 0
        self.stopped = asyncio.Event()
        # worker 側の thread id と取消しの判定を、始まり・終わり・取消しの間で食い違わせない
        self._calls_lock = threading.Lock()
        self.server = server_cls(self.handle, loop=loop,
                                 max_line_size=MAX_REQUEST_LINE, max_field_size=MAX_HEADER_BYTES,
                                 max_headers=MAX_HEADERS, keepalive_timeout=KEEPALIVE_TIMEOUT,
                                 lingering_time=LINGERING_TIME, tcp_keepalive=False,
                                 access_log=None, logger=_SILENT)

    async def handle(self, request):
        raw = await _raw_request(request)
        slot = request.protocol._slot
        call = _Call()
        if slot not in self.slots:
            return self._response(*_closed_reply())
        protocol = request.protocol
        authorization = wire_authorization_values(protocol.wire_head)
        # 認証の根拠はこの要求の header block だけ。2 本目以降は取り出せず 401 になる
        protocol.wire_head = b""
        call.future = self._executor.submit(self._run, call, raw, slot.peer, authorization)
        slot.calls.add(call)
        self.calls.add(call.future)
        call.future.add_done_callback(self.calls.discard)
        try:
            reply = await asyncio.wrap_future(call.future, loop=self._loop)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if call.abandoned and (task is None or task.cancelling() == 0):
                # 接続が先に閉じた。応答の送り先はもう無い
                return self._response(*_closed_reply())
            raise
        finally:
            slot.calls.discard(call)
        return self._response(reply.status, reply.headers, reply.body)

    def _run(self, call: _Call, raw: bytes, peer: str, authorization: list[str]):
        """worker thread で adapter を回す。取り消された呼出しは daemon への接続ごと切る。"""
        ident = threading.get_ident()
        interrupt = getattr(self._client, "interrupt_thread", None)
        release = getattr(self._client, "release_thread", None)
        with self._calls_lock:
            call.ident = ident
            if call.abandoned and interrupt is not None:
                interrupt(ident)
        try:
            return handle_request(raw, self._client, host_policy=self._host_policy,
                                  authenticator=self._authenticator, peer=peer,
                                  authorization=authorization)
        finally:
            with self._calls_lock:
                call.ident = None
                if call.abandoned and release is not None:
                    release(ident)

    def _abandon(self, slot: _Slot) -> None:
        """閉じた接続の呼出しを取り消す。始まる前なら破棄、実行中なら daemon との接続を切る。"""
        interrupt = getattr(self._client, "interrupt_thread", None)
        for call in list(slot.calls):
            with self._calls_lock:
                call.abandoned = True
                if call.future is not None and call.future.cancel():
                    continue
                if call.ident is not None and interrupt is not None:
                    interrupt(call.ident)

    def accept(self, conn, peer: str) -> None:
        # 送信元の判定は accept_loop の allow 照合で完了している
        if len(self.slots) >= MAX_CONNECTIONS:
            self.refused += 1
            conn.close()
            return
        slot = _Slot(self.released, peer)
        self.slots.add(slot)
        task = self._loop.create_task(self._connect(conn, slot))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _connect(self, conn, slot: _Slot) -> None:
        try:
            await self._loop.connect_accepted_socket(lambda: self.server.protocol(slot), conn)
        except BaseException:
            # transport ができる前の失敗や取消しでは socket と枠が残るので返す
            conn.close()
            self.released(slot)
            raise

    def released(self, slot: _Slot) -> None:
        if slot not in self.slots:
            return
        self.slots.discard(slot)
        slot.close()
        self._abandon(slot)
        self.closed += 1
        if self._stop_after is not None and self.closed >= self._stop_after:
            self.stopped.set()

    def close(self) -> None:
        for slot in list(self.slots):
            slot.close()
            # 開いたままの接続の呼出しも、切れた接続と同じ取消しに通す
            self._abandon(slot)
        self.slots.clear()
        for task in list(self.tasks):
            task.cancel()

    def drain(self) -> None:
        """実行中の daemon 呼出しが終わるまで待つ。固まっていれば接続を切って割り込む。

        thread は切断された呼出しが応答喪失で戻れば終わる。それでも戻らない呼出し
        (接続前の待ち) は HTTP_TIMEOUT が上限。
        """
        _done, pending = wait(list(self.calls), timeout=SHUTDOWN_GRACE)
        if not pending:
            return
        interrupt = getattr(self._client, "interrupt", None)
        if interrupt is not None:
            interrupt()
        wait(list(pending), timeout=HTTP_TIMEOUT)


def _closed_reply() -> tuple[int, dict[str, str], bytes]:
    body = render_error(502)
    return 502, response_headers(length=len(body)), body


def _loop_error(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    # 既定の handler は例外文と traceback (要求由来の値を含み得る) を出す。型名だけにする
    exc = context.get("exception")
    name = type(exc).__name__ if exc is not None else "none"
    print(f"afx-ops: web_loop_error {name}", file=sys.stderr, flush=True)


async def _serve(parts, listener, client, on_serving, stop_after, *,
                 allow=None, host_policy: HostPolicy | None = None,
                 authenticator: BasicAuthenticator | None = None) -> None:
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_loop_error)
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="afx-web")
    service = _Service(parts, loop, client, executor, stop_after, host_policy=host_policy,
                       authenticator=authenticator)
    acceptor = (AcceptLoop(listener, service.accept) if allow is None
                else AcceptLoop(listener, service.accept, allow=allow))
    try:
        acceptor.attach(loop)
        if on_serving is not None:
            on_serving()
        await service.stopped.wait()
    finally:
        # fd 枯渇で AcceptLoop が listener を閉じた後は reader が外れている
        if listener.fileno() >= 0:
            acceptor.detach(loop)
        service.close()
        executor.shutdown(False, cancel_futures=True)
        # 待つ間も loop は回し続ける (connection_lost や timer の取消しが進む)
        await loop.run_in_executor(None, service.drain)
