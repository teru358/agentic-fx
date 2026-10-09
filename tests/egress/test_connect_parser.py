"""CONNECT 要求 parser と 1 接続の読み関数の契約。

parser は bytes の上の状態機械なので bytes を直接与える。期限と EOF は
127.0.0.1 の一時 port の実 socket で観測する。
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from agentic_fx.egress import connect_parser as cp

OK_LINE = b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n"
OK_HOST = b"Host: api.anthropic.com:443\r\n"
OK_REQUEST = OK_LINE + OK_HOST + b"\r\n"


def run(data: bytes) -> cp.ConnectRequestParser:
    parser = cp.ConnectRequestParser()
    parser.feed(data)
    return parser


def test_constants_are_pinned():
    assert cp.MAX_LINE_BYTES == 1024
    assert cp.MAX_HEADER_LINES == 32
    assert cp.MAX_HEADER_BYTES == 8 * 1024
    assert cp.REQUEST_DEADLINE_SECONDS == 5.0
    assert cp.ALLOWED_PORT == "443"


def test_valid_request_reaches_done():
    parser = run(OK_REQUEST)
    assert parser.state is cp.State.DONE
    assert parser.reject_reason is None
    assert parser.authority == "api.anthropic.com:443"
    assert parser.hostname == "api.anthropic.com"
    assert parser.preread == b""


def _line_of(total: int) -> bytes:
    prefix = b"CONNECT a.example:443 HTTP/1.1"
    return prefix + b"x" * (total - len(prefix) - 2) + b"\r\n"


@pytest.mark.parametrize(
    "line, reason",
    [
        (b"connect api.anthropic.com:443 HTTP/1.1\r\n", cp.Reject.BAD_METHOD),
        (b"CONNECT  api.anthropic.com:443 HTTP/1.1\r\n", cp.Reject.BAD_REQUEST_LINE),
        (b"CONNECT\tapi.anthropic.com:443 HTTP/1.1\r\n", cp.Reject.CONTROL_CHAR),
        (b"CONNECT api.anthropic.com:443 HTTP/1.0\r\n", cp.Reject.BAD_VERSION),
        (b"CONNECT api.anthropic.com:443 HTTP/1.1 \r\n", cp.Reject.BAD_REQUEST_LINE),
        (b"CONNECT api.anthropic.com:443 HTTP/1.1\n", cp.Reject.BARE_LF),
        (_line_of(1025), cp.Reject.LINE_TOO_LONG),
        (b"CONNECT api.anthropic.com:443 HTTP/1.1 extra\r\n", cp.Reject.BAD_REQUEST_LINE),
        (b"CONNECT api.anthropic.com:443\r\n", cp.Reject.BAD_REQUEST_LINE),
        (b" CONNECT api.anthropic.com:443 HTTP/1.1\r\n", cp.Reject.BAD_REQUEST_LINE),
        (b"\r\n", cp.Reject.BAD_REQUEST_LINE),
    ],
)
def test_request_line_grammar(line, reason):
    parser = run(line + OK_HOST + b"\r\n")
    assert parser.state is cp.State.REJECTED
    assert parser.reject_reason is reason


def test_request_line_of_exactly_1024_bytes_is_not_a_line_length_error():
    # authority の文法で落ちるだけで、行長では落ちない
    parser = run(_line_of(1024))
    assert parser.reject_reason is not cp.Reject.LINE_TOO_LONG


def test_request_line_positive_case():
    parser = run(OK_LINE)
    assert parser.state is cp.State.HEADERS
    assert parser.reject_reason is None


@pytest.mark.parametrize(
    "authority",
    [
        b"user@host.example:443",
        b"1.2.3.4:443",
        b"[::1]:443",
        b"host.example.:443",
        b"Host.example:443",
        "hôst.example:443".encode(),
        b"xn--bcher-kva.example:443",
        b"host.example:80",
        b"host.example:0443",
        b"host.example:+443",
        b"host.example:",
        b"host.example",
        b":443",
        b"-host.example:443",
        b"host-.example:443",
        b"ho_st.example:443",
        b"host..example:443",
        (b"a" * 64) + b".example:443",
        b"host.example:443:443",
        b"1.example.123:443",
    ],
)
def test_authority_rejections(authority):
    line = b"CONNECT " + authority + b" HTTP/1.1\r\n"
    parser = run(line + b"Host: " + authority + b"\r\n\r\n")
    assert parser.state is cp.State.REJECTED
    assert parser.reject_reason is not None


@pytest.mark.parametrize(
    "authority",
    [
        b"api.anthropic.com:443",
        b"chatgpt.com:443",
        b"ab.chatgpt.com:443",
        b"localhost:443",
        b"a-b.c1.example:443",
        (b"a" * 63) + b".example:443",
    ],
)
def test_authority_acceptance(authority):
    line = b"CONNECT " + authority + b" HTTP/1.1\r\n"
    parser = run(line + b"Host: " + authority + b"\r\n\r\n")
    assert parser.state is cp.State.DONE


def _request_with_headers(headers: list[bytes]) -> bytes:
    return OK_LINE + b"".join(headers) + b"\r\n"


def _filler_lines(total_bytes: int, line_bytes: int = 1024) -> list[bytes]:
    lines: list[bytes] = []
    remaining = total_bytes
    n = 0
    while remaining > 0:
        size = min(line_bytes, remaining)
        name = b"X-F%d: " % n
        lines.append(name + b"a" * (size - len(name) - 2) + b"\r\n")
        remaining -= size
        n += 1
    return lines


@pytest.mark.parametrize(
    "headers, reason",
    [
        ([OK_HOST] + [b"X-%d: v\r\n" % i for i in range(32)], cp.Reject.TOO_MANY_HEADERS),
        ([OK_HOST, b"X-Big: " + b"a" * (1025 - 9) + b"\r\n"], cp.Reject.LINE_TOO_LONG),
        ([OK_HOST] + _filler_lines(8192 - len(OK_HOST) + 1), cp.Reject.HEADERS_TOO_LARGE),
        ([OK_HOST, b"X-A: v\n"], cp.Reject.BARE_LF),
        ([OK_HOST, b"X-A: v\r\n w\r\n"], cp.Reject.OBS_FOLD),
        ([OK_HOST, b"X-A: v\r\n\tw\r\n"], cp.Reject.OBS_FOLD),
        ([OK_HOST, b"X-A: v\x00w\r\n"], cp.Reject.NUL_BYTE),
        ([OK_HOST, b"X-A: v\x01w\r\n"], cp.Reject.CONTROL_CHAR),
        ([OK_HOST, b"X-A: v\x7fw\r\n"], cp.Reject.CONTROL_CHAR),
        ([OK_HOST, b"X-A: v\rw\r\n"], cp.Reject.CONTROL_CHAR),
        ([OK_HOST, OK_HOST], cp.Reject.DUPLICATE_HOST),
        ([OK_HOST, b"host: api.anthropic.com:443\r\n"], cp.Reject.DUPLICATE_HOST),
        (
            [OK_HOST, b"Proxy-Authorization: a\r\n", b"proxy-authorization: b\r\n"],
            cp.Reject.DUPLICATE_PROXY_AUTHORIZATION,
        ),
        ([OK_HOST, b"no colon here\r\n"], cp.Reject.BAD_HEADER),
        ([OK_HOST, b"X-A : v\r\n"], cp.Reject.BAD_HEADER),
    ],
)
def test_header_limits_and_forbidden_bytes(headers, reason):
    parser = run(_request_with_headers(headers))
    assert parser.state is cp.State.REJECTED
    assert parser.reject_reason is reason


def test_header_boundaries_are_accepted():
    # 32 行ちょうど (Host を含む)
    headers = [OK_HOST] + [b"X-%d: v\r\n" % i for i in range(31)]
    assert run(_request_with_headers(headers)).state is cp.State.DONE
    # 1 行 1,024 byte ちょうど
    big = b"X-Big: " + b"a" * (1024 - 9) + b"\r\n"
    assert len(big) == 1024
    assert run(_request_with_headers([OK_HOST, big])).state is cp.State.DONE
    # header 全体 8 KiB ちょうど
    headers = [OK_HOST] + _filler_lines(8192 - len(OK_HOST))
    assert sum(map(len, headers)) == 8192
    assert run(_request_with_headers(headers)).state is cp.State.DONE
    # tab は header 値の中では許される
    assert run(_request_with_headers([OK_HOST, b"X-A: v\tw\r\n"])).state is cp.State.DONE


@pytest.mark.parametrize(
    "host_line, reason",
    [
        (b"Host: api.anthropic.com:443\r\n", None),
        (b"host: api.anthropic.com:443\r\n", None),
        (b"Host:api.anthropic.com:443\r\n", None),
        (b"Host: api.anthropic.com\r\n", cp.Reject.HOST_MISMATCH),
        (b"Host: api.anthropic.com:444\r\n", cp.Reject.HOST_MISMATCH),
        (b"Host: API.anthropic.com:443\r\n", cp.Reject.HOST_MISMATCH),
        (b"Host:  api.anthropic.com:443\r\n", cp.Reject.HOST_MISMATCH),
        (b"Host: api.anthropic.com:443 \r\n", cp.Reject.HOST_MISMATCH),
        (b"Host: other.example:443\r\n", cp.Reject.HOST_MISMATCH),
        (b"X-A: v\r\n", cp.Reject.HOST_MISSING),
    ],
)
def test_host_header_must_equal_authority(host_line, reason):
    parser = run(OK_LINE + host_line + b"\r\n")
    if reason is None:
        assert parser.state is cp.State.DONE
    else:
        assert parser.state is cp.State.REJECTED
        assert parser.reject_reason is reason


def _split_runs(data: bytes, tail: bytes):
    """全 2 分割と 1 byte ずつ。先読みは最後の chunk に付ける。"""
    for i in range(len(data) + 1):
        yield [data[:i], data[i:] + tail] if i < len(data) else [data + tail]
    yield [data[i : i + 1] for i in range(len(data) - 1)] + [data[-1:] + tail]


def _snapshot(parser: cp.ConnectRequestParser):
    return (parser.state, parser.reject_reason, parser.authority, parser.preread)


@pytest.mark.parametrize(
    "request_bytes",
    [
        OK_REQUEST,
        b"CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com:443\r\nX-A: v\r\n\r\n",
        b"connect api.anthropic.com:443 HTTP/1.1\r\n\r\n",
        b"CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com:443\nX: 1\r\n\r\n",
        OK_LINE + OK_HOST + b"X-A: v\x00\r\n\r\n",
        OK_LINE + OK_HOST + OK_HOST + b"\r\n",
        OK_LINE + OK_HOST + b"X-A: v\r\n w\r\n\r\n",
        b"CONNECT 1.2.3.4:443 HTTP/1.1\r\nHost: 1.2.3.4:443\r\n\r\n",
        OK_LINE + b"Host: x:443\r\n\r\n",
        # 確定済み header が 8 KiB 近く、最後の行が CRLF 無しで超過する
        OK_LINE + OK_HOST + b"".join(_filler_lines(8192 - len(OK_HOST) - 100)) + b"X-L: " + b"a" * 200,
        # 確定済みがちょうど 8 KiB のところへ 1 byte でも続けば超過
        OK_LINE + OK_HOST + b"".join(_filler_lines(8192 - len(OK_HOST))) + b"X",
        # 8 KiB ちょうどの直後の終端の空行は数えない
        OK_LINE + OK_HOST + b"".join(_filler_lines(8192 - len(OK_HOST))) + b"\r\n",
    ],
)
def test_every_split_point_gives_same_result(request_bytes):
    tail = b"\x16\x03\x01\x02\x00tls-client-hello" if request_bytes is OK_REQUEST else b""
    whole = cp.ConnectRequestParser()
    whole.feed(request_bytes + tail)
    expected = _snapshot(whole)
    for chunks in _split_runs(request_bytes, tail):
        parser = cp.ConnectRequestParser()
        for chunk in chunks:
            if parser.state in (cp.State.DONE, cp.State.REJECTED):
                break
            parser.feed(chunk)
        assert _snapshot(parser) == expected, chunks[:3]


def test_unterminated_header_line_over_the_total_limit_is_rejected_at_once():
    fixed = OK_LINE + OK_HOST + b"".join(_filler_lines(8192 - len(OK_HOST) - 100))
    parser = cp.ConnectRequestParser()
    parser.feed(fixed + b"X-L: " + b"a" * 90)  # 合計 8192 以内なので待つ
    assert parser.state is cp.State.HEADERS
    parser.feed(b"a" * 10)  # 未終端のまま 8192 を超えた
    assert parser.state is cp.State.REJECTED
    assert parser.reject_reason is cp.Reject.HEADERS_TOO_LARGE


def test_bytes_after_header_end_are_kept_as_preread():
    hello = bytes(range(256)) * 3 + b"\r\n\r\n"
    parser = run(OK_REQUEST + hello)
    assert parser.state is cp.State.DONE
    assert parser.preread == hello


def test_feed_after_done_is_an_error():
    parser = run(OK_REQUEST)
    with pytest.raises(ValueError):
        parser.feed(b"x")


# --- 実 socket ---


class Listener:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]

    def accept(self) -> socket.socket:
        conn, _ = self.sock.accept()
        return conn

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def listener():
    lst = Listener()
    yield lst
    lst.close()


def _client(port: int) -> socket.socket:
    c = socket.create_connection(("127.0.0.1", port), timeout=15)
    return c


def _read_in_thread(conn, accepted_at, out):
    def target():
        out["result"] = cp.read_connect_request(conn, accepted_at=accepted_at)
        out["returned_at"] = time.monotonic()

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t


def test_read_connect_request_returns_done_with_preread(listener):
    client = _client(listener.port)
    conn = listener.accept()
    try:
        client.sendall(OK_REQUEST + b"hello-bytes")
        result = cp.read_connect_request(conn, accepted_at=time.monotonic())
        assert result.outcome is cp.Outcome.DONE
        assert result.parser.preread == b"hello-bytes"
        assert conn.fileno() != -1  # DONE では閉じない
    finally:
        conn.close()
        client.close()


def test_slow_request_is_closed_five_seconds_after_accept(listener):
    client = _client(listener.port)
    conn = listener.accept()
    accepted_at = time.monotonic()
    out: dict = {}
    t = _read_in_thread(conn, accepted_at, out)
    stop = time.monotonic() + 8
    data = OK_LINE
    received = b"unset"
    try:
        i = 0
        while time.monotonic() < stop and i < len(data):
            try:
                client.sendall(data[i : i + 1])
            except OSError:
                break
            i += 1
            time.sleep(0.5)
        client.settimeout(5)
        try:
            received = client.recv(100)
        except OSError:
            received = b"error"
        t.join(timeout=5)
    finally:
        client.close()
        conn.close()
    assert not t.is_alive()
    assert out["result"].outcome is cp.Outcome.TIMEOUT
    elapsed = out["returned_at"] - accepted_at
    assert 4.8 <= elapsed <= 5.6, elapsed
    assert received == b""
    assert conn.fileno() == -1


def test_header_flood_within_deadline_is_rejected_by_size_not_time(listener):
    client = _client(listener.port)
    conn = listener.accept()
    accepted_at = time.monotonic()
    out: dict = {}
    t = _read_in_thread(conn, accepted_at, out)
    try:
        flood = OK_LINE + OK_HOST + b"".join(_filler_lines(9 * 1024))
        try:
            client.sendall(flood)
        except OSError:
            pass
        t.join(timeout=4)
        client.settimeout(3)
        try:
            received = client.recv(100)
        except OSError:
            received = b""
    finally:
        client.close()
        conn.close()
    assert not t.is_alive()
    assert out["result"].outcome is cp.Outcome.REJECTED
    assert out["result"].parser.reject_reason in (
        cp.Reject.HEADERS_TOO_LARGE,
        cp.Reject.TOO_MANY_HEADERS,
    )
    assert out["returned_at"] - accepted_at < 2.0
    assert received == b""


def test_unterminated_oversize_header_is_rejected_without_waiting_for_the_deadline(listener):
    client = _client(listener.port)
    conn = listener.accept()
    accepted_at = time.monotonic()
    out: dict = {}
    t = _read_in_thread(conn, accepted_at, out)
    try:
        fixed = OK_LINE + OK_HOST + b"".join(_filler_lines(8192 - len(OK_HOST) - 100))
        client.sendall(fixed + b"X-L: " + b"a" * 200)  # 最後の行は CRLF 無しで止める
        t.join(timeout=3)
    finally:
        client.close()
        conn.close()
    assert not t.is_alive()
    assert out["result"].outcome is cp.Outcome.REJECTED
    assert out["result"].parser.reject_reason is cp.Reject.HEADERS_TOO_LARGE
    assert out["returned_at"] - accepted_at < 1.0


@pytest.mark.parametrize("how", ["shutdown", "close"])
def test_eof_before_done_closes_without_response(listener, how):
    client = _client(listener.port)
    conn = listener.accept()
    out: dict = {}
    t = _read_in_thread(conn, time.monotonic(), out)
    try:
        client.sendall(OK_LINE + b"Host: api.anthropic")
        if how == "shutdown":
            client.shutdown(socket.SHUT_WR)
            client.settimeout(3)
            received = client.recv(100)
        else:
            client.close()
            received = b""
        t.join(timeout=3)
    finally:
        client.close()
        conn.close()
    assert not t.is_alive()
    assert out["result"].outcome is cp.Outcome.EOF
    assert received == b""
    assert conn.fileno() == -1


def test_grammar_violation_closes_without_response(listener):
    client = _client(listener.port)
    conn = listener.accept()
    try:
        client.sendall(b"GET / HTTP/1.1\r\n\r\n")
        result = cp.read_connect_request(conn, accepted_at=time.monotonic())
        client.settimeout(3)
        received = client.recv(100)
    finally:
        client.close()
        conn.close()
    assert result.outcome is cp.Outcome.REJECTED
    assert result.parser.reject_reason is cp.Reject.BAD_METHOD
    assert received == b""
