"""全 bind での Host 検査と、500 / 502 の線引き (adapter 単位)。"""
from __future__ import annotations

import pytest

from afx_ops.api_client import Reply
from afx_ops.web import render, request
from afx_ops.web.request import HostPolicy, Request, handle_request

LAN = HostPolicy("192.168.1.10", 8080, loopback=False)
LOOP = HostPolicy("127.0.0.1", 8080, loopback=True)
LAN80 = HostPolicy("192.168.1.10", 80, loopback=False)
LOOP80 = HostPolicy("127.0.0.1", 80, loopback=True)


class Daemon:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def status(self):
        self.calls.append("status")
        return Reply(200, {"mode": "paper"}, None)


def _raw(method: str, *hosts: str, path: str = "/", extra: str = "") -> bytes:
    lines = "".join(f"Host:{host}\r\n" for host in hosts)
    return f"{method} {path} HTTP/1.1\r\n{lines}{extra}\r\n".encode("ascii")


def _answer(policy, raw: bytes):
    daemon = Daemon()
    return handle_request(raw, daemon, host_policy=policy), daemon


# --- AC-7: 非 loopback ---------------------------------------------------------------

@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("host", [" 192.168.1.10:8080", "192.168.1.10:8080", "\t192.168.1.10:8080 \t",
                                  "   192.168.1.10:8080"])
def test_the_exact_bind_and_port_pass_whatever_the_outer_whitespace(method, host):
    response, daemon = _answer(LAN, _raw(method, host))
    assert response.status == 200 and daemon.calls == ["status"]
    assert (response.body == b"") is (method == "HEAD")


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(("hosts", "label"), [
    ((), "missing"),
    ((" 192.168.1.10:8080", " 192.168.1.10:8080"), "duplicate-equal"),
    ((" 192.168.1.10:8080", " 10.0.0.1:8080"), "duplicate-different"),
    ((" 192.168.1.10",), "port-omitted"),
    ((" 192.168.1.10:80",), "other-port"),
    ((" 192.168.1.10:08080",), "leading-zero-port"),
    ((" 192.168.1.10:8080:1",), "extra-port"),
    ((" 192.168.1.10:",), "empty-port"),
    ((" 192.168.1.11:8080",), "other-address"),
    ((" 127.0.0.1:8080",), "loopback-on-lan"),
    ((" localhost:8080",), "localhost-on-lan"),
    ((" example.com:8080",), "hostname"),
    ((" 192.168.1.10 :8080",), "inner-space"),
    ((" 192.168.1.10\t:8080",), "inner-tab"),
    ((" 192.168.1.010:8080",), "leading-zero-address"),
    ((" [::ffff:192.168.1.10]:8080",), "mapped-ipv6"),
    ((" user@192.168.1.10:8080",), "userinfo"),
    ((" 192.168.1.10:8080,192.168.1.10:8080",), "list"),
    ((" ",), "empty"),
])
def test_other_host_values_are_400_without_a_daemon_call(method, hosts, label):
    """M-T1-HOST-MISSING / M-T1-HOST-DUPLICATE / M-T1-HOST-MISMATCH。"""
    response, daemon = _answer(LAN, _raw(method, *hosts))
    assert response.status == 400, label
    assert daemon.calls == [], label
    assert (response.body == b"") is (method == "HEAD")
    assert response.headers["Content-Length"] != "0"


def test_port_80_is_the_only_one_that_may_be_omitted():
    for host in (" 192.168.1.10", " 192.168.1.10:80"):
        assert _answer(LAN80, _raw("GET", host))[0].status == 200
    for host in (" 192.168.1.10:8080", " 192.168.1.10:"):
        assert _answer(LAN80, _raw("GET", host))[0].status == 400
    assert _answer(LAN, _raw("GET", " 192.168.1.10"))[0].status == 400


# --- AC-8: loopback ------------------------------------------------------------------

@pytest.mark.parametrize("host", [" 127.0.0.1:8080", " localhost:8080", " LOCALHOST:8080",
                                  " LocalHost:8080"])
def test_loopback_accepts_its_address_and_a_case_insensitive_localhost(host):
    assert _answer(LOOP, _raw("GET", host))[0].status == 200


@pytest.mark.parametrize("hosts", [
    (), (" 127.0.0.1:8080", " localhost:8080"), (" 127.0.0.1",), (" localhost",),
    (" 127.0.0.2:8080",), (" localhost:80",), (" localhost.:8080",), (" local host:8080",),
    (" 127.0.0.1 :8080",), (" ::1",), (" [::1]:8080",), (" 192.168.1.10:8080",),
])
def test_loopback_rejects_everything_else(hosts):
    response, daemon = _answer(LOOP, _raw("GET", *hosts))
    assert response.status == 400 and daemon.calls == []


def test_loopback_with_a_non_default_address_does_not_accept_the_default_one():
    policy = HostPolicy("127.0.0.2", 8080, loopback=True)
    assert _answer(policy, _raw("GET", " 127.0.0.2:8080"))[0].status == 200
    assert _answer(policy, _raw("GET", " localhost:8080"))[0].status == 200
    assert _answer(policy, _raw("GET", " 127.0.0.1:8080"))[0].status == 400


def test_loopback_port_80_may_be_omitted():
    for host in (" 127.0.0.1", " localhost", " 127.0.0.1:80", " LOCALHOST:80"):
        assert _answer(LOOP80, _raw("GET", host))[0].status == 200


# --- 検査の順 -----------------------------------------------------------------------

def test_host_is_checked_before_the_route_and_the_method():
    assert _answer(LAN, _raw("GET", " 10.0.0.1:8080", path="/nowhere"))[0].status == 400
    assert _answer(LAN, _raw("GET", " 192.168.1.10:8080", path="/nowhere"))[0].status == 404
    post = "Content-Length: 0\r\n"
    assert _answer(LAN, _raw("POST", " 10.0.0.1:8080", extra=post))[0].status == 400
    response, daemon = _answer(LAN, _raw("POST", " 192.168.1.10:8080", extra=post))
    assert response.status == 405 and daemon.calls == []


def test_parse_limits_stop_the_request_before_the_host_is_looked_at():
    big = "X-Pad: " + "a" * 9000 + "\r\n"
    assert _answer(LAN, _raw("GET", " 10.0.0.1:8080", extra=big))[0].status == 413
    assert _answer(LAN, _raw("GET", " 10.0.0.1:8080", extra="Content-Length: 70000\r\n"))[0].status == 413
    assert _answer(LAN, b"GET /" + b"a" * 3000 + b" HTTP/1.1\r\nHost: x\r\n\r\n")[0].status == 413
    assert _answer(LAN, b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")[0].status == 400


def test_a_built_request_is_checked_the_same_way():
    daemon = Daemon()
    ok = Request("GET", "/", {}, (("Host", "192.168.1.10:8080"),))
    assert handle_request(ok, daemon, host_policy=LAN).status == 200
    bad = Request("GET", "/", {}, (("Host", "192.168.1.10:8080"), ("host", "192.168.1.10:8080")))
    assert handle_request(bad, daemon, host_policy=LAN).status == 400
    assert handle_request(Request("GET", "/", {}), daemon, host_policy=LAN).status == 400
    assert daemon.calls == ["status"]


def test_without_a_policy_the_value_of_host_is_not_judged():
    assert handle_request(_raw("GET", " anything"), Daemon()).status == 200


def test_the_runtime_always_hands_the_policy_to_the_adapter():
    import inspect

    from afx_ops.web import runtime
    source = inspect.getsource(runtime)
    assert "host_policy=self._host_policy" in source
    assert "HostPolicy(config.web_bind, config.port, config.web_loopback)" in source


# --- 500 / 502 の線引き (AC-22 / AC-39) -------------------------------------------------

@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_daemon_transport_failures_are_502(method):
    class Down:
        def status(self):
            raise ConnectionRefusedError("down")

    response = handle_request(Request(method, "/", {}), Down())
    assert response.status == 502


@pytest.mark.parametrize("reply", [Reply(500, None, "internal"), Reply(200, None, "E_X"),
                                   Reply(200, {"mode": "paper"}, "E_X"), Reply(404, {}, None),
                                   Reply(200, None, None)])
def test_daemon_non_200_coded_or_schema_less_replies_are_502(reply):
    class Daemon502:
        def status(self):
            return reply

    assert handle_request(Request("GET", "/", {}), Daemon502()).status == 502


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_an_unexpected_renderer_exception_is_500_not_502(method, monkeypatch):
    """M-T1-STATUS-500-502: protocol 内部例外を 502 にする変異は落ちる。"""
    def explode(data):
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(render, "render_status", explode)
    response = handle_request(Request(method, "/", {}), Daemon())
    assert response.status == 500
    assert (response.body == b"") is (method == "HEAD")


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("data", [{}, {"mode": 3}, {"mode": ["x"], "positions": None}])
def test_a_dict_reply_the_renderer_cannot_draw_is_502(method, data, monkeypatch):
    def reject(payload):
        return payload["required"]["inner"]

    monkeypatch.setattr(render, "render_status", reject)

    class Odd:
        def status(self):
            return Reply(200, data, None)

    response = handle_request(Request(method, "/", {}), Odd())
    assert response.status == 502
    assert (response.body == b"") is (method == "HEAD")


def test_an_unexpected_adapter_exception_is_500(monkeypatch):
    def explode(path):
        raise RuntimeError("router bug")

    monkeypatch.setattr(request, "resolve_route", explode)
    assert handle_request(Request("GET", "/", {}), Daemon()).status == 500


def test_a_boundary_decided_status_is_not_rewritten_as_an_internal_error():
    for raw, status in ((_raw("GET", " x", path="/nowhere"), 400),
                        (b"GET / HTTP/1.1\r\n\r\n", 400),
                        (b"GET /" + b"a" * 3000 + b" HTTP/1.1\r\nHost: x\r\n\r\n", 413)):
        assert handle_request(raw, Daemon(), host_policy=LAN).status == status
    assert handle_request(Request("GET", "/nowhere", {}), Daemon()).status == 404
    assert handle_request(Request("POST", "/", {}), Daemon()).status == 405


@pytest.mark.parametrize("status", [400, 404, 413, 500, 502])
def test_head_errors_have_no_body_and_the_same_length_as_get(status, monkeypatch):
    class Down:
        def status(self):
            raise ConnectionError("x")

    def explode(data):
        raise RuntimeError

    def build(method):
        if status == 400:
            return handle_request(Request(method, "/logs", {"x": "1"}), Daemon())
        if status == 404:
            return handle_request(Request(method, "/nowhere", {}), Daemon())
        if status == 413:
            return handle_request(Request(method, "/", {}, (("X-Pad", "y" * 9000),)), Daemon())
        if status == 502:
            return handle_request(Request(method, "/", {}), Down())
        monkeypatch.setattr(render, "render_status", explode)
        return handle_request(Request(method, "/", {}), Daemon())

    got, head = build("GET"), build("HEAD")
    assert got.status == head.status == status
    assert head.body == b"" and got.body != b""
    assert head.headers["Content-Length"] == got.headers["Content-Length"] == str(len(got.body))
