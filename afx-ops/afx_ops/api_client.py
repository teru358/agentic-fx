"""daemon の操作 API を呼ぶ唯一の経路 (UDS、1 接続 1 要求)。

要求ごとに socket path を新しく lstat して connect し、次の順で確かめてから鍵を送る。

1. 共有契約の接続前検査 (親 dir・socket の lstat)
2. connect
3. 固有 admission: ``SO_PEERPIDFD`` を必ず取り fdinfo の pid を照合、instance lock の dev 一致
4. 共有契約の接続後検査 (``SO_PEERCRED`` uid、保持者 = peer pid、保持者の生存)
5. 送信直前に pidfd を poll (POLLIN なら相手は終了済み)

どこか一つでも確定できなければ鍵は 1 byte も送らない。応答待ちは socket の読取り可能と
pidfd の POLLIN を同時に poll し、相手の終了を先に見たら応答を受け付けない。pidfd は
要求 object (``_Request``) が取得から要求終了まで持ち、``close`` だけが一回閉じる。

afx 再起動で socket / lock の inode が変わっても、次の要求は新しい path を確かめ直す。
認証できなければ ``ApiError`` を返すだけで process 内では再試行しない (起動口は
``run_guarded`` で非零終了し、systemd の restart に委ねる)。

HTTP は stdlib socket の最小実装: ``Content-Length`` 必須、chunked 不可、
``Connection: close``、JSON。応答喪失の再送はしない (ops の principal が届く endpoint に
``Idempotency-Key`` 付きのものは無く、決定は正本を読み直して扱うため)。
"""
from __future__ import annotations

import errno
import json
import os
import re
import select
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO
from urllib.parse import quote, urlencode

from afx_ops import server_auth
from afx_ops.keys import KeyReadError, read_token
from afx_ops.server_auth import PROC_LOCKS, ServerAuthError, SocketMissing

READINESS_INTERVAL = 0.25
READINESS_LIMIT = 30.0
HTTP_TIMEOUT = 15.0
ACTOR_HEADER = "X-Afx-Asserted-Actor"
ACTOR_MAX_BYTES = 28
_ACTOR = re.compile(r"discord:[0-9]{17,20}")
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_MAX_RESPONSE = 8 * 1024 * 1024

RC_OK = 0
RC_UNREACHABLE = 3
RC_KEY = 4

# 要求経路の失敗の語。server 認証の語 (server_auth.REASONS) に加え、鍵を送る前の失敗
# (socket 無し・接続拒否・socket を使えない・connect の期限切れ・起動待ちの期限・actor
# 形式外) と、鍵を送った後の応答喪失。
CLIENT_REASONS = ("no_socket", "refused", "socket_unusable", "timeout", "readiness_timeout",
                  "invalid_actor", "response_lost")
# 起動待ちが待ってよい失敗 (daemon がまだ listen していない・fd の一時的な枯渇)。
# それ以外 (権限・socket でない等の恒久的な失敗、認証の失敗) は待たずに止める。
_WAITABLE = ("no_socket", "refused", "pidfd_transient")


class ApiError(Exception):
    """要求が成立しなかった。``reason`` は固定語。

    ``token_sent`` が False なら鍵は 1 byte も送っていない。
    """

    def __init__(self, reason: str, *, token_sent: bool = False, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.token_sent = token_sent
        # server 認証の失敗段 (``step=<段> errno=<n>``)。stderr の診断専用で、応答や通知には出さない
        self.detail = detail


_DETAIL_SHAPE = re.compile(r"step=[a-z_]{1,16} errno=[0-9]{1,4}")


def with_detail(reason: str, detail: object) -> str:
    """stderr の 1 行用に ``<reason> detail=<detail>``。detail が固定形でなければ reason だけ。"""
    if type(detail) is str and _DETAIL_SHAPE.fullmatch(detail):
        return f"{reason} detail={detail}"
    return reason


@dataclass(frozen=True)
class Endpoint:
    """接続先。path は機能別設定の絶対 path。"""
    socket_path: Path
    lock_path: Path
    uid: int = field(default_factory=os.getuid)
    proc_locks: Path = PROC_LOCKS


@dataclass(frozen=True)
class Reply:
    status: int
    data: dict | None
    code: str | None
    # Retry-After header の秒数 (整数の形だけ読む。無い・日付形式・不正は None)
    retry_after: int | None = None


class Actor:
    """``X-Afx-Asserted-Actor`` の値。形式を通ったものだけが作れる。"""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        _check_actor_text(value)
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Actor) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        return f"Actor({self._value!r})"


def _check_actor_text(value: object) -> str:
    # \d は全角数字にも一致するので [0-9] の fullmatch で決める (末尾改行も許さない)。
    if (type(value) is not str or not value.isascii()
            or len(value.encode("ascii")) > ACTOR_MAX_BYTES
            or _ACTOR.fullmatch(value) is None):
        raise ApiError("invalid_actor")
    return value


# --------------------------------------------------------------- 1 接続 1 要求

class _Request:
    """接続 1 本と、その相手の pidfd を所有する。

    ``open`` は鍵を送る前の全検査を済ませた状態で返す。``close`` は socket と pidfd を
    一回だけ閉じる (pidfd を閉じる箇所はここだけ)。
    """

    def __init__(self, endpoint: Endpoint, *, timeout: float = HTTP_TIMEOUT,
                 deadline: float | None = None) -> None:
        self._endpoint = endpoint
        self._timeout = timeout
        self._deadline = deadline
        self._sock: socket.socket | None = None
        self._pidfd = -1
        self._sent = False

    @classmethod
    def open(cls, endpoint: Endpoint, *, timeout: float = HTTP_TIMEOUT,
             deadline: float | None = None) -> "_Request":
        """``deadline`` (``time.monotonic`` の時刻) があれば、socket を使う admission の各段にも
        残り時間を渡す。socket を使わない段 (lock device の検査) は上限の外で、合計は
        ``deadline`` にその段の所要を足した時間まで延び得る。"""
        request = cls(endpoint, timeout=timeout, deadline=deadline)
        try:
            request._connect_and_verify()
        except BaseException:
            request.close()
            raise
        return request

    def _connect_and_verify(self) -> None:
        ep = self._endpoint
        try:
            server_auth.check_socket_path(ep.socket_path, uid=ep.uid)
            # connect・送信・受信のどれも HTTP_TIMEOUT で打ち切る (相手が accept / 読取りを
            # しなくなっても固まらない)
            self._sock = server_auth.connect_unix(ep.socket_path, timeout=self._timeout)
        except SocketMissing:
            raise ApiError("no_socket") from None
        except ServerAuthError as err:
            raise ApiError(err.reason, detail=err.detail) from None
        except OSError as exc:
            raise ApiError(_connect_failure(exc)) from None
        try:
            # 固有 admission (共有契約の接続後検査より前)。
            self._spend_remaining()
            self._pidfd = server_auth.fetch_pidfd(self._sock)
            server_auth.check_pidfd(self._sock, self._pidfd)
            server_auth.check_lock_device(ep.lock_path, proc_locks=ep.proc_locks)
            # 共有契約の接続後検査。pidfd はこの object が持ち、dev の救済はしない。
            self._spend_remaining()
            server_auth.verify_peer(self._sock, uid=ep.uid, lock_path=ep.lock_path,
                                    proc_locks=ep.proc_locks, pidfd=self._pidfd,
                                    dev_fallback=False)
        except ServerAuthError as err:
            raise ApiError(err.reason, detail=err.detail) from None

    def _spend_remaining(self) -> None:
        """deadline が指定されていれば、残り時間を socket の期限にする (尽きていれば期限切れ)。"""
        if self._deadline is None or self._sock is None:
            return
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise ApiError("timeout")
        self._sock.settimeout(left)

    def _peer_exited(self) -> bool:
        return server_auth.pidfd_exited(self._pidfd)

    def send(self, payload: bytes) -> None:
        """送信直前に pidfd を poll し、相手が生きている場合だけ全部書く。"""
        if self._sock is None or self._pidfd < 0 or self._sent:
            raise ApiError("refused")
        if self._peer_exited():
            raise ApiError("peer_gone")
        self._sent = True
        try:
            self._sock.sendall(payload)
        except OSError:
            raise ApiError("response_lost", token_sent=True) from None

    def limit(self, seconds: float) -> None:
        """以後の送受信に使う socket の期限 (要求全体の残り時間に合わせるため)。"""
        if self._sock is not None:
            self._sock.settimeout(seconds)

    def receive(self, *, timeout: float = HTTP_TIMEOUT) -> tuple[int, bytes, int | None]:
        """応答を読む。相手の終了・期限・形式不正はすべて応答喪失。"""
        assert self._sock is not None and self._sent
        deadline = time.monotonic() + timeout
        poller = select.poll()
        poller.register(self._sock.fileno(), select.POLLIN)
        poller.register(self._pidfd, select.POLLIN)
        buffer = b""
        while True:
            parsed = _parse_response(buffer)
            if parsed is not None:
                return parsed
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ApiError("response_lost", token_sent=True)
            events = dict(poller.poll(max(1, int(remaining * 1000))))
            # 相手の終了を見たら、同時に届いている bytes があっても応答として扱わない。
            if events.get(self._pidfd, 0):
                raise ApiError("response_lost", token_sent=True)
            if not events.get(self._sock.fileno(), 0):
                continue
            try:
                chunk = self._sock.recv(65536)
            except OSError:
                raise ApiError("response_lost", token_sent=True) from None
            if not chunk or len(buffer) + len(chunk) > _MAX_RESPONSE:
                raise ApiError("response_lost", token_sent=True)
            buffer += chunk

    def abort(self) -> None:
        """別 thread から、待っている送受信を打ち切る (fd は閉じず、持ち主の close に任せる)。"""
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        pidfd, self._pidfd = self._pidfd, -1
        if pidfd >= 0:
            os.close(pidfd)

    def __enter__(self) -> "_Request":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _connect_failure(exc: OSError) -> str:
    """connect の失敗の語。listen 前の拒否だけを待てる失敗にし、他は恒久的な失敗とする。"""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if exc.errno == errno.ECONNREFUSED:
        return "refused"
    if exc.errno == errno.ENOENT:
        return "no_socket"
    # EACCES・EPERM (socket file の権限)、ENOTSOCK 等は待っても直らない
    return "socket_unusable"


def _parse_response(buffer: bytes) -> tuple[int, bytes, int | None] | None:
    """揃っていれば (status, body, Retry-After の秒数)。まだなら None。形式不正は応答喪失。"""
    head, sep, rest = buffer.partition(b"\r\n\r\n")
    if not sep:
        if len(buffer) > 65536:
            raise ApiError("response_lost", token_sent=True)
        return None
    try:
        lines = head.decode("ascii").split("\r\n")
        version, status_text, *_ = lines[0].split(" ", 2)
        status = int(status_text)
    except (UnicodeDecodeError, ValueError):
        raise ApiError("response_lost", token_sent=True) from None
    headers = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if not colon:
            raise ApiError("response_lost", token_sent=True)
        headers[name.strip().lower()] = value.strip()
    length = headers.get("content-length")
    if (not version.startswith("HTTP/1.") or "transfer-encoding" in headers
            or length is None or re.fullmatch(r"[0-9]{1,10}", length) is None):
        raise ApiError("response_lost", token_sent=True)
    if len(rest) < int(length):
        return None
    after = headers.get("retry-after")
    seconds = int(after) if after is not None and re.fullmatch(r"[0-9]{1,6}", after) else None
    return status, rest[:int(length)], seconds


def _encode(method: str, target: str, token: str, body: bytes | None,
            actor: Actor | None) -> bytes:
    lines = [f"{method} {target} HTTP/1.1", "Host: afx", f"Authorization: Bearer {token}",
             f"Content-Length: {0 if body is None else len(body)}", "Connection: close"]
    if body is not None:
        lines.append("Content-Type: application/json")
    if actor is not None:
        # header を組む唯一の箇所。型と形式をここでも確かめる (継承や別 object で迂回させない)。
        if not isinstance(actor, Actor):
            raise ApiError("invalid_actor")
        lines.append(f"{ACTOR_HEADER}: {_check_actor_text(actor.value)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + (body or b"")


def _build(token: str, method: str, path: str, query: dict | None, body: dict | None,
           actor: Actor | None) -> bytes:
    target = path + ("?" + urlencode(query) if query else "")
    raw = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    return _encode(method, target, token, raw, actor)


def _reply(status: int, body: bytes, retry_after: int | None = None) -> Reply:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return Reply(status, None, "internal", retry_after)
    if payload.get("ok") is True:
        data = payload.get("data")
        return Reply(status, data if isinstance(data, dict) else None, None, retry_after)
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = error.get("code")
    return Reply(status, None, code if isinstance(code, str) else "internal", retry_after)


# --------------------------------------------------------------- client

def _remaining(deadline: float, *, token_sent: bool = False) -> float:
    """deadline までの残り時間。尽きていれば期限切れ。"""
    left = deadline - time.monotonic()
    if left <= 0:
        raise ApiError("timeout", token_sent=token_sent)
    return left


class _Client:
    """principal 1 つ分の client。鍵は自分の principal のものだけを持つ。"""

    principal: str = ""

    def __init__(self, endpoint: Endpoint, token: str) -> None:
        self._endpoint = endpoint
        self._token = token
        self._inflight: dict[_Request, int] = {}
        self._interrupted_threads: set[int] = set()
        self._lock = threading.Lock()
        self._interrupted = False

    @classmethod
    def from_token(cls, endpoint: Endpoint, token: bytes):
        return cls(endpoint, token.decode("ascii"))

    @classmethod
    def from_key_files(cls, endpoint: Endpoint, *, key_file: Path, ready_file: Path,
                       _for_tests: bool = True):
        """鍵を読む (隔離の前に呼ぶ)。失敗は ``KeyReadError`` の固定 code。"""
        if _for_tests is not True:
            raise ValueError("test seam disabled")
        return cls.from_token(endpoint, read_token(cls.principal, key_file=key_file,
                                                    ready_file=ready_file).encode("ascii"))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(socket={str(self._endpoint.socket_path)!r})"

    def _call(self, method: str, path: str, *, query: dict | None = None,
              body: dict | None = None, timeout: float | None = None,
              deadline: float | None = None) -> Reply:
        """actor header を付けない要求。actor を付けられるのは ``BotClient`` だけ。"""
        return self._send(_build(self._token, method, path, query, body, None), timeout=timeout,
                          deadline=deadline)

    def _send(self, payload: bytes, *, timeout: float | None = None,
              deadline: float | None = None) -> Reply:
        """1 要求を送る。``timeout`` は接続と受信それぞれの期限、``deadline`` (``time.monotonic``
        の時刻) は接続・admission・送信・受信の合計の期限で、各段は残り時間で打ち切る。ただし
        admission の lock device の検査 (socket を使わない) は上限の外で、合計は ``deadline`` に
        その所要を足した時間まで延び得る。"""
        if deadline is None:
            request_timeout = HTTP_TIMEOUT if timeout is None else timeout
        else:
            request_timeout = _remaining(deadline)
        opened = (_Request.open(self._endpoint, timeout=request_timeout) if deadline is None
                  else _Request.open(self._endpoint, timeout=request_timeout, deadline=deadline))
        with opened as request:
            with self._lock:
                ident = threading.get_ident()
                self._inflight[request] = ident
                interrupted = self._interrupted or ident in self._interrupted_threads
            try:
                if interrupted:
                    request.abort()
                if deadline is None:
                    request.send(payload)
                    return _reply(*request.receive(timeout=request_timeout))
                request.limit(_remaining(deadline))
                request.send(payload)
                return _reply(*request.receive(timeout=_remaining(deadline, token_sent=True)))
            finally:
                with self._lock:
                    self._inflight.pop(request, None)

    def interrupt(self) -> None:
        """実行中の要求の接続を切り、待っている thread を応答喪失で戻す。以後の要求も同様。"""
        with self._lock:
            self._interrupted = True
            running = list(self._inflight)
        for request in running:
            request.abort()

    def interrupt_thread(self, ident: int) -> None:
        """その thread が実行中の要求 (これから開く要求も) だけを切る。解除は ``release_thread``。"""
        with self._lock:
            self._interrupted_threads.add(ident)
            running = [request for request, owner in self._inflight.items() if owner == ident]
        for request in running:
            request.abort()

    def release_thread(self, ident: int) -> None:
        with self._lock:
            self._interrupted_threads.discard(ident)

    def _get(self, path: str, *, timeout: float | None = None, deadline: float | None = None,
             **query) -> Reply:
        return self._call("GET", path, query={k: v for k, v in query.items() if v is not None},
                          timeout=timeout, deadline=deadline)


class BotClient(_Client):
    """``ops-bot``。決定とその job の追跡は Discord 利用者の actor 付きでだけ呼べる。"""

    principal = "ops-bot"

    def events(self, *, after: int = 0, limit: int = 100) -> Reply:
        return self._get("/v1/events", after=after, limit=limit)

    def approvals(self, *, limit: int = 20) -> Reply:
        return self._get("/v1/approvals", status="pending", limit=limit)

    def approval(self, approval_id: int, *, timeout: float | None = None) -> Reply:
        return self._get(f"/v1/approvals/{_int_id(approval_id)}", timeout=timeout)

    def improve_run(self, mission_id: int, *, deadline: float) -> Reply:
        """改善 mission の終端 snapshot と詳細。``deadline`` は ``time.monotonic`` の時刻で、
        接続から本文の読込までの合計をこれで打ち切る。"""
        return self._get(f"/v1/improve/runs/{_int_id(mission_id)}", deadline=deadline)

    def approve(self, actor: Actor, approval_id: int, *, payload_sha256: str) -> "Decision":
        return self._decide(actor, "approve", approval_id, {"payload_sha256": payload_sha256})

    def reject(self, actor: Actor, approval_id: int, *, reason: str | None = None) -> "Decision":
        return self._decide(actor, "reject", approval_id,
                            {} if reason is None else {"reason": reason})

    def retry(self, actor: Actor, approval_id: int, *, payload_sha256: str) -> "Decision":
        return self._decide(actor, "retry", approval_id, {"payload_sha256": payload_sha256})

    def poll(self, job: "DecisionJob") -> Reply:
        return self._call_as(_actor_of(job), "GET", f"/v1/jobs/{job.job_id}")

    def cancel(self, job: "DecisionJob") -> Reply:
        return self._call_as(_actor_of(job), "POST", f"/v1/jobs/{job.job_id}/cancel")

    def _call_as(self, actor: Actor, method: str, path: str, *,
                 body: dict | None = None) -> Reply:
        """actor header を付ける唯一の経路。"""
        if not isinstance(actor, Actor):
            raise ApiError("invalid_actor")
        return self._send(_build(self._token, method, path, None, body, actor))

    def _decide(self, actor: Actor, kind: str, approval_id: int, body: dict) -> "Decision":
        if not isinstance(actor, Actor):
            raise ApiError("invalid_actor")
        reply = self._call_as(actor, "POST", f"/v1/approvals/{_int_id(approval_id)}/{kind}",
                              body=body)
        job_id = reply.data.get("job_id") if reply.data else None
        job = (DecisionJob(job_id, actor) if isinstance(job_id, str)
               and _JOB_ID.fullmatch(job_id) else None)
        return Decision(reply, job)


class WebClient(_Client):
    """``ops-web``。読み取りだけで、actor を付ける手段を持たない。"""

    principal = "ops-web"

    def status(self) -> Reply:
        return self._get("/v1/status")

    def approvals(self, *, limit: int = 20) -> Reply:
        return self._get("/v1/approvals", status="pending", limit=limit)

    def approval(self, approval_id: int) -> Reply:
        return self._get(f"/v1/approvals/{_int_id(approval_id)}")

    def approval_change(self, approval_id: int) -> Reply:
        """承認の変更内容 (構造化 diff と申告)。bot は呼ばない。"""
        return self._get(f"/v1/approvals/{_int_id(approval_id)}/change")

    def log(self, *, n: int = 100) -> Reply:
        return self._get("/v1/log", n=n)

    def activity(self, *, n: int = 100, category: str | None = None) -> Reply:
        return self._get("/v1/activity", n=n, category=category)


class NotifyClient(_Client):
    """``ops-notify``。event の読取りだけで、actor を付ける手段を持たない。"""

    principal = "ops-notify"

    def events(self, *, after: int = 0, limit: int = 100) -> Reply:
        return self._get("/v1/events", after=after, limit=limit)


@dataclass(frozen=True)
class DecisionJob:
    """決定が作った job。作成時の actor と束ね、追跡と取消しは同じ actor で送る。"""
    job_id: str
    actor: Actor


@dataclass(frozen=True)
class Decision:
    reply: Reply
    job: DecisionJob | None


def _actor_of(job: DecisionJob) -> Actor:
    if not isinstance(job, DecisionJob) or not isinstance(job.actor, Actor):
        raise ApiError("invalid_actor")
    if not isinstance(job.job_id, str) or _JOB_ID.fullmatch(job.job_id) is None:
        raise ValueError("job id")
    return job.actor


def _int_id(value: int) -> str:
    if type(value) is not int or value < 0:
        raise ValueError("approval id")
    return quote(str(value))


# --------------------------------------------------------------- 起動

def wait_ready(endpoint: Endpoint, *, interval: float = READINESS_INTERVAL,
               limit: float = READINESS_LIMIT,
               sleep: Callable[[float], None] = time.sleep,
               now: Callable[[], float] = time.monotonic) -> None:
    """UDS が現れ server 認証が成功するまで待つ。鍵は送らない。

    待つのは socket が無い・接続を拒否される・pidfd を一時的に作れない間だけで、
    socket を使えない (権限等) か認証に落ちたらその場で ``ApiError`` (固定 reason)。
    期限を過ぎたら ``readiness_timeout``。
    """
    deadline = now() + limit
    while True:
        remaining = deadline - now()
        if remaining <= 0:
            raise ApiError("readiness_timeout")
        try:
            with _Request.open(endpoint, timeout=min(HTTP_TIMEOUT, remaining)):
                return
        except ApiError as err:
            if err.reason not in _WAITABLE:
                raise
        if now() + interval > deadline:
            raise ApiError("readiness_timeout")
        sleep(interval)


def run_guarded(body: Callable[[], object], *, stderr: TextIO | None = None) -> int:
    """ops process の本体を呼び、要求経路・鍵の失敗を固定 reason の非零終了にする。

    process 内で再試行しない (認証し直しは systemd の restart で新 process が行う)。
    """
    out = sys.stderr if stderr is None else stderr
    try:
        body()
    except ApiError as err:
        print(f"afx-ops: {err.reason}", file=out, flush=True)
        return RC_UNREACHABLE
    except KeyReadError as err:
        print(f"afx-ops: key {err.code}", file=out, flush=True)
        return RC_KEY
    return RC_OK
