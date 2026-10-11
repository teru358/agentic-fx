"""1 回送信 (transport) の既定値と失敗の固定語。loopback の TLS webhook に実際に送る。"""
from __future__ import annotations

import ast
import asyncio
import functools
import json
import socket
import ssl
import time
from pathlib import Path

import pytest

from _notify_fixtures import HOST, Dns, Webhook, make_pki, needs_openssl
from _notify_seams import test_ca_context, udp_resolver_factory
from afx_ops.notify import retry, transport
from afx_ops.notify.delivery import DeliveryError, validate_webhook_url

TARGET = validate_webhook_url(f"https://{HOST}/afx/transport-1c9d".encode())
BODY = json.dumps({"event_id": 1}).encode()


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return make_pki(tmp_path_factory.mktemp("pki"))


@pytest.fixture
def servers():
    made = []

    def track(server):
        made.append(server)
        return server
    yield track
    for server in made:
        server.close()


def _send(pki, hook, dns, **kwargs):
    return functools.partial(transport.post, ssl_context=test_ca_context(pki.ca_pem),
                             resolver_factory=udp_resolver_factory(dns_port=dns.port,
                                                                   port=hook.port), **kwargs)


def test_system_context_verifies_certificates_and_hostnames():
    context = transport.system_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_source_has_no_switch_that_weakens_tls_proxy_or_redirects():
    source = Path(transport.__file__).read_text(encoding="utf-8")
    for word in ("ssl=False", "verify_ssl", "CERT_NONE", "check_hostname", "trust_env=True",
                 "allow_redirects=True", "_create_unverified_context"):
        assert word not in source
    calls = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if isinstance(kw.value, ast.Constant):
                    calls[kw.arg] = kw.value.value
    assert calls["trust_env"] is False
    assert calls["allow_redirects"] is False
    assert calls["use_dns_cache"] is False and calls["force_close"] is True


@needs_openssl
def test_production_default_trusts_only_the_system_store(pki, servers):
    # 試験 CA は system の trust store に無いので、既定の context では検証に落ちる
    hook = servers(Webhook(*pki.leaf("good")))
    dns = servers(Dns(["127.0.0.1"]))
    with pytest.raises(DeliveryError) as raised:
        transport.post(TARGET, BODY, resolver_factory=udp_resolver_factory(
            dns_port=dns.port, port=hook.port))
    assert (raised.value.reason, raised.value.retryable) == ("tls_failed", True)
    assert hook.requests == []


@needs_openssl
def test_environment_proxy_is_ignored(pki, servers, monkeypatch):
    hook = servers(Webhook(*pki.leaf("good")))
    dns = servers(Dns(["127.0.0.1"]))
    proxy = socket.socket()
    proxy.bind(("127.0.0.1", 0))
    proxy.listen(4)
    proxy.settimeout(0.2)
    url = f"http://127.0.0.1:{proxy.getsockname()[1]}"
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, url)
    try:
        _send(pki, hook, dns)(TARGET, BODY)
        with pytest.raises(TimeoutError):
            proxy.accept()
    finally:
        proxy.close()
    assert [r.json() for r in hook.requests] == [{"event_id": 1}]


@needs_openssl
@pytest.mark.parametrize("status", [200, 201, 204, 299])
def test_every_two_xx_is_success(pki, servers, status):
    hook = servers(Webhook(*pki.leaf("good"), statuses=[status]))
    dns = servers(Dns(["127.0.0.1"]))
    _send(pki, hook, dns)(TARGET, BODY)
    assert [r.status for r in hook.requests] == [status]


@needs_openssl
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_every_redirect_is_a_permanent_failure_and_is_not_followed(pki, servers, status):
    hook = servers(Webhook(*pki.leaf("good"), statuses=[status]))
    dns = servers(Dns(["127.0.0.1"]))
    with pytest.raises(DeliveryError) as raised:
        _send(pki, hook, dns)(TARGET, BODY)
    assert (raised.value.reason, raised.value.retryable) == ("redirect", False)
    assert len(hook.requests) == 1 and dns.queries == [HOST]


@needs_openssl
@pytest.mark.parametrize("raw", [
    b"NOT-HTTP garbage\r\n\r\n",
    b"HTTP/1.1 abc X\r\nContent-Length: 0\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nbad header line\r\nContent-Length: 0\r\n\r\n",
])
def test_malformed_http_reply_is_a_permanent_response_invalid(pki, servers, raw):
    hook = servers(Webhook(*pki.leaf("good"), statuses=[raw]))
    dns = servers(Dns(["127.0.0.1"]))
    with pytest.raises(DeliveryError) as raised:
        _send(pki, hook, dns)(TARGET, BODY)
    assert (raised.value.reason, raised.value.retryable) == ("response_invalid", False)
    assert len(hook.requests) == 1


@needs_openssl
def test_reply_closed_without_any_bytes_is_a_retryable_connection_loss(pki, servers):
    hook = servers(Webhook(*pki.leaf("good"), statuses=[b""]))
    dns = servers(Dns(["127.0.0.1"]))
    with pytest.raises(DeliveryError) as raised:
        _send(pki, hook, dns)(TARGET, BODY)
    assert (raised.value.reason, raised.value.retryable) == ("connection_lost", True)


@needs_openssl
def test_two_xx_whose_body_is_cut_short_is_a_retryable_connection_loss(pki, servers):
    raw = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nx"
    hook = servers(Webhook(*pki.leaf("good"), statuses=[raw]))
    dns = servers(Dns(["127.0.0.1"]))
    with pytest.raises(DeliveryError) as raised:
        _send(pki, hook, dns)(TARGET, BODY)
    assert (raised.value.reason, raised.value.retryable) == ("connection_lost", True)


@needs_openssl
def test_two_xx_with_an_oversized_body_is_a_permanent_response_invalid(pki, servers):
    size = 64 * 1024 + 1
    raw = (f"HTTP/1.1 200 OK\r\nContent-Length: {size}\r\nConnection: close\r\n\r\n"
           .encode("ascii") + b"x" * size)
    hook = servers(Webhook(*pki.leaf("good"), statuses=[raw]))
    dns = servers(Dns(["127.0.0.1"]))
    with pytest.raises(DeliveryError) as raised:
        _send(pki, hook, dns)(TARGET, BODY)
    assert (raised.value.reason, raised.value.retryable) == ("response_invalid", False)


@needs_openssl
def test_two_xx_with_a_body_at_the_limit_is_success(pki, servers):
    size = 64 * 1024
    raw = (f"HTTP/1.1 200 OK\r\nContent-Length: {size}\r\nConnection: close\r\n\r\n"
           .encode("ascii") + b"x" * size)
    hook = servers(Webhook(*pki.leaf("good"), statuses=[raw]))
    dns = servers(Dns(["127.0.0.1"]))
    _send(pki, hook, dns)(TARGET, BODY)


@needs_openssl
def test_a_reply_trickled_below_the_read_timeout_still_ends_at_the_total_limit(pki, servers):
    # 1 byte ずつ返す相手は sock_read を毎回リセットする。全体の上限がないと終わらない
    hook = servers(Webhook(*pki.leaf("good"), statuses=["trickle"], hang=20.0))
    dns = servers(Dns(["127.0.0.1"]))
    started = time.monotonic()
    with pytest.raises(DeliveryError) as raised:
        _send(pki, hook, dns, timeout=1.0, total_timeout=2.0)(TARGET, BODY)
    assert (raised.value.reason, raised.value.retryable) == ("timeout", True)
    assert time.monotonic() - started < 6.0


def test_default_timeouts_bound_the_whole_exchange():
    assert retry.TOTAL_TIMEOUT == 30.0 and retry.CONNECT_READ_TIMEOUT == 10.0


@needs_openssl
def test_unresolvable_name_is_a_retryable_dns_failure(pki, servers):
    hook = servers(Webhook(*pki.leaf("good")))
    dns = servers(Dns([None]))
    with pytest.raises(DeliveryError) as raised:
        _send(pki, hook, dns)(TARGET, BODY)
    assert (raised.value.reason, raised.value.retryable) == ("dns_failed", True)
    assert hook.connections == []


@needs_openssl
def test_refused_connection_is_a_retryable_connect_failure(pki, servers):
    dns = servers(Dns(["127.0.0.1"]))
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    send = functools.partial(transport.post, ssl_context=test_ca_context(pki.ca_pem),
                             resolver_factory=udp_resolver_factory(dns_port=dns.port, port=port))
    with pytest.raises(DeliveryError) as raised:
        send(TARGET, BODY)
    assert (raised.value.reason, raised.value.retryable) == ("connect_failed", True)


def test_exception_text_never_leaves_the_transport():
    import aiohttp
    key = aiohttp.client_reqrep.ConnectionKey(HOST, 443, True, True, None, None, None)
    cases = [
        (aiohttp.ClientConnectorDNSError(key, OSError(-2, HOST)), "dns_failed", True),
        (aiohttp.ClientConnectorError(key, OSError(111, HOST)), "connect_failed", True),
        (ssl.SSLCertVerificationError(HOST), "tls_failed", True),
        (asyncio.TimeoutError(), "timeout", True),
        (aiohttp.ServerDisconnectedError(), "connection_lost", True),
        (aiohttp.ClientPayloadError(), "connection_lost", True),
        (aiohttp.ClientResponseError(None, (), status=0, message=HOST), "response_invalid",
         False),
        (aiohttp.InvalidURL(HOST), "url_invalid", False),
        (RuntimeError(HOST), "transport_failed", False),
    ]
    for exc, reason, retryable in cases:
        got = transport._classify_exception(exc)
        assert (got.reason, got.retryable, str(got)) == (reason, retryable, reason)


def test_body_must_be_bytes():
    with pytest.raises(DeliveryError) as raised:
        transport.post(TARGET, "{}")  # type: ignore[arg-type]
    assert raised.value.reason == "payload_invalid"


def test_interrupts_pass_through_the_transport_unclassified(monkeypatch):
    def interrupted(coroutine):
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(transport.asyncio, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        transport.post(TARGET, BODY)
