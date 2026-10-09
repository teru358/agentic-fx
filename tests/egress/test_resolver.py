import socket
import threading
import time

import pytest

from agentic_fx.egress import resolver as resolver_mod
from agentic_fx.egress.resolver import MissionResolver, ResolveError, is_eligible_address


@pytest.mark.parametrize(
    "addr",
    [
        "0.0.0.0", "0.255.255.255", "10.0.0.0", "10.255.255.255", "100.64.0.0", "100.127.255.255",
        "100.100.1.1", "127.0.0.1", "127.255.255.255", "169.254.0.0", "169.254.255.255",
        "172.16.0.0", "172.31.255.255", "192.0.0.1", "192.0.2.0", "192.0.2.255", "192.168.0.0",
        "192.168.255.255", "198.18.0.0", "198.19.255.255", "198.51.100.0", "198.51.100.255",
        "203.0.113.0", "203.0.113.255", "224.0.0.0", "239.255.255.255", "240.0.0.0",
        "255.255.255.255",
        "::", "::1", "fc00::", "fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "fd12::1",
        "fec0::1", "feff::1", "fe80::", "febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "ff00::", "ff02::1",
        "2001:db8::", "2001:db8:ffff:ffff:ffff:ffff:ffff:ffff", "2001:db8::1",
        "3fff::", "3fff:fff:ffff:ffff:ffff:ffff:ffff:ffff", "3fff:abc::1",
        "::ffff:127.0.0.1", "::ffff:8.8.8.8", "::127.0.0.1", "::8.8.8.8",
        "2002:7f00:1::", "2002:0808:0808::", "64:ff9b::7f00:1", "64:ff9b::808:808",
        "64:ff9b:1::1", "64:ff9b:1:ffff:ffff:ffff:ffff:ffff",
        "not-an-address", "", "8.8.8.8%eth0x", "2606:4700::1111%eth0", "2606:4700::1111%1",
    ],
)
def test_non_global_addresses_are_rejected(addr):
    assert not is_eligible_address(addr)


@pytest.mark.parametrize(
    "addr",
    [
        "100.63.255.255", "100.128.0.0", "8.8.8.8", "1.1.1.1", "11.0.0.0", "126.255.255.255",
        "128.0.0.0", "169.253.255.255", "169.255.0.0", "172.15.255.255", "172.32.0.0",
        "192.0.1.0", "192.0.3.0", "192.167.255.255", "192.169.0.0", "198.17.255.255",
        "198.20.0.0", "198.51.99.255", "198.51.101.0", "203.0.112.255", "203.0.114.0",
        "223.255.255.255",
        "2606:4700::1111", "2001:db7:ffff:ffff:ffff:ffff:ffff:ffff", "2001:db9::",
        "3ffe:ffff::1", "4000::", "fbff:ffff::1",
        "2001:4860:4860::8888", "2003::1", "2001:200::", "2001:4860::1", "2001:200:1::", "64:ff9a::1", "64:ff9b:2::1",
    ],
)
def test_global_addresses_just_outside_ranges_are_accepted(addr):
    assert is_eligible_address(addr)


@pytest.mark.parametrize(
    "addr",
    [
        "192.88.99.0", "192.88.99.2", "192.88.99.255", "255.255.255.255",
        "2001:2::", "2001:2:0:ffff:ffff:ffff:ffff:ffff", "5f00::", "5f00:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        "100:0:0:1::", "100:0:0:1:ffff:ffff:ffff:ffff",
        "2001::", "2001::1", "2001:0:ffff:ffff:ffff:ffff:ffff:ffff",
        "2001:1::1", "2001:1::2",
        "2001:3::", "2001:3:ffff:ffff:ffff:ffff:ffff:ffff",
        "2001:4:112::", "2001:4:112:ffff:ffff:ffff:ffff:ffff",
        "2001:10::", "2001:1f:ffff:ffff:ffff:ffff:ffff:ffff",
        "2001:20::", "2001:2f:ffff:ffff:ffff:ffff:ffff:ffff",
        "2001:30::", "2001:3f:ffff:ffff:ffff:ffff:ffff:ffff",
        "2001:1::3", "2001:5::1", "2001:1::", "2001:2:1::", "2001:f::", "2001:40::",
        "2001:4:111::1", "2001:4:113::1", "2001:100::", "2001:1ff:ffff:ffff:ffff:ffff:ffff:ffff",
    ],
)
def test_special_purpose_ranges_that_are_not_globally_reachable_are_rejected(addr):
    assert not is_eligible_address(addr)


def test_denied_tables_are_closed_sets_with_fixed_counts():
    assert len(resolver_mod._DENIED_V4) == 16
    assert len(resolver_mod._DENIED_V6) == 24


@pytest.fixture(autouse=True)
def _allow_example_hosts(monkeypatch):
    # resolver の挙動 (固定・期限・上限) を見るテストは任意の host 名を使うので、
    # 許可判定だけ ".example" を足して差し替える。許可判定そのものを見るテストは
    # ".example" を使わず、実物の判定を通す。
    real = resolver_mod.is_connect_host_allowed
    monkeypatch.setattr(
        resolver_mod, "is_connect_host_allowed", lambda b, h: h.endswith(".example") or real(b, h)
    )


def _mk(fn=None, **kw):
    if fn is None:
        return MissionResolver("claude", **kw)
    return MissionResolver("claude", fn, **kw)


def _join(r, timeout=5):
    for t in list(r._threads):
        t.join(timeout)


def _running(r):
    return sum(t.is_alive() for t in r._threads)


def _answer(*addrs):
    def fn(host, port, family=0, type=0, proto=0, flags=0):
        out = []
        for a in addrs:
            fam = socket.AF_INET6 if ":" in a else socket.AF_INET
            sockaddr = (a, port, 0, 0) if fam == socket.AF_INET6 else (a, port)
            out.append((fam, socket.SOCK_STREAM, 6, "", sockaddr))
        return out
    return fn


def test_one_ineligible_address_rejects_whole_set():
    r = _mk(_answer("8.8.8.8", "127.0.0.1"))
    with pytest.raises(ResolveError) as e:
        r.resolve("api.anthropic.com")
    assert e.value.status == 502
    assert r.fixed_addresses("api.anthropic.com") is None
    with pytest.raises(ResolveError) as e:
        r.resolve("api.anthropic.com")
    assert e.value.status == 502


def test_real_localhost_resolution_is_rejected(monkeypatch):
    # 実 getaddrinfo の localhost が不適格で落ちることを見る (許可判定だけ通す)
    monkeypatch.setattr(resolver_mod, "is_connect_host_allowed", lambda b, h: True)
    r = _mk()
    with pytest.raises(ResolveError) as e:
        r.resolve("localhost")
    assert e.value.status == 502


def test_first_resolution_is_fixed_for_the_mission():
    answers = [("8.8.8.8", "1.1.1.1"), ("9.9.9.9",)]
    calls = []

    def fn(host, port, family=0, type=0, proto=0, flags=0):
        calls.append((host, port, family, type))
        return _answer(*answers[len(calls) - 1])(host, port)

    r = _mk(fn)
    first = r.resolve("api.anthropic.com")
    second = r.resolve("api.anthropic.com")
    assert first == second == ("8.8.8.8", "1.1.1.1")
    assert calls == [("api.anthropic.com", 443, socket.AF_UNSPEC, socket.SOCK_STREAM)]
    assert r.resolve("second.example") == ("9.9.9.9",)
    assert len(calls) == 2


def test_duplicate_addresses_keep_first_order():
    r = _mk(_answer("8.8.8.8", "1.1.1.1", "8.8.8.8"))
    assert r.resolve("h.example") == ("8.8.8.8", "1.1.1.1")


def test_resolution_over_five_seconds_is_504_and_fixed():
    release = threading.Event()
    calls = []

    def fn(host, port, family=0, type=0, proto=0, flags=0):
        calls.append(host)
        release.wait(30)
        return _answer("8.8.8.8")(host, port)

    r = _mk(fn)
    try:
        t0 = time.monotonic()
        with pytest.raises(ResolveError) as e:
            r.resolve("slow.example")
        elapsed = time.monotonic() - t0
        assert e.value.status == 504
        assert 4.8 <= elapsed <= 6.0
        release.set()
        _join(r)
        # 遅れて返った結果は捨て、再解決もしない
        with pytest.raises(ResolveError) as e:
            r.resolve("slow.example")
        assert e.value.status == 504
        assert r.fixed_addresses("slow.example") is None
        assert calls == ["slow.example"]
    finally:
        release.set()
        _join(r)
    assert _running(r) == 0






def test_nxdomain_is_502():
    def fn(host, port, family=0, type=0, proto=0, flags=0):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    r = _mk(fn)
    with pytest.raises(ResolveError) as e:
        r.resolve("nx.example")
    assert e.value.status == 502
    with pytest.raises(ResolveError) as e:
        r.resolve("nx.example")
    assert e.value.status == 502


def test_empty_answer_is_502():
    r = _mk(lambda *a, **k: [])
    with pytest.raises(ResolveError) as e:
        r.resolve("empty.example")
    assert e.value.status == 502


@pytest.mark.parametrize(
    "addr",
    [
        "192.0.0.255", "100::", "100::ffff:ffff:ffff:ffff",
        "::255.255.255.255", "::ffff:255.255.255.255", "::ffff:128.0.0.1",
        "64:ff9b::ffff:ffff", "64:ff9b::8080:808",
        "2002:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "2002:8080:808::",
        "ff80::1", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
    ],
)
def test_upper_end_of_each_range_is_rejected(addr):
    assert not is_eligible_address(addr)


def test_scope_id_is_not_carried_into_the_fixed_set():
    r = _mk(_answer("2606:4700::1111%eth0", "8.8.8.8"))
    with pytest.raises(ResolveError) as e:
        r.resolve("h.example")
    assert e.value.status == 502
    assert r.fixed_addresses("h.example") is None
    r = _mk(_answer("fe80::1%eth0"))
    with pytest.raises(ResolveError) as e:
        r.resolve("ll.example")
    assert e.value.status == 502


def test_unexpected_resolver_exception_is_502_and_fixed():
    calls = []

    def fn(host, port, family=0, type=0, proto=0, flags=0):
        calls.append(host)
        raise RuntimeError("boom")

    r = _mk(fn)
    t0 = time.monotonic()
    with pytest.raises(ResolveError) as e:
        r.resolve("odd.example")
    assert e.value.status == 502
    assert time.monotonic() - t0 < 2.0
    with pytest.raises(ResolveError) as e:
        r.resolve("odd.example")
    assert e.value.status == 502
    assert calls == ["odd.example"]


def test_nxdomain_result_is_not_retried():
    calls = []

    def fn(host, port, family=0, type=0, proto=0, flags=0):
        calls.append(host)
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    r = _mk(fn)
    for _ in range(3):
        with pytest.raises(ResolveError):
            r.resolve("nx.example")
    assert calls == ["nx.example"]




def test_concurrent_requests_for_one_host_resolve_once():
    for round_no in range(10):
        calls = []
        lock = threading.Lock()

        def fn(host, port, family=0, type=0, proto=0, flags=0):
            with lock:
                calls.append(host)
            time.sleep(0.05)
            return _answer("8.8.8.8")(host, port)

        r = _mk(fn)
        barrier = threading.Barrier(8)
        results = []

        def ask():
            barrier.wait(5)
            results.append(r.resolve(f"same{round_no}.example"))

        threads = [threading.Thread(target=ask) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        _join(r)
        assert calls == [f"same{round_no}.example"]
        assert results == [("8.8.8.8",)] * 8


def test_unknown_authorities_never_reach_dns_or_create_threads():
    calls = []

    def fn(host, port, family=0, type=0, proto=0, flags=0):
        calls.append(host)
        return _answer("8.8.8.8")(host, port)

    r = _mk(fn)
    before = threading.active_count()
    for i in range(100):
        with pytest.raises(ResolveError) as e:
            r.resolve(f"evil{i}.test")
        assert e.value.status == 403
    assert calls == []
    assert _running(r) == 0
    assert threading.active_count() == before
    assert r.fixed_addresses("evil0.test") is None


def test_host_allowed_for_another_backend_is_refused():
    calls = []
    r = MissionResolver("codex", lambda *a, **k: calls.append(a) or _answer("8.8.8.8")("x", 443))
    with pytest.raises(ResolveError) as e:
        r.resolve("api.anthropic.com")
    assert e.value.status == 403
    assert r.resolve("chatgpt.com") == ("8.8.8.8",)
    assert len(calls) == 1


def test_opencode_backend_resolves_nothing():
    r = MissionResolver("opencode", _answer("8.8.8.8"))
    with pytest.raises(ResolveError) as e:
        r.resolve("api.anthropic.com")
    assert e.value.status == 403


def test_close_returns_by_deadline_with_a_blocked_resolution_and_refuses_later_resolves():
    release = threading.Event()
    started = threading.Event()
    calls = []

    def fn(host, port, family=0, type=0, proto=0, flags=0):
        calls.append(host)
        started.set()
        release.wait(30)
        return _answer("8.8.8.8")(host, port)

    r = _mk(fn)
    outcome = []

    def ask():
        try:
            outcome.append(r.resolve("blocked.example"))
        except ResolveError as e:
            outcome.append(e)

    t = threading.Thread(target=ask)
    t.start()
    try:
        assert started.wait(3)
        t0 = time.monotonic()
        r.close(time.monotonic() + 0.3)
        elapsed = time.monotonic() - t0
        assert 0.25 <= elapsed <= 1.0
        with pytest.raises(ResolveError) as e:
            r.resolve("other.example")
        assert e.value.status == 503
        t.join(3)
        assert not t.is_alive()  # 待っていた要求側も close で解放される
        assert isinstance(outcome[0], ResolveError)
        assert _running(r) == 1  # 解決関数は返るまで残る
    finally:
        release.set()
        _join(r)
        t.join(5)
    # 遅れて返った結果は捨てられ、固定されない
    assert r.fixed_addresses("blocked.example") is None
    assert calls == ["blocked.example"]
    assert _running(r) == 0


def test_close_with_no_workers_returns_immediately_and_is_idempotent():
    r = _mk(_answer("8.8.8.8"))
    assert r.resolve("a.example") == ("8.8.8.8",)
    _join(r)
    t0 = time.monotonic()
    r.close(time.monotonic() + 5)
    r.close(time.monotonic() + 5)
    assert time.monotonic() - t0 < 0.5
    with pytest.raises(ResolveError) as e:
        r.resolve("a.example")
    assert e.value.status == 503


def test_close_joins_running_workers_that_finish_before_the_deadline():
    def fn(host, port, family=0, type=0, proto=0, flags=0):
        time.sleep(0.2)
        return _answer("8.8.8.8")(host, port)

    r = _mk(fn)
    t = threading.Thread(target=lambda: r.resolve("quick.example"))
    t.start()
    time.sleep(0.05)
    t0 = time.monotonic()
    r.close(time.monotonic() + 3)
    assert time.monotonic() - t0 < 1.0
    assert _running(r) == 0
    t.join(3)


def test_malformed_answer_shape_is_502_not_a_timeout():
    def fn(host, port, family=0, type=0, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ())]

    r = _mk(fn)
    t0 = time.monotonic()
    with pytest.raises(ResolveError) as e:
        r.resolve("shape.example")
    assert e.value.status == 502
    assert time.monotonic() - t0 < 2.0


def test_failure_after_the_resolution_call_still_releases_the_waiter_with_502(monkeypatch):
    def boom(text):
        raise RuntimeError("post-processing failure")

    monkeypatch.setattr(resolver_mod, "is_eligible_address", boom)
    r = _mk(_answer("8.8.8.8"))
    t0 = time.monotonic()
    with pytest.raises(ResolveError) as e:
        r.resolve("post.example")
    assert e.value.status == 502
    assert time.monotonic() - t0 < 2.0
    assert r.fixed_addresses("post.example") is None


def _real_policy(monkeypatch):
    from agentic_fx.egress import policy

    monkeypatch.setattr(resolver_mod, "is_connect_host_allowed", policy.is_connect_host_allowed)


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
def test_threads_ever_created_are_bounded_by_the_allowed_hosts_under_the_real_policy(
    monkeypatch, backend
):
    from agentic_fx.egress import policy

    _real_policy(monkeypatch)
    release = threading.Event()
    running = 0
    peak = 0
    lock = threading.Lock()

    def fn(host, port, family=0, type=0, proto=0, flags=0):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        release.wait(30)
        with lock:
            running -= 1
        return _answer("8.8.8.8")(host, port)

    r = MissionResolver(backend, fn, timeout=0.2)
    hosts = sorted(policy.connect_hosts(backend)) + [f"x{i}.test" for i in range(50)]
    before = threading.active_count()
    try:
        for h in hosts * 3:
            try:
                r.resolve(h)
            except ResolveError:
                pass
        assert threading.active_count() - before <= len(policy.connect_hosts(backend))
        assert len(policy.connect_hosts(backend)) <= policy.RESOLVER_MAX_THREADS
        assert peak <= len(policy.connect_hosts(backend))
    finally:
        release.set()
        r.close(time.monotonic() + 5)


def test_resolver_refuses_a_backend_whose_allowed_hosts_exceed_the_thread_limit(monkeypatch):
    monkeypatch.setattr(resolver_mod, "RESOLVER_MAX_THREADS", 1)
    with pytest.raises(ValueError):
        MissionResolver("codex")
    MissionResolver("claude")
