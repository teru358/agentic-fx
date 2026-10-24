"""有界 HTTP 入力を read-only DTO 表示へ写す transport 非依存 adapter。"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl

from afx_ops.web.headers import response_headers
from afx_ops.web import render
from afx_ops.web.routes import RouteMatch, resolve_route

MAX_REQUEST_LINE = 2048
MAX_HEADER_BYTES = 8192
MAX_BODY_BYTES = 65536
_METHOD = re.compile(r"[A-Z]+\Z")
_TARGET = re.compile(r"/[!-~]*\Z")
_INTEGER = re.compile(r"[1-9][0-9]{0,2}\Z")
_ACTIVITY_CATEGORIES = frozenset({"NEWS", "TECH", "AGGREGATE", "TRADE", "IMPROVE",
                                  "APPROVAL", "SYSTEM"})


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


class _Reject(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


# renderer が reply の形の不一致で落ちたときの例外型


class _Upstream(Exception):
    """daemon との通信の失敗 (transport 例外・timeout)。502 にする。"""


@dataclass(frozen=True)
class HostPolicy:
    """`Host` header の許可。全 bind で、設定の address と port に完全一致するものだけを通す。

    loopback の bind は設定 IPv4 の他に ASCII case-insensitive の `localhost` も通す。
    port は `:<設定 port>` を要求し、設定 port が 80 のときだけ省略を許す。
    """

    bind: str
    port: int
    loopback: bool

    def allows(self, values: list[str]) -> bool:
        if len(values) != 1:
            return False
        host, sep, port = values[0].partition(":")
        if sep:
            if port != str(self.port):
                return False
        elif self.port != 80:
            return False
        if host == self.bind:
            return True
        return self.loopback and host.isascii() and host.lower() == "localhost"


def _parse_target(target: str) -> tuple[str, dict[str, str]]:
    if not _TARGET.fullmatch(target) or "#" in target or "%" in target:
        raise _Reject(400)
    path, mark, raw_query = target.partition("?")
    if not path or "//" in path or "/../" in path or path.endswith("/..") or "/./" in path:
        raise _Reject(400)
    try:
        pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=bool(mark),
                          encoding="ascii", errors="strict") if mark else []
    except ValueError:
        raise _Reject(400) from None
    query: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in query or not key.isascii() or not value.isascii():
            raise _Reject(400)
        query[key] = value
    return path, query


def parse_request_line(line: bytes) -> tuple[str, str, dict[str, str]]:
    """ASCII の origin-form HTTP/1.1 request line を、path と query に分ける。

    transport 側が request line を個別に検査する際にも同じ規則を使えるよう、公開する。
    """
    if not isinstance(line, bytes) or len(line) > MAX_REQUEST_LINE:
        raise ValueError("request line")
    try:
        decoded = line.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("request line") from exc
    parts = decoded.split(" ")
    if len(parts) != 3 or not _METHOD.fullmatch(parts[0]) or parts[2] != "HTTP/1.1":
        raise ValueError("request line")
    try:
        path, query = _parse_target(parts[1])
    except _Reject as exc:
        raise ValueError("request line") from exc
    return parts[0], path, query


def parse_http_request(raw: bytes) -> Request:
    """request line / header / body の大きさと HTTP/1.1 の最小形を先に確かめる。"""
    if not isinstance(raw, bytes):
        raise _Reject(400)
    end = raw.find(b"\r\n\r\n")
    if end < 0:
        raise _Reject(413 if len(raw) > MAX_HEADER_BYTES else 400)
    head, body = raw[:end], raw[end + 4:]
    line_end = head.find(b"\r\n")
    request_line = head if line_end < 0 else head[:line_end]
    if len(request_line) > MAX_REQUEST_LINE or len(head) + 4 > MAX_HEADER_BYTES:
        raise _Reject(413)
    try:
        method, path, query = parse_request_line(request_line)
    except ValueError:
        raise _Reject(400)
    headers: list[tuple[str, str]] = []
    content_length = 0
    saw_length = False
    saw_host = False
    for raw_line in head.split(b"\r\n")[1:]:
        try:
            header = raw_line.decode("ascii")
        except UnicodeDecodeError:
            raise _Reject(400) from None
        name, sep, value = header.partition(":")
        if not sep or not name or not name.isascii() or name != name.strip() or "\r" in value:
            raise _Reject(400)
        lowered = name.lower()
        if lowered == "content-length":
            if saw_length or not value.strip().isdigit():
                raise _Reject(400)
            saw_length = True
            content_length = int(value.strip())
        elif lowered == "transfer-encoding":
            raise _Reject(400)
        elif lowered == "host":
            saw_host = True
        headers.append((name, value.strip(" \t")))
    if content_length > MAX_BODY_BYTES or len(body) > MAX_BODY_BYTES:
        raise _Reject(413)
    if not saw_host:
        raise _Reject(400)
    if content_length != len(body):
        raise _Reject(400)
    return Request(method, path, query, tuple(headers), body)


def _positive(query: dict[str, str], name: str, *, maximum: int, default: int) -> int:
    if set(query) - {name}:
        raise _Reject(400)
    value = query.get(name)
    if value is None:
        return default
    if _INTEGER.fullmatch(value) is None:
        raise _Reject(400)
    number = int(value)
    if number > maximum:
        raise _Reject(400)
    return number


def _change_of(client, approval_id: int, detail) -> dict | None:
    """承認 #N に足す変更内容。取れなくても (古い daemon、通信失敗) 本体のページは出す。"""
    if getattr(detail, "status", None) != 200 or type(getattr(detail, "data", None)) is not dict:
        return None
    fetch = getattr(client, "approval_change", None)
    if fetch is None:
        return None
    try:
        reply = fetch(approval_id)
    except Exception:  # noqa: BLE001 — 変更内容が取れないだけでページ全体を失敗させない
        return None
    if getattr(reply, "status", None) != 200 or type(getattr(reply, "data", None)) is not dict:
        return None
    return reply.data


def _list_of(value: object, element: type) -> bool:
    return type(value) is list and all(type(item) is element for item in value)


def _check_reply_shape(route: str, data: dict) -> None:
    """renderer を呼ぶ前に、reply が route ごとの期待の形かを見る。外れていれば `_Upstream` (502)。

    ここを通った reply で renderer が投げた例外は、adapter 自身の欠陥として 500 になる。
    """
    if route == "status":
        valid = type(data.get("mode", "")) is str and type(data.get("jobs", [])) is list
    elif route == "approvals":
        valid = (_list_of(data.get("approvals"), dict)
                 and ("open_journals" not in data or _list_of(data["open_journals"], dict)))
    elif route == "approval":
        valid = type(data.get("id")) is int and type(data.get("change", {})) in (dict, type(None))
    elif route == "log":
        valid = type(data.get("lines", [])) is list and type(data.get("log", [])) is list
    elif route == "activity":
        valid = type(data.get("lines", [])) is list
    else:
        valid = False
    if not valid:
        raise _Upstream


def _call(function, *args, **kwargs):
    """daemon への呼出し。transport 例外・timeout は `_Upstream` (502) にする。"""
    try:
        return function(*args, **kwargs)
    except Exception:  # noqa: BLE001 — 原因の文は外へ出さない
        raise _Upstream from None


def _invoke(match: RouteMatch, query: dict[str, str], client):
    method = match.route.client_method
    if method == "status":
        if query:
            raise _Reject(400)
        return _call(client.status), render.render_status
    if method == "approvals":
        limit = _positive(query, "limit", maximum=200, default=20)
        return _call(client.approvals, limit=limit), render.render_approvals
    if method == "approval":
        if query:
            raise _Reject(400)
        reply = _call(client.approval, match.values["id"])
        change_data = _change_of(client, match.values["id"], reply)
        return reply, functools.partial(render.render_approval, change_data=change_data)
    if method == "log":
        n = _positive(query, "n", maximum=500, default=100)
        return _call(client.log, n=n), render.render_log
    if method == "activity":
        if set(query) - {"n", "category"}:
            raise _Reject(400)
        n = _positive({key: value for key, value in query.items() if key == "n"}, "n", maximum=500, default=100)
        category = query.get("category")
        if category is not None and category not in _ACTIVITY_CATEGORIES:
            raise _Reject(400)
        return _call(client.activity, n=n, category=category), render.render_activity
    raise _Reject(404)


def _validate_request(request: Request) -> None:
    """aiohttp 等が組み立てた Request も、parser と同じ上限へ戻す。"""
    if (type(request.method) is not str or not request.method.isascii()
            or _METHOD.fullmatch(request.method) is None or type(request.path) is not str
            or not request.path.isascii() or _TARGET.fullmatch(request.path) is None
            or "?" in request.path or type(request.query) is not dict
            or any(type(key) is not str or type(value) is not str or not key.isascii()
                   or not value.isascii() for key, value in request.query.items())
            or type(request.headers) is not tuple or type(request.body) is not bytes):
        raise _Reject(400)
    if len(request.body) > MAX_BODY_BYTES:
        raise _Reject(413)
    # wire と同じ勘定: request line + CRLF、各 `name:value` + CRLF、終端の CRLF
    header_bytes = len(request.method) + len(request.path) + len(" HTTP/1.1") + 1 + 2 + 2
    if request.query:
        header_bytes += 1 + len(request.query) - 1 + sum(
            len(key) + (1 + len(value) if value else 0) for key, value in request.query.items())
    for header in request.headers:
        if (type(header) is not tuple or len(header) != 2 or type(header[0]) is not str
                or type(header[1]) is not str or not header[0].isascii()
                or not header[1].isascii() or not header[0]):
            raise _Reject(400)
        # 空白なしの `name:value` + CRLF が最小の wire 形。これより大きく数えると、wire で上限内の要求を拒む
        header_bytes += len(header[0]) + len(header[1]) + 3
    if header_bytes > MAX_HEADER_BYTES:
        raise _Reject(413)


def _response(status: int, body: bytes, *, head: bool = False, extra: dict[str, str] | None = None) -> Response:
    return Response(status, response_headers(length=len(body), extra=extra), b"" if head else body)


def _error(status: int, *, head: bool = False, extra: dict[str, str] | None = None) -> Response:
    return _response(status, render.render_error(status), head=head, extra=extra)


def _is_head(request: Request | bytes) -> bool:
    """HEAD への応答は、error でも本文を持たない。parser が拒む前の bytes は先頭の語で見る。"""
    if isinstance(request, bytes):
        return request.startswith(b"HEAD ")
    return isinstance(request, Request) and request.method == "HEAD"


def handle_request(request: Request | bytes, client, *,
                   host_policy: HostPolicy | None = None) -> Response:
    """入力から response を作る唯一の adapter。client は read-only fake でもよい。

    拒否の順は 有界 parse → Host → route → daemon。daemon との通信の失敗と期待 schema で
    ない reply は 502、この adapter 自身の想定外の例外は 500。`host_policy` が None なら
    Host の値は検査しない (試験の部品。production の runtime は必ず渡す)。
    """
    head = _is_head(request)
    try:
        if isinstance(request, bytes):
            request = parse_http_request(request)
        if not isinstance(request, Request):
            raise _Reject(400)
        _validate_request(request)
        if host_policy is not None and not host_policy.allows(
                [value for name, value in request.headers if name.lower() == "host"]):
            raise _Reject(400)
        if any(name.lower() == "x-afx-asserted-actor" for name, _value in request.headers):
            raise _Reject(400)
        match = resolve_route(request.path)
        if match is None:
            return _error(404, head=head)
        if request.method not in {"GET", "HEAD"}:
            return _error(405, head=head, extra={"Allow": "GET, HEAD"})
        if request.body:
            raise _Reject(400)
        reply, renderer = _invoke(match, request.query, client)
        if getattr(reply, "status", None) != 200 or getattr(reply, "code", None) is not None or type(getattr(reply, "data", None)) is not dict:
            return _error(502, head=head)
        _check_reply_shape(match.route.client_method, reply.data)
        body = renderer(reply.data)
        return _response(200, body, head=head)
    except _Reject as reject:
        return _error(reject.status, head=head)
    except _Upstream:
        return _error(502, head=head)
    except Exception:
        return _error(500, head=head)
