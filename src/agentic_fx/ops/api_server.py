"""操作 API の UDS listener。

pathname UDS (`data/run/api.sock`、dir 0700 / socket 0600) だけで待ち受け、TCP
listener は作らない。接続ごとに本文を読む前に peer の uid と子孫関係を
``SO_PEERCRED`` / ``SO_PEERPIDFD`` で検査し、次に ``Authorization: Bearer`` を
digest の定時間比較で照合してから :class:`OpsService` へ routing する。

上限: 同時接続 (既定 4、超過は 503)、要求全体の deadline (accept から既定 5 秒、
超過は 408)、本文 (既定 64 KiB、超過は 413)、1 接続 1 要求。``Content-Length``
は POST で必須、chunked は受け付けない。内部例外は incident id だけを返し、
例外の中身は技術 log へ同じ id で残す。
"""
from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import queue
import re
import socket
import stat
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlsplit

from .contracts import (PRINCIPAL_SCOPES, ErrorCode, OpsError, Principal, Scope,
                        validate_principal_scopes)
from .keys import KeySet
from .service import ENDPOINTS

_log = logging.getLogger(__name__)

# ErrorCode に無い、HTTP 層だけで決まる失敗。
UNAUTHENTICATED = "unauthenticated"
PEER_REJECTED = "peer_rejected"

SO_PEERPIDFD = getattr(socket, "SO_PEERPIDFD", 77)
_UCRED = struct.Struct("3i")
_MAX_HEADER_BYTES = 16384
_UNIX_PATH_MAX = 107

_STATUS = {
    ErrorCode.INVALID_ARGUMENT: 400, UNAUTHENTICATED: 401, ErrorCode.FORBIDDEN: 403,
    PEER_REJECTED: 403, ErrorCode.AUTOPILOT_RESTRICTED: 403, ErrorCode.NOT_FOUND: 404,
    ErrorCode.REQUEST_TIMEOUT: 408, ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.UNAVAILABLE: 503, ErrorCode.DATABASE_BUSY: 503, ErrorCode.INTERNAL: 500,
}
_REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 408: "Request Timeout", 409: "Conflict",
            411: "Length Required", 413: "Payload Too Large",
            500: "Internal Server Error", 503: "Service Unavailable"}


def status_of(code: str) -> int:
    return _STATUS.get(code, 409)


@dataclass(frozen=True, slots=True)
class ApiLimits:
    max_connections: int = 4
    max_body_bytes: int = 65536
    request_deadline_seconds: float = 5.0


class ApiStartError(Exception):
    """listener を開けない。``code`` は固定語。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Reject(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


@dataclass(frozen=True, slots=True)
class Peer:
    pid: int | None
    uid: int | None
    # pidfd で生存中の pid を再確認できたか。できない環境では decide / local_guard を拒否する。
    pidfd_verified: bool


@dataclass(slots=True)
class Request:
    method: str
    path: str
    query: dict
    headers: dict
    body: bytes


# --------------------------------------------------------------- peer 検査

def _pidfd_pid(pidfd: int) -> int | None:
    with open(f"/proc/self/fdinfo/{pidfd}", encoding="ascii") as file:
        for line in file:
            if line.startswith("Pid:"):
                return int(line.split()[1])
    return None


def _parent_pid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as file:
            data = file.read()
    except OSError:
        return None
    # comm は空白や ')' を含み得るので、最後の ')' の後ろから数える。
    fields = data[data.rfind(b")") + 2:].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return None


def is_descendant(pid: int, ancestor: int) -> bool:
    """pid の親を辿り、ancestor に着けば True (pid 自身が ancestor のときは False)。"""
    current = pid
    for _ in range(256):
        parent = _parent_pid(current)
        if parent is None or parent <= 0:
            return False
        if parent == ancestor:
            return True
        if parent == 1:
            return False
        current = parent
    return False


def check_peer(conn: socket.socket, *, service_pid: int,
               pidfd_supported: bool = True) -> Peer:
    """本文を読む前の peer 検査。不一致は :class:`_Reject` (403 peer_rejected)。"""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    pid, uid, _gid = _UCRED.unpack(raw)
    if uid != os.getuid() or pid <= 0:
        raise _Reject(403, PEER_REJECTED)
    pidfd = None
    if pidfd_supported:
        try:
            pidfd = conn.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD)
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                raise _Reject(403, PEER_REJECTED) from None
            pidfd = None
    try:
        if pidfd is not None and _pidfd_pid(pidfd) != pid:
            raise _Reject(403, PEER_REJECTED)
        # サービス自身の子孫 (worker 等) は正しい鍵を持っていても拒否する。
        if is_descendant(pid, service_pid):
            raise _Reject(403, PEER_REJECTED)
        # 子孫検査の間に peer が入れ替わっていないかを pidfd で再確認する。
        if pidfd is not None and _pidfd_pid(pidfd) != pid:
            raise _Reject(403, PEER_REJECTED)
        return Peer(pid, uid, pidfd is not None)
    finally:
        if pidfd is not None:
            os.close(pidfd)


# --------------------------------------------------------------- 要求の読取

def _recv(conn: socket.socket, size: int, deadline: float,
          monotonic: Callable[[], float]) -> bytes:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _Reject(408, ErrorCode.REQUEST_TIMEOUT)
    conn.settimeout(remaining)
    try:
        return conn.recv(size)
    except (socket.timeout, TimeoutError):
        raise _Reject(408, ErrorCode.REQUEST_TIMEOUT) from None


def read_request(conn: socket.socket, *, deadline: float, max_body: int,
                 monotonic: Callable[[], float] = time.monotonic) -> Request:
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        if len(buffer) > _MAX_HEADER_BYTES:
            raise _Reject(413, ErrorCode.PAYLOAD_TOO_LARGE)
        chunk = _recv(conn, 4096, deadline, monotonic)
        if not chunk:
            raise _Reject(400, ErrorCode.INVALID_ARGUMENT)
        buffer += chunk
    head, _, rest = buffer.partition(b"\r\n\r\n")
    if len(head) > _MAX_HEADER_BYTES:
        raise _Reject(413, ErrorCode.PAYLOAD_TOO_LARGE)
    try:
        lines = head.decode("ascii").split("\r\n")
        method, target, version = lines[0].split(" ")
    except (UnicodeDecodeError, ValueError):
        raise _Reject(400, ErrorCode.INVALID_ARGUMENT) from None
    if not version.startswith("HTTP/1."):
        raise _Reject(400, ErrorCode.INVALID_ARGUMENT)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            raise _Reject(400, ErrorCode.INVALID_ARGUMENT)
        key = name.lower()
        if key in headers:
            raise _Reject(400, ErrorCode.INVALID_ARGUMENT)
        headers[key] = value.strip()
    if "transfer-encoding" in headers:
        raise _Reject(400, ErrorCode.INVALID_ARGUMENT)
    length_text = headers.get("content-length")
    # 本文の有無を取り違えると 1 接続 1 要求の境界が崩れるので、GET にも明示させる。
    if length_text is None:
        raise _Reject(411, ErrorCode.INVALID_ARGUMENT)
    if re.fullmatch(r"[0-9]{1,10}", length_text) is None:
        raise _Reject(400, ErrorCode.INVALID_ARGUMENT)
    length = int(length_text)
    if method == "GET" and length != 0:
        raise _Reject(400, ErrorCode.INVALID_ARGUMENT)
    if length > max_body:
        raise _Reject(413, ErrorCode.PAYLOAD_TOO_LARGE)
    body = rest
    while len(body) < length:
        chunk = _recv(conn, min(65536, length - len(body)), deadline, monotonic)
        if not chunk:
            raise _Reject(400, ErrorCode.INVALID_ARGUMENT)
        body += chunk
    if len(body) > length:
        # 1 接続 1 要求。後ろに続く bytes は受け付けない。
        raise _Reject(400, ErrorCode.INVALID_ARGUMENT)
    split = urlsplit(target)
    try:
        query = dict(parse_qsl(split.query, keep_blank_values=True, strict_parsing=False,
                               max_num_fields=8))
    except ValueError:
        raise _Reject(400, ErrorCode.INVALID_ARGUMENT) from None
    return Request(method, split.path, query, headers, body)


# --------------------------------------------------------------- routing

def _int(text: str | None, *, default: int | None = None) -> int:
    if text is None:
        if default is None:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        return default
    if re.fullmatch(r"-?[0-9]{1,18}", text) is None:
        raise OpsError(ErrorCode.INVALID_ARGUMENT)
    return int(text)


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    pattern: re.Pattern
    scope: Scope
    handler: str
    accepted: int = 200


def _route(method: str, path: str, scope: Scope, handler: str, accepted: int = 200) -> Route:
    regex = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path) + "$"
    return Route(method, re.compile(regex), scope, handler, accepted)


_ACCEPTED_202 = frozenset({"ask", "approve", "reject", "retry", "improve"})
_ENDPOINT_COUNT = 24


def build_routes(endpoints: dict) -> tuple[Route, ...]:
    """§4.2 の 24 本を endpoint 表から作る。表と route が別々に変わらないよう、
    route の定義は表だけに置き、形が崩れていれば起動時に fail closed で止める。"""
    if len(endpoints) != _ENDPOINT_COUNT:
        raise RuntimeError("endpoint table must hold exactly 24 entries")
    routes = []
    for code, endpoint in endpoints.items():
        method, _, path = endpoint.route.partition(" ")
        if method not in ("GET", "POST") or not path.startswith("/v1/") or endpoint.code != code:
            raise RuntimeError(f"malformed endpoint: {code}")
        routes.append(_route(method, path, endpoint.scope, code,
                             202 if code in _ACCEPTED_202 else 200))
    return tuple(routes)


# local_guard の 3 本は UDS listener だけで routing する。
ROUTES = build_routes(ENDPOINTS)
_PIDFD_REQUIRED = frozenset({Scope.DECIDE, Scope.LOCAL_GUARD})


def _match(method: str, path: str, transport: str) -> tuple[Route, dict] | None:
    for route in ROUTES:
        if route.scope is Scope.LOCAL_GUARD and transport != "uds":
            continue
        found = route.pattern.match(path)
        if found and route.method == method:
            return route, found.groupdict()
    return None


def _body(request: Request) -> dict:
    if not request.body:
        return {}
    try:
        value = json.loads(request.body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise OpsError(ErrorCode.INVALID_ARGUMENT) from None
    if not isinstance(value, dict):
        raise OpsError(ErrorCode.INVALID_ARGUMENT)
    return value


def _call(ops, route: Route, params: dict, request: Request, principal: Principal,
          deadline: float):
    """route を ops 関数へ写す。本文の decided_by / asserted_actor 等は読まない。"""
    q, h = request.query, request.headers
    key = h.get("idempotency-key")
    name = route.handler
    if name == "status":
        return ops.status(principal, deadline=deadline)
    if name == "log":
        return ops.log(principal, _int(q.get("n"), default=20))
    if name == "activity":
        return ops.activity(principal, _int(q.get("n"), default=20), q.get("category") or None)
    if name == "approval_list":
        if q.get("status", "pending") != "pending":
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        return ops.approval_list(principal, limit=_int(q.get("limit"), default=20),
                                 deadline=deadline)
    if name == "approval_detail":
        return ops.approval_detail(principal, _int(params["id"]), deadline=deadline)
    if name == "reflection_status":
        return ops.reflection_status(principal, _int(params["order_id"]), deadline=deadline)
    if name == "whoami":
        return ops.whoami(principal)
    if name == "job_get":
        return ops.get_job(principal, params["id"])
    if name == "events":
        limit = _int(q.get("limit"), default=100)
        if not 1 <= limit <= 500:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        return ops.events_after(principal, after=_int(q.get("after"), default=0),
                                limit=limit, deadline=deadline)
    body = _body(request)
    if name == "ask":
        return ops.ask(principal, key, body.get("question"), deadline=deadline)
    if name == "data_resume":
        return ops.resume_data(principal, acknowledge=body.get("acknowledge"),
                               deadline=deadline)
    if name in ("approve", "retry"):
        method = ops.approve if name == "approve" else ops.retry
        return method(principal, _int(params["id"]), body.get("payload_sha256"),
                      deadline=deadline)
    if name == "reject":
        return ops.reject(principal, _int(params["id"]), body.get("reason"),
                          deadline=deadline)
    if name == "killswitch_reset":
        return ops.reset_kill_switch(principal, body.get("expected_generation"),
                                     deadline=deadline)
    if name == "killswitch_reconcile":
        return ops.reconcile_kill_switch(principal, deadline=deadline)
    if name == "reflection_retry":
        if "expected_attempts" not in body or "expected_last_attempt_at" not in body:
            raise OpsError(ErrorCode.INVALID_ARGUMENT)
        return ops.retry_reflection(
            principal, key, _int(params["order_id"]),
            expected_attempts=body.get("expected_attempts"),
            expected_last_attempt_at=body.get("expected_last_attempt_at"),
            deadline=deadline)
    if name == "improve":
        return ops.improve(principal, key, deadline=deadline)
    if name == "backlog_add":
        return ops.add_backlog(principal, key, body.get("idea"), deadline=deadline)
    if name.startswith("backlog_"):
        return ops.transition_backlog(principal, _int(params["id"]), name[len("backlog_"):],
                                      deadline=deadline)
    if name == "policy":
        return ops.add_policy(principal, key, body.get("text"), deadline=deadline)
    if name == "job_cancel":
        return ops.cancel_job(principal, params["id"], deadline=deadline)
    raise OpsError(ErrorCode.NOT_FOUND)


# --------------------------------------------------------------- 拒否の通知

class _RejectionNotifier:
    """拒否を分類ごと 1 分に 1 回だけ notifier へ送る (送信は別 thread)。"""

    def __init__(self, notifier, wall_clock: Callable[[], datetime]) -> None:
        self._notifier = notifier
        self._wall_clock = wall_clock
        self._sent: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=16)
        self._thread: threading.Thread | None = None

    def record(self, category: str) -> None:
        if self._notifier is None:
            return
        minute = self._wall_clock().strftime("%Y-%m-%dT%H:%M")
        with self._lock:
            self._sent = {item for item in self._sent if item[0] == minute}
            if (minute, category) in self._sent:
                return
            self._sent.add((minute, category))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="afx-api-notify",
                                                daemon=True)
                self._thread.start()
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(f"[agentic-fx] 操作 API が要求を拒否しました: {category}")

    def _run(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                return
            try:
                self._notifier.send(text)
            except Exception:
                _log.warning("api rejection notify failed", exc_info=True)

    def close(self, timeout: float) -> None:
        with self._lock:
            thread = self._thread
        if thread is None:
            return
        with contextlib.suppress(queue.Full):
            self._queue.put(None, timeout=max(0.0, timeout))
        thread.join(max(0.0, timeout))


# --------------------------------------------------------------- server

class ApiServer:
    def __init__(self, ops, keyset: KeySet, socket_path: Path, *,
                 limits: ApiLimits = ApiLimits(),
                 monotonic: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], datetime] | None = None,
                 notifier=None, service_pid: int | None = None,
                 pidfd_supported: bool = True) -> None:
        _validate_authorization(PRINCIPAL_SCOPES)
        self._ops = ops
        self._keyset = keyset
        self.socket_path = Path(socket_path)
        self.limits = limits
        self._monotonic = monotonic
        self._service_pid = service_pid if service_pid is not None else os.getpid()
        self._pidfd_supported = pidfd_supported
        self._notify = _RejectionNotifier(
            notifier, wall_clock or (lambda: datetime.now(timezone.utc)))
        self._listener: socket.socket | None = None
        self._socket_ino: tuple[int, int] | None = None
        self._accept_thread: threading.Thread | None = None
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._active: dict[threading.Thread, socket.socket] = {}

    # ------------------------------------------------------------ 起動
    def start(self) -> None:
        self.open()
        self.serve()

    def open(self) -> None:
        """socket を bind / listen する (accept はまだしない)。"""
        path = self.socket_path
        if len(os.fsencode(str(path))) > _UNIX_PATH_MAX:
            raise ApiStartError("socket_path_too_long")
        self._prepare_dir(path.parent)
        self._remove_stale(path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        try:
            listener.bind(str(path))
            # dir が 0700 なので、chmod までの間に他 uid から届くことはない。
            os.chmod(path, 0o600)
            listener.listen(16)
            listener.settimeout(0.2)
            st = os.lstat(path)
        except OSError:
            listener.close()
            raise ApiStartError("bind_failed") from None
        self._socket_ino = (st.st_dev, st.st_ino)
        self._listener = listener

    @staticmethod
    def _prepare_dir(directory: Path) -> None:
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            st = os.lstat(directory)
        except OSError:
            raise ApiStartError("socket_dir_invalid") from None
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise ApiStartError("socket_dir_invalid")
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.chmod(directory, 0o700)

    @staticmethod
    def _remove_stale(path: Path) -> None:
        """前回の socket file だけを除去する。生きた server や通常 file は残す。"""
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
            raise ApiStartError("socket_path_occupied")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        probe.settimeout(0.5)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            pass
        except OSError:
            raise ApiStartError("socket_path_occupied") from None
        else:
            raise ApiStartError("socket_in_use")
        finally:
            probe.close()
        try:
            again = os.lstat(path)
            if (again.st_dev, again.st_ino) == (st.st_dev, st.st_ino):
                os.unlink(path)
        except FileNotFoundError:
            pass

    def serve(self) -> None:
        if self._listener is None:
            raise ApiStartError("not_open")
        self._accept_thread = threading.Thread(target=self._accept_loop,
                                               name="afx-api-accept", daemon=True)
        self._accept_thread.start()

    # ------------------------------------------------------------ 受付
    def _accept_loop(self) -> None:
        listener = self._listener
        while not self._closed.is_set():
            try:
                conn, _ = listener.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                if self._closed.is_set():
                    return
                _log.warning("api accept failed", exc_info=True)
                time.sleep(0.05)
                continue
            self.dispatch(conn, "uds")

    def dispatch(self, conn: socket.socket, transport: str) -> None:
        """接続を受け取り、上限内なら handler thread を起こす。"""
        accepted_at = self._monotonic()
        with self._lock:
            full = len(self._active) >= self.limits.max_connections or self._closed.is_set()
            if not full:
                thread = threading.Thread(target=self._handle,
                                          args=(conn, transport, accepted_at),
                                          name="afx-api-conn", daemon=True)
                self._active[thread] = conn
        if full:
            self._record_rejection("limit_rejected")
            self._respond(conn, 503, _error(ErrorCode.UNAVAILABLE), accepted_at + 0.5)
            conn.close()
            return
        thread.start()

    def _handle(self, conn: socket.socket, transport: str, accepted_at: float) -> None:
        deadline = accepted_at + self.limits.request_deadline_seconds
        try:
            status, payload = self._process(conn, transport, deadline)
            self._respond(conn, status, payload, max(deadline, self._monotonic() + 1.0))
        except Exception:
            _log.warning("api connection failed", exc_info=True)
        finally:
            with contextlib.suppress(OSError):
                conn.close()
            with self._lock:
                self._active.pop(threading.current_thread(), None)

    def _process(self, conn: socket.socket, transport: str,
                 deadline: float) -> tuple[int, dict]:
        try:
            peer = Peer(None, None, False)
            if transport == "uds":
                peer = check_peer(conn, service_pid=self._service_pid,
                                  pidfd_supported=self._pidfd_supported)
            request = read_request(conn, deadline=deadline,
                                   max_body=self.limits.max_body_bytes,
                                   monotonic=self._monotonic)
            principal = self._authenticate(request)
            found = _match(request.method, request.path, transport)
            if found is None:
                raise _Reject(404, ErrorCode.NOT_FOUND)
            route, params = found
            if route.scope in _PIDFD_REQUIRED and not peer.pidfd_verified:
                raise _Reject(403, ErrorCode.FORBIDDEN)
            data = _call(self._ops, route, params, request, principal, deadline)
            if route.method == "GET":
                _log.debug("api read %s", route.handler)
            return route.accepted, {"ok": True, "data": data}
        except _Reject as reject:
            if reject.code == UNAUTHENTICATED:
                self._record_rejection("authentication_failed")
            elif reject.code == PEER_REJECTED:
                self._record_rejection("peer_rejected")
            elif reject.status in (408, 411, 413):
                self._record_rejection("limit_rejected")
            return reject.status, _error(reject.code)
        except OpsError as err:
            return status_of(err.code), _error(err.code)
        except Exception:
            incident = uuid.uuid4().hex[:16]
            _log.error("api internal error incident=%s", incident, exc_info=True)
            payload = _error(ErrorCode.INTERNAL)
            payload["error"]["incident"] = incident
            return 500, payload

    def _authenticate(self, request: Request) -> Principal:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        principal = None
        if scheme == "Bearer" and token:
            principal = self._keyset.authenticate(token.strip())
        if principal is None:
            raise _Reject(401, UNAUTHENTICATED)
        return principal

    def _record_rejection(self, category: str) -> None:
        try:
            self._ops.record_rejection(category)
        except Exception:
            _log.warning("api rejection record failed", exc_info=True)
        self._notify.record(category)

    def _respond(self, conn: socket.socket, status: int, payload: dict,
                 deadline: float) -> None:
        try:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        except (TypeError, ValueError):
            incident = uuid.uuid4().hex[:16]
            _log.error("api response encode failed incident=%s", incident)
            status, payload = 500, _error(ErrorCode.INTERNAL)
            payload["error"]["incident"] = incident
            body = json.dumps(payload).encode()
        head = (f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
        try:
            conn.settimeout(max(0.05, deadline - self._monotonic()))
            conn.sendall(head + body)
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            return
        _discard_pending(conn)

    # ------------------------------------------------------------ 停止
    def flush_rejections(self) -> None:
        self._ops.flush_rejections()

    def close_listener(self) -> None:
        """新規受付を止める (停止順序の (1))。受付中の要求の読取も打ち切る。"""
        self._closed.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            with contextlib.suppress(OSError):
                listener.close()
        if self._socket_ino is not None:
            try:
                st = os.lstat(self.socket_path)
                if (st.st_dev, st.st_ino) == self._socket_ino:
                    os.unlink(self.socket_path)
            except OSError:
                pass
            self._socket_ino = None
        with self._lock:
            conns = list(self._active.values())
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RD)

    def join(self, timeout: float) -> bool:
        """accept thread と handler thread を bounded join する。全部抜けたら True。"""
        deadline = self._monotonic() + max(0.0, timeout)
        if self._accept_thread is not None:
            self._accept_thread.join(max(0.0, deadline - self._monotonic()))
        with self._lock:
            threads = list(self._active)
        for thread in threads:
            thread.join(max(0.0, deadline - self._monotonic()))
        self._notify.close(max(0.0, deadline - self._monotonic()))
        with self._lock:
            alive = any(t.is_alive() for t in self._active)
        return not alive and not (self._accept_thread is not None
                                  and self._accept_thread.is_alive())

    def stop(self, timeout: float = 5.0) -> bool:
        self.close_listener()
        return self.join(timeout)


def _discard_pending(conn: socket.socket, limit: int = 1 << 20) -> None:
    """読まずに残った受信 bytes を中身を見ずに捨てる。

    UDS は受信 queue に未読が残ったまま close すると peer の recv が ECONNRESET に
    なり、送った応答 (403 / 413 / 503) が届かない。解釈はしない。
    """
    try:
        conn.setblocking(False)
        dropped = 0
        while dropped < limit:
            chunk = conn.recv(65536)
            if not chunk:
                return
            dropped += len(chunk)
    except OSError:
        return


def _validate_authorization(table) -> None:
    """認可データの検査。未知 principal / scope、重複、approver 以外の local_guard で起動しない。"""
    validate_principal_scopes(table)
    for principal, scopes in table.items():
        if not isinstance(principal, Principal) or not isinstance(scopes, frozenset):
            raise ValueError("authorization table is malformed")
        if any(not isinstance(scope, Scope) for scope in scopes):
            raise ValueError("unknown scope")


def _error(code: str) -> dict:
    return {"ok": False, "error": {"code": str(code),
                                   "message": str(code).replace("_", " ")}}
