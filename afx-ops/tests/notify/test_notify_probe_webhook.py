"""`afx-ops probe notify` の本体 (`probe.main`) を loopback の TLS webhook へ向けて回す。

probe は隔離しない短命 process で、名前解決は system の resolver を使う。この host の 443 には
本物の待受があるので、試験は同じ process の中で送信の名前解決と CA だけを試験用に差し替え
(subprocess には差し替えの経路を作らない)、入力の読取り・送信・戻りは本物を通す。
"""
from __future__ import annotations

import functools
import re
from datetime import datetime

import pytest

from _notify_fixtures import HOST, Dns, Webhook, make_pki, needs_openssl
from _notify_seams import test_ca_context, udp_resolver_factory
from afx_ops import probe
from afx_ops.isolation.startup import feature_dirs
from afx_ops.notify import transport

pytestmark = needs_openssl
MARK = "probe-hook-secret-58aa"


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


def _install(home, url: str) -> None:
    dirs = feature_dirs("notify")
    dirs.config.mkdir(parents=True, mode=0o700)
    (dirs.config / "ops.toml").write_text(
        f'instance_root = "{home}/absent-root"\nkey_dir = "{home}/absent-keys"\n')
    (dirs.config / "ops.toml").chmod(0o600)
    dirs.secret.mkdir(mode=0o700)
    secret = dirs.secret / "webhook.url"
    secret.write_text(url)
    secret.chmod(0o600)


def _route(monkeypatch, pki, hook, dns, **kwargs) -> None:
    original = transport.post
    monkeypatch.setattr(transport, "post", functools.partial(
        original, ssl_context=test_ca_context(pki.ca_pem),
        resolver_factory=udp_resolver_factory(dns_port=dns.port, port=hook.port), **kwargs))


def _tree(home):
    return sorted((str(p), p.stat().st_mtime_ns) for p in home.rglob("*"))


def test_probe_sends_the_reserved_body_once_and_creates_nothing(home, pki, servers,
                                                                monkeypatch, capfd):
    _install(home, f"https://{HOST}/afx/{MARK}")
    hook = servers(Webhook(*pki.leaf("good")))
    dns = servers(Dns(["127.0.0.1"]))
    _route(monkeypatch, pki, hook, dns)
    before = _tree(home)
    assert probe.main("notify") == 0
    out, err = capfd.readouterr()
    assert (out, err) == ("", "")
    [request] = hook.requests
    assert request.sni == HOST and len(hook.connections) == 1
    body = request.json()
    assert set(body) == {"code", "probe", "nonce", "sent_at"}
    assert body["code"] == "afx.probe.notify" and body["probe"] is True
    assert re.fullmatch(r"[0-9a-f]{32}", body["nonce"])
    datetime.fromisoformat(body["sent_at"])
    # state dir も鍵 dir も作らず読まない
    assert _tree(home) == before
    assert not (home / ".local").exists()


@pytest.mark.parametrize("case, leaf, statuses, reason, requests", [
    ("bad_certificate", "rogue", [], "probe_tls_failed", 0),
    ("hostname_mismatch", "other-name", [], "probe_tls_failed", 0),
    ("redirect", "good", [302], "probe_redirect", 1),
    ("client_error", "good", [404], "probe_http_4xx", 1),
    ("server_error", "good", [503], "probe_http_5xx", 1),
    ("too_many_requests", "good", [429], "probe_http_429", 1),
    ("timeout", "good", ["hang"], "probe_timeout", 1),
])
def test_unreachable_webhook_is_rc_73_with_one_attempt(case, leaf, statuses, reason, requests,
                                                      home, pki, servers, monkeypatch, capfd):
    _install(home, f"https://{HOST}/afx/{MARK}")
    hook = servers(Webhook(*pki.leaf(leaf), statuses=statuses, hang=1.0))
    dns = servers(Dns(["127.0.0.1"]))
    _route(monkeypatch, pki, hook, dns, timeout=0.3)
    assert probe.main("notify") == 73
    out, err = capfd.readouterr()
    assert (out, err) == ("", f"afx-ops: {reason}\n")
    assert len(hook.requests) == requests
    # 再送しない
    assert len(hook.connections) == 1
    assert MARK not in err and HOST not in err
    assert not (home / ".local").exists()


@pytest.mark.parametrize("url", [f"http://{HOST}/x", f"https://{HOST}:8443/x",
                                 f"https://user:pw@{HOST}/x"])
def test_url_outside_the_contract_is_an_input_failure(url, home, pki, servers, monkeypatch,
                                                      capfd):
    _install(home, url)
    hook = servers(Webhook(*pki.leaf("good")))
    dns = servers(Dns(["127.0.0.1"]))
    _route(monkeypatch, pki, hook, dns)
    assert probe.main("notify") == 71
    assert capfd.readouterr() == ("", "afx-ops: url_invalid\n")
    assert hook.connections == [] and dns.queries == []
