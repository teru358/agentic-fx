"""adapter の認証段: 拒否の順、401 / 429 の応答、HEAD の長さ、daemon 呼出し 0、wire の Authorization。"""
from __future__ import annotations

import base64

import pytest

from afx_ops.api_client import Reply
from afx_ops.web import auth
from afx_ops.web.request import HostPolicy, Request, handle_request, wire_authorization_values

PASSWORD = b"correct-horse-battery-1"
PEER = "192.168.1.20"
HOST = HostPolicy("127.0.0.1", 80, True)
TOKEN = base64.b64encode(b"afx:" + PASSWORD).decode("ascii")
WRONG = base64.b64encode(b"afx:nope-nope-nope-nope").decode("ascii")


class Clock:
    now = 500.0

    def __call__(self):
        return self.now


class Client:
    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def status(self):
        self.calls.append("status")
        if self.fail == "upstream":
            raise ConnectionError("down")
        return Reply(200, {"mode": "paper"}, None)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def authenticator(clock):
    return auth.BasicAuthenticator(PASSWORD, clock=clock)


def wire(authorization: bytes | None, *, method: bytes = b"GET", path: bytes = b"/",
         host: bytes = b"Host: 127.0.0.1\r\n", extra: bytes = b"") -> bytes:
    line = b"" if authorization is None else b"Authorization:" + authorization + b"\r\n"
    return method + b" " + path + b" HTTP/1.1\r\n" + host + line + extra + b"\r\n"


def call(raw, client, authenticator, peer=PEER, **kwargs):
    return handle_request(raw, client, host_policy=HOST, authenticator=authenticator, peer=peer,
                          **kwargs)


OK = b" Basic " + TOKEN.encode()


# --- AC-14 / AC-15 / AC-22: 認証の結果と daemon 呼出し ------------------------------------------

def test_the_right_credential_reaches_the_route(authenticator):
    client = Client()
    response = call(wire(OK), client, authenticator)
    assert response.status == 200 and client.calls == ["status"]


def test_a_wrong_credential_is_401_with_the_challenge_and_no_daemon_call(authenticator):
    client = Client()
    response = call(wire(b" Basic " + WRONG.encode()), client, authenticator)
    assert response.status == 401
    assert response.headers["WWW-Authenticate"] == 'Basic realm="afx"'
    assert client.calls == []


@pytest.mark.parametrize("authorization", [
    b" Basic  " + TOKEN.encode(),
    b" Basic\t" + TOKEN.encode(),
    b" Basic " + TOKEN.encode() + b" ",
    b" Basic " + TOKEN.encode() + b"\t",
    b"  Basic " + TOKEN.encode(),
    b"\tBasic " + TOKEN.encode(),
    b" Basic" + TOKEN.encode(),
    b" Bearer " + TOKEN.encode(),
    b"",
])
def test_ows_variants_of_the_wire_value_are_401(authenticator, authorization):
    """M-T2-BASIC-2SP / M-T2-BASIC-TAB / M-T2-BASIC-OWS: wire の空白を正規化して受理する変異は落ちる。"""
    client = Client()
    response = call(wire(authorization), client, authenticator)
    assert response.status == 401 and client.calls == []


@pytest.mark.parametrize("authorization", [b" Basic " + TOKEN.encode(), b"Basic " + TOKEN.encode(),
                                           b" basic " + TOKEN.encode()])
def test_zero_or_one_space_after_the_colon_is_only_the_field_delimiter(authenticator, authorization):
    assert call(wire(authorization), Client(), authenticator).status == 200


def test_urlsafe_alphabet_is_401():
    """M-T2-BASIC-BASE64: URL-safe alphabet を受理する変異は落ちる。"""
    secret = b"~~~~~~~~~~~~~~~~>>>>"
    standard = base64.b64encode(b"afx:" + secret)
    urlsafe = base64.urlsafe_b64encode(b"afx:" + secret)
    assert standard != urlsafe
    a = auth.BasicAuthenticator(secret, clock=Clock())
    assert call(wire(b" Basic " + standard), Client(), a).status == 200
    assert call(wire(b" Basic " + urlsafe), Client(), a).status == 401


def test_missing_and_duplicate_authorization_are_401(authenticator):
    client = Client()
    assert call(wire(None), client, authenticator).status == 401
    doubled = wire(OK, extra=b"Authorization: Basic " + TOKEN.encode() + b"\r\n")
    assert call(doubled, client, authenticator).status == 401
    assert client.calls == []


def test_every_failure_form_has_the_same_response(authenticator):
    forms = [None, b" Basic " + WRONG.encode(), b" Bearer x", b" Basic !!!", b" Basic" + b" " * 3,
             b" Basic " + b"A" * 200]
    seen = set()
    for index, form in enumerate(forms):
        response = call(wire(form), Client(), authenticator, peer=f"10.0.0.{index}")
        seen.add((response.status, tuple(sorted(response.headers.items())), response.body))
    assert len(seen) == 1


def test_the_built_request_object_uses_its_headers(authenticator):
    request = Request("GET", "/", {}, (("Host", "127.0.0.1"), ("Authorization", "Basic " + TOKEN)))
    assert handle_request(request, Client(), host_policy=HOST, authenticator=authenticator,
                          peer=PEER).status == 200
    bare = Request("GET", "/", {}, (("Host", "127.0.0.1"),))
    assert handle_request(bare, Client(), host_policy=HOST, authenticator=authenticator,
                          peer=PEER).status == 401


def test_a_transport_supplied_wire_value_overrides_the_parsed_one(authenticator):
    got = call(wire(OK), Client(), authenticator, authorization=["Basic  " + TOKEN])
    assert got.status == 401


def test_auth_without_a_peer_fails_closed(authenticator):
    response = handle_request(wire(OK), Client(), host_policy=HOST, authenticator=authenticator)
    assert response.status == 500


def test_without_an_authenticator_authorization_is_ignored():
    client = Client()
    assert handle_request(wire(b" garbage"), client, host_policy=HOST).status == 200
    assert handle_request(wire(None), client, host_policy=HOST).status == 200


# --- 拒否の順: parse → Host → auth → route ----------------------------------------------------

def test_host_is_checked_before_auth_and_does_not_count_as_a_failure(authenticator):
    bad_host = wire(None, host=b"Host: evil.example\r\n")
    for _ in range(10):
        assert call(bad_host, Client(), authenticator).status == 400
    assert authenticator.web_auth_failed_total == 0


def test_parse_limits_come_before_auth(authenticator):
    oversized = wire(None, extra=b"X-Pad: " + b"a" * 9000 + b"\r\n")
    assert call(oversized, Client(), authenticator).status == 413
    assert call(b"BROKEN\r\n\r\n", Client(), authenticator).status == 400
    assert authenticator.web_auth_failed_total == 0


def test_auth_comes_before_route_resolution_and_method_checks(authenticator):
    client = Client()
    assert call(wire(None, path=b"/nowhere"), client, authenticator, peer="10.0.0.1").status == 401
    assert call(wire(None, method=b"POST"), client, authenticator, peer="10.0.0.2").status == 401
    assert call(wire(OK, path=b"/nowhere"), client, authenticator).status == 404
    assert call(wire(OK, method=b"POST"), client, authenticator).status == 405
    assert client.calls == []


def test_an_authenticated_daemon_failure_is_still_502(authenticator):
    assert call(wire(OK), Client("upstream"), authenticator).status == 502


# --- AC-17 / AC-20 / AC-39: 401 と 429 の本文と HEAD -------------------------------------------

def _limit(authenticator, peer=PEER):
    for _ in range(auth.SOURCE_LIMIT):
        call(wire(None), Client(), authenticator, peer=peer)


def test_429_has_retry_after_and_never_touches_the_daemon(authenticator, clock):
    client = Client()
    _limit(authenticator)
    clock.now += 10.0
    response = call(wire(OK), client, authenticator)
    assert response.status == 429
    assert response.headers["Retry-After"] == "50"
    assert "WWW-Authenticate" not in response.headers
    assert client.calls == []


def test_429_for_one_source_leaves_another_source_alone(authenticator):
    _limit(authenticator)
    assert call(wire(OK), Client(), authenticator, peer="10.5.5.5").status == 200


def test_rate_key_is_the_accepted_peer_not_a_header(authenticator):
    """M-T2-RATE-PEER-HEADER: 送信元を proxy header から導出する変異は落ちる。"""
    for index in range(auth.SOURCE_LIMIT):
        extra = f"X-Forwarded-For: 10.9.9.{index}\r\nForwarded: for=10.8.8.{index}\r\n".encode()
        call(wire(None, extra=extra), Client(), authenticator)
    response = call(wire(None, extra=b"X-Forwarded-For: 10.1.1.1\r\n"), Client(), authenticator)
    assert response.status == 429
    assert len(authenticator._sources) == 1


@pytest.mark.parametrize("status", [401, 413, 429, 500, 502])
def test_head_has_no_body_and_the_get_content_length(authenticator, status):
    """M-T2-HEAD-401 / 413 / 429 / 500 / 502: HEAD の長さを 0 にする変異は落ちる。"""
    if status == 429:
        _limit(authenticator)

    def build(method: bytes):
        if status == 401:
            return wire(b" Basic " + WRONG.encode(), method=method), Client()
        if status == 413:
            return wire(None, method=method, extra=b"X-Pad: " + b"a" * 9000 + b"\r\n"), Client()
        if status == 429:
            return wire(None, method=method), Client()
        if status == 500:
            return wire(OK, method=method), object()
        return wire(OK, method=method), Client("upstream")

    results = {}
    for method in (b"GET", b"HEAD"):
        raw, client = build(method)
        results[method] = call(raw, client, authenticator, peer="10.31.0.1" if status == 401 else PEER)
    get, head = results[b"GET"], results[b"HEAD"]
    assert get.status == head.status == status
    assert get.body != b"" and head.body == b""
    assert head.headers["Content-Length"] == get.headers["Content-Length"] == str(len(get.body))
    for name in ("WWW-Authenticate", "Retry-After"):
        assert head.headers.get(name) == get.headers.get(name)


def test_the_401_body_is_fixed_and_carries_nothing_secret(authenticator):
    first = call(wire(None), Client(), authenticator, peer="10.0.0.1")
    second = call(wire(b" Basic " + WRONG.encode()), Client(), authenticator, peer="10.0.0.2")
    assert first.body == second.body and first.body
    assert PASSWORD not in first.body and b"10.0.0" not in first.body


# --- wire から Authorization を取り出す ----------------------------------------------------------

def test_wire_values_keep_everything_after_one_delimiter_space():
    head = (b"GET / HTTP/1.1\r\nHost: x\r\nAuthorization: Basic  a \r\nauthorization:\tb\r\n"
            b"AUTHORIZATION:c\r\nX-Other: Authorization: d")
    assert wire_authorization_values(head) == ["Basic  a ", "\tb", "c"]
