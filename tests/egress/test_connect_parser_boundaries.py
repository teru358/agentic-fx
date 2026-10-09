"""CONNECT parser の境界と、拒否理由・期限起点・接続断の細部の契約。"""

from __future__ import annotations

import socket
import struct
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


def _request_for(authority: bytes) -> bytes:
    return b"CONNECT " + authority + b" HTTP/1.1\r\nHost: " + authority + b"\r\n\r\n"


# --- 改行の来ない行 ---


@pytest.mark.parametrize("bytewise", [False, True])
def test_line_without_lf_is_rejected_once_it_reaches_the_limit(bytewise):
    # LF が付けば必ず上限を超えるので、上限に達した時点で拒否する
    data = b"CONNECT " + b"a" * (cp.MAX_LINE_BYTES - 8)
    assert len(data) == cp.MAX_LINE_BYTES
    parser = cp.ConnectRequestParser()
    if bytewise:
        for i in range(len(data)):
            parser.feed(data[i : i + 1])
    else:
        parser.feed(data)
    assert parser.state is cp.State.REJECTED
    assert parser.reject_reason is cp.Reject.LINE_TOO_LONG


def test_line_without_lf_one_byte_below_the_limit_is_still_pending():
    parser = run(b"CONNECT " + b"a" * (cp.MAX_LINE_BYTES - 1 - 8))
    assert parser.state is cp.State.REQ_LINE


def test_line_of_1023_bytes_plus_one_more_byte_is_rejected_however_it_is_split():
    data = b"CONNECT " + b"a" * (cp.MAX_LINE_BYTES - 8)
    for split in (cp.MAX_LINE_BYTES - 1, 1, 500):
        parser = cp.ConnectRequestParser()
        parser.feed(data[:split])
        parser.feed(data[split:])
        assert parser.reject_reason is cp.Reject.LINE_TOO_LONG, split


def test_header_line_without_lf_beyond_the_limit_is_rejected():
    parser = run(OK_LINE + OK_HOST + b"X-A: " + b"a" * cp.MAX_LINE_BYTES)
    assert parser.state is cp.State.REJECTED
    assert parser.reject_reason is cp.Reject.LINE_TOO_LONG


# --- 制御文字 ---


def test_leading_lf_is_a_bare_lf():
    parser = run(b"\nCONNECT api.anthropic.com:443 HTTP/1.1\r")
    assert parser.reject_reason is cp.Reject.BARE_LF


@pytest.mark.parametrize("byte", [b"\x1f", b"\x0b", b"\x0c", b"\x1b"])
def test_every_c0_control_except_tab_is_rejected_in_header_values(byte):
    parser = run(OK_LINE + OK_HOST + b"X-A: v" + byte + b"w\r\n\r\n")
    assert parser.reject_reason is cp.Reject.CONTROL_CHAR


@pytest.mark.parametrize("byte", [b"\x1f", b"\x7f", b"\r"])
def test_control_bytes_are_rejected_in_the_request_line(byte):
    parser = run(b"CONNECT api.anthropic.com:443" + byte + b" HTTP/1.1\r\n" + OK_HOST + b"\r\n")
    assert parser.reject_reason is cp.Reject.CONTROL_CHAR


def test_nul_in_the_request_line_is_rejected_as_nul():
    parser = run(b"CONNECT api.anthropic.com:443\x00 HTTP/1.1\r\n" + OK_HOST + b"\r\n")
    assert parser.reject_reason is cp.Reject.NUL_BYTE


def test_empty_authority_between_single_spaces_is_a_request_line_error():
    parser = run(b"CONNECT  HTTP/1.1\r\n" + OK_HOST + b"\r\n")
    assert parser.reject_reason is cp.Reject.BAD_REQUEST_LINE


# --- authority ---


@pytest.mark.parametrize(
    "authority, reason",
    [
        (b"user@host.example:443", cp.Reject.USER_INFO),
        (b"@host.example:443", cp.Reject.USER_INFO),
        (b"[::1]:443", cp.Reject.IP_LITERAL),
        (b"[203.0.113.1]:443", cp.Reject.IP_LITERAL),
        (b"1.2.3.4:443", cp.Reject.IP_LITERAL),
        (b"host.123:443", cp.Reject.IP_LITERAL),
        (b"127.1:443", cp.Reject.IP_LITERAL),
        ("hôst.example:443".encode(), cp.Reject.NON_ASCII),
        (b"xn--bcher-kva.example:443", cp.Reject.IDN_LABEL),
        (b"www.xn--bcher-kva.example:443", cp.Reject.IDN_LABEL),
        (b"host.example", cp.Reject.BAD_PORT),
        (b"host.example:", cp.Reject.BAD_PORT),
        (b"host.example:80", cp.Reject.BAD_PORT),
        (b":443", cp.Reject.BAD_AUTHORITY),
        (b"host.example:443:443", cp.Reject.BAD_AUTHORITY),
        (b"host.exam_ple:443", cp.Reject.BAD_AUTHORITY),
        (b"host.Example:443", cp.Reject.BAD_AUTHORITY),
        (b"host.example-:443", cp.Reject.BAD_AUTHORITY),
        (b"host.-example:443", cp.Reject.BAD_AUTHORITY),
        (b"host." + b"a" * 64 + b":443", cp.Reject.BAD_AUTHORITY),
    ],
)
def test_authority_rejection_reasons(authority, reason):
    parser = run(_request_for(authority))
    assert parser.state is cp.State.REJECTED
    assert parser.reject_reason is reason


def _hostname_of(total: int) -> bytes:
    # 63 byte label を "." でつなぎ、末尾 label で長さを合わせる
    labels: list[bytes] = []
    remaining = total
    while remaining > 0:
        size = min(63, remaining)
        labels.append(b"a" * size)
        remaining -= size + 1
    host = b".".join(labels)
    return host[:total]


def test_hostname_of_253_bytes_is_accepted():
    host = _hostname_of(253)
    assert len(host) == 253 and not host.endswith(b".")
    parser = run(_request_for(host + b":443"))
    assert parser.state is cp.State.DONE
    assert parser.hostname == host.decode()


def test_hostname_of_254_bytes_is_rejected():
    host = _hostname_of(253) + b"a"
    assert len(host) == 254 and max(map(len, host.split(b"."))) <= 63
    parser = run(_request_for(host + b":443"))
    assert parser.reject_reason is cp.Reject.BAD_AUTHORITY


def test_numeric_labels_other_than_the_last_are_allowed():
    parser = run(_request_for(b"123.example.com:443"))
    assert parser.state is cp.State.DONE


# --- header ---


@pytest.mark.parametrize(
    "host_line",
    [b"Host:\tapi.anthropic.com:443\r\n", b"Host: \tapi.anthropic.com:443\r\n"],
)
def test_host_value_separator_is_a_single_space_only(host_line):
    parser = run(OK_LINE + host_line + b"\r\n")
    assert parser.reject_reason is cp.Reject.HOST_MISMATCH


def test_header_with_empty_name_is_rejected():
    parser = run(OK_LINE + OK_HOST + b": v\r\n\r\n")
    assert parser.reject_reason is cp.Reject.BAD_HEADER


# --- 拒否後の feed ---


def test_feed_after_rejection_stays_rejected():
    parser = run(b"GET / HTTP/1.1\r\n")
    assert parser.reject_reason is cp.Reject.BAD_METHOD
    assert parser.feed(OK_REQUEST) is cp.State.REJECTED
    assert parser.state is cp.State.REJECTED
    assert parser.reject_reason is cp.Reject.BAD_METHOD
    assert parser.authority is None
    assert parser.preread == b""


# --- 実 socket ---


@pytest.fixture
def pair():
    lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lst.bind(("127.0.0.1", 0))
    lst.listen(1)
    client = socket.create_connection(lst.getsockname(), timeout=15)
    conn, _ = lst.accept()
    lst.close()
    yield client, conn
    client.close()
    conn.close()


def test_deadline_counts_from_accepted_at_not_from_the_call(pair):
    client, conn = pair
    accepted_at = time.monotonic() - 3.0
    out: dict = {}

    def target():
        called = time.monotonic()
        out["result"] = cp.read_connect_request(conn, accepted_at=accepted_at)
        out["took"] = time.monotonic() - called

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=4)
    alive = t.is_alive()
    client.close()  # 戻らない実装を EOF で解放する
    t.join(timeout=3)
    assert not alive
    assert out["result"].outcome is cp.Outcome.TIMEOUT
    assert 1.8 <= out["took"] <= 2.6, out["took"]


def test_accepted_at_is_required(pair):
    _, conn = pair
    with pytest.raises(TypeError):
        cp.read_connect_request(conn)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        cp.read_connect_request(conn, accepted_at=None)  # type: ignore[arg-type]
    assert conn.fileno() != -1


def test_four_seconds_after_accept_only_about_one_second_is_left(pair):
    client, conn = pair
    accepted_at = time.monotonic() - 4.0
    started = time.monotonic()
    result = cp.read_connect_request(conn, accepted_at=accepted_at)
    took = time.monotonic() - started
    assert result.outcome is cp.Outcome.TIMEOUT
    assert 0.8 <= took <= 1.6, took
    assert conn.fileno() == -1


def test_silent_client_is_closed_five_seconds_after_accept(pair):
    client, conn = pair
    accepted_at = time.monotonic()
    out: dict = {}

    def target():
        out["result"] = cp.read_connect_request(conn, accepted_at=accepted_at)
        out["returned_at"] = time.monotonic()

    t = threading.Thread(target=target, daemon=True)
    t.start()
    time.sleep(2.0)
    client.sendall(b"C")
    t.join(timeout=4.5)
    alive = t.is_alive()
    client.close()
    t.join(timeout=3)
    assert not alive
    assert out["result"].outcome is cp.Outcome.TIMEOUT
    assert 4.8 <= out["returned_at"] - accepted_at <= 5.6


def test_connection_reset_before_done_is_eof_and_closed(pair):
    client, conn = pair
    client.sendall(OK_LINE)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    client.close()  # RST
    time.sleep(0.2)
    result = cp.read_connect_request(conn, accepted_at=time.monotonic())
    assert result.outcome is cp.Outcome.EOF
    assert conn.fileno() == -1


def test_done_restores_the_socket_timeout_it_had_before_the_call(pair):
    client, conn = pair
    assert conn.gettimeout() is None
    client.sendall(OK_REQUEST)
    result = cp.read_connect_request(conn, accepted_at=time.monotonic())
    assert result.outcome is cp.Outcome.DONE
    assert conn.gettimeout() is None


def test_done_restores_a_non_default_socket_timeout(pair):
    client, conn = pair
    conn.settimeout(7.0)
    client.sendall(OK_REQUEST)
    result = cp.read_connect_request(conn, accepted_at=time.monotonic())
    assert result.outcome is cp.Outcome.DONE
    assert conn.gettimeout() == 7.0

