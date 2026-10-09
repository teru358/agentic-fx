import pytest

from agentic_fx.egress import policy


def test_connect_hosts_are_backend_constants():
    assert policy.connect_hosts("claude") == frozenset({"api.anthropic.com"})
    assert policy.connect_hosts("codex") == frozenset({"chatgpt.com", "ab.chatgpt.com"})
    assert policy.connect_hosts("opencode") == frozenset()
    others = [
        "http-intake.logs.datadoghq.com", "api.datadoghq.com", "statsig.anthropic.com",
        "events.statsigapi.net", "models.opencode.ai", "registry.npmjs.org",
        "localhost", "example.com", "chatgpt.com.evil.test", "anthropic.com",
    ]
    for backend in ("claude", "codex", "opencode"):
        for host in others:
            assert not policy.is_connect_host_allowed(backend, host)
    assert policy.is_connect_host_allowed("claude", "api.anthropic.com")
    assert not policy.is_connect_host_allowed("claude", "chatgpt.com")
    assert not policy.is_connect_host_allowed("unknown", "api.anthropic.com")


def test_egress_constants_digest_is_stable_and_sensitive():
    base = policy.egress_constants_digest()
    assert base == policy.egress_constants_digest()
    assert len(base) == 64
    hosts = {b: set(h) for b, h in policy.EGRESS_HOSTS.items()}
    hosts["claude"].add("extra.example")
    assert policy.egress_constants_digest(hosts) != base
    hosts = {b: set(h) for b, h in policy.EGRESS_HOSTS.items()}
    hosts["codex"].discard("ab.chatgpt.com")
    assert policy.egress_constants_digest(hosts) != base
    same = {b: set(h) for b, h in policy.EGRESS_HOSTS.items()}
    assert policy.egress_constants_digest(same) == base


def test_opencode_direct_target_is_loopback_8080_literal():
    assert policy.OPENCODE_DIRECT_TARGET == ("127.0.0.1", 8080)
    assert policy.LLAMA_SWAP_URL == "http://127.0.0.1:8080/v1"


def test_resolver_limits_are_fixed_constants():
    assert policy.CONNECT_PORT == 443
    assert policy.RESOLVER_MAX_THREADS == 4
    assert policy.RESOLVER_TIMEOUT_SEC == 5.0


def test_host_match_is_exact_lowercase_without_terminal_dot_or_subdomain():
    for host in (
        "API.ANTHROPIC.COM", "Api.anthropic.com", "api.anthropic.com.",
        "x.api.anthropic.com", "evil-api.anthropic.com",
    ):
        assert not policy.is_connect_host_allowed("claude", host)
    for host in ("ChatGPT.com", "chatgpt.com.", "ab.chatgpt.com.", "x.chatgpt.com", "xchatgpt.com"):
        assert not policy.is_connect_host_allowed("codex", host)


@pytest.mark.parametrize(
    "name, value",
    [
        ("OPENCODE_DIRECT_TARGET", ("127.0.0.1", 8081)),
        ("LLAMA_SWAP_URL", "http://127.0.0.1:8080/v2"),
        ("CONNECT_PORT", 444),
        ("RESOLVER_MAX_THREADS", 5),
        ("RESOLVER_TIMEOUT_SEC", 6.0),
    ],
)
def test_egress_constants_digest_changes_with_each_constant(monkeypatch, name, value):
    base = policy.egress_constants_digest()
    monkeypatch.setattr(policy, name, value)
    assert policy.egress_constants_digest() != base


def test_egress_constants_digest_ignores_input_order():
    base = policy.egress_constants_digest()
    reordered = {
        "opencode": [],
        "codex": ["ab.chatgpt.com", "chatgpt.com", "ab.chatgpt.com"],
        "claude": ["api.anthropic.com"],
    }
    assert policy.egress_constants_digest(reordered) == base
    forward = {"claude": ["api.anthropic.com"], "codex": ["chatgpt.com", "ab.chatgpt.com"], "opencode": []}
    assert policy.egress_constants_digest(forward) == base
