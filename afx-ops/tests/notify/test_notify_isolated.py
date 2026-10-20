"""隔離済みの実 process で notify を回す: 実 UDS の fake daemon、loopback TLS の webhook、
UDP DNS、実 state dir。

この host では 443 に本物の待受があり、非特権では 443 を bind できないので、送信は
loopback の試験 port で観測する (resolver の答えに試験 port を載せ、Landlock の connect
許可も試験 port だけにする)。443 だけが connect できることは固定 plan の canary
(test_canary の test_tcp_connect_is_limited_to_443_by_the_fixed_plan) が受け持つ。
"""
from __future__ import annotations

import errno
import re
import json
import os
import socket
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE.parent / "isolation") not in sys.path:
    sys.path.insert(0, str(HERE.parent / "isolation"))

from _afx_isolation_support import (  # noqa: E402
    Interactive,
    layout_of,
    make_root,
    needs_landlock,
    needs_x86_64,
    run_canary,
)
from _notify_fixtures import (  # noqa: E402
    HOST,
    Dns,
    EventsDaemon,
    Webhook,
    event,
    expected_body,
    make_pki,
    needs_openssl,
)
from afx_ops.notify.retry import MAX_ATTEMPTS  # noqa: E402

pytestmark = [needs_x86_64, needs_landlock, needs_openssl]

SECRET_MARK = "afx-hook-secret-4b7e"
EPERM, EACCES = errno.EPERM, errno.EACCES


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return make_pki(tmp_path_factory.mktemp("pki"))


_STARTED = {"version": 3, "cursor": 0, "high_watermark": 0, "event_cursor_initialized": True}


@pytest.fixture
def root(tmp_path) -> Path:
    root = make_root(tmp_path)
    url = root / "notify" / "secret" / "webhook.url"
    url.write_text(f"https://{HOST}/afx/{SECRET_MARK}")
    url.chmod(0o600)
    # event cursor の初期化が済んだ (cursor 0 から読む) snapshot
    (root / "notify" / "state" / "notify.snapshot").write_text(json.dumps(_STARTED))
    (root / "notify" / "state" / "notify.snapshot").chmod(0o600)
    return root


@pytest.fixture
def daemon(root, tmp_path):
    layout = layout_of(root)
    events = EventsDaemon(layout.socket_path, layout.data, tmp_path)
    yield events
    events.close()


@pytest.fixture
def servers():
    made = []

    def track(server):
        made.append(server)
        return server
    yield track
    for server in made:
        server.close()


def _webhook(servers, pki, leaf="good", **kwargs) -> Webhook:
    cert, key = pki.leaf(leaf)
    return servers(Webhook(cert, key, **kwargs))


def _spec(root, pki, hook, *, dns=None, **params) -> dict:
    params = {"ca_pem": pki.ca_pem, "port": hook.port, "connect_ports": [hook.port],
              **({"dns_port": dns.port} if dns is not None else {}), **params}
    return {"feature": "notify", "root": str(root), "op": "notify_runtime", "params": params,
            "wait": {"interval": 0.05, "limit": 10}}


def _snapshot(root) -> dict | None:
    path = root / "notify" / "state" / "notify.snapshot"
    return json.loads(path.read_text()) if path.exists() else None


def _stderr_lines(got) -> list[str]:
    return [line for line in got.stderr.splitlines() if line.startswith("afx-ops:")]


def _assert_no_secret(got) -> None:
    assert SECRET_MARK not in got.stdout + got.stderr
    assert HOST not in got.stderr


def test_isolated_child_posts_real_events_over_verified_tls(root, pki, daemon, servers,
                                                             child_env):
    hook = _webhook(servers, pki)
    dns = servers(Dns(["127.0.0.1"]))
    daemon.set([event(1), event(2)])
    got = run_canary(_spec(root, pki, hook, dns=dns), child_env)
    assert got.rc == 0, got.stderr[-3000:]
    assert got.result["rc"] == 0
    # 隔離後の送信は新しい module を読み込まない (aiohttp は preload 済み)
    assert got.result["new_modules"] == []
    sent = hook.requests
    assert [r.json() for r in sent] == [expected_body(1), expected_body(2)]
    for request in sent:
        assert request.sni == HOST
        assert request.request.startswith("POST /afx/")
        assert request.headers["content-type"] == "application/json"
        assert int(request.headers["content-length"]) == len(request.body)
        assert request.status == 200
    # 1 送信 1 接続 (使い回さない)、送信ごとに名前を引く (DNS cache を持たない)
    assert len(hook.connections) == 2
    assert dns.queries == [HOST, HOST]
    assert _snapshot(root) == {"version": 3, "cursor": 2, "high_watermark": 2,
                                "event_cursor_initialized": True}
    assert [r["after"] for r in daemon.requests()][:2] == [0, 2]
    _assert_no_secret(got)


def _set_url(root, host: str) -> None:
    (root / "notify" / "secret" / "webhook.url").write_text(f"https://{host}/afx/{SECRET_MARK}")


def test_nss_resolution_tls_and_the_notify_profile_denials_in_one_process(root, pki, daemon,
                                                                           servers, child_env,
                                                                           tmp_path):
    # aiohttp 既定の resolver (thread で getaddrinfo、NSS の files と AI_ADDRCONFIG) で引く
    _set_url(root, "localhost")
    hook = _webhook(servers, pki)
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen(1)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    daemon.set([event(1)])
    after = [
        {"op": "tcp_connect_only", "params": {"port": other.getsockname()[1]}},
        {"op": "bind", "params": {"host": "127.0.0.1"}},
        {"op": "bind", "params": {"host": "0.0.0.0"}},
        {"op": "listen", "params": {}},
        {"op": "read_file", "params": {"path": str(root / "keys" / "ops-notify.token")}},
        {"op": "read_file", "params": {"path": str(root / "notify" / "secret" / "webhook.url")}},
        {"op": "read_file", "params": {"path": str(outside)}},
        {"op": "read_file", "params": {"path": "/etc/passwd"}},
        {"op": "read_file", "params": {"path": "/etc/resolv.conf"}},
        {"op": "getaddrinfo", "params": {"host": "localhost", "addrconfig": True}},
    ]
    try:
        got = run_canary(_spec(root, pki, hook, resolver="nss", after=after), child_env)
    finally:
        other.close()
    assert got.rc == 0, got.stderr[-3000:]
    assert got.result["rc"] == 0 and got.result["new_modules"] == []
    assert [(r.sni, r.local, r.json()) for r in hook.requests] == [
        ("localhost", "127.0.0.1", expected_body(1))]
    connect, bind4, bind_any, listen, key, secret, out, passwd, resolv, gai = \
        got.result["after"]
    assert connect == [-1, EACCES]          # 許可 port 以外への TCP connect (Landlock)
    assert bind4 == bind_any == [-1, EPERM]  # bind (seccomp)
    assert listen == [-1, EPERM]
    assert key == secret == out == passwd == [-1, EACCES]  # 鍵・secret の読み直し、許可外
    assert resolv[0] > 0 and resolv[1] == 0  # dns_tls_rules の resolv.conf は読める
    assert gai[0] > 0 and gai[1] == 0
    _assert_no_secret(got)


def test_hostname_is_resolved_again_for_every_delivery(root, pki, daemon, servers, child_env):
    hook = _webhook(servers, pki, addresses=("127.0.0.1", "127.0.0.2"))
    dns = servers(Dns(["127.0.0.1", "127.0.0.2"]))
    daemon.set([event(1), event(2)])
    got = run_canary(_spec(root, pki, hook, dns=dns), child_env)
    assert got.rc == 0, got.stderr[-3000:]
    assert got.result["rc"] == 0
    assert dns.queries == [HOST, HOST]
    assert [(r.local, r.json()["event_id"]) for r in hook.requests] == [
        ("127.0.0.1", 1), ("127.0.0.2", 2)]


def test_crash_after_two_xx_resends_the_same_id_but_a_duplicate_fetch_does_not(
        root, pki, daemon, servers, child_env):
    hook = _webhook(servers, pki)
    dns = servers(Dns(["127.0.0.1"]))
    daemon.set([event(1)])
    crashed = run_canary(_spec(root, pki, hook, dns=dns, crash_after_2xx=True), child_env)
    assert crashed.rc == 86, crashed.stderr[-3000:]
    assert [r.status for r in hook.requests] == [200]
    assert _snapshot(root) == _STARTED
    # 再起動: 同じ cursor から読み直して同じ event_id をもう一度送る
    again = run_canary(_spec(root, pki, hook, dns=dns), child_env)
    assert again.result["rc"] == 0, again.stderr[-3000:]
    assert [r.json()["event_id"] for r in hook.requests] == [1, 1]
    assert _snapshot(root)["cursor"] == 1
    # daemon が cursor 以下を何度返しても (重複取得)、もう送らない
    daemon.set([event(1), event(1)], ignore_after=True)
    third = run_canary(_spec(root, pki, hook, dns=dns), child_env)
    assert third.result["rc"] == 0, third.stderr[-3000:]
    assert [r.json()["event_id"] for r in hook.requests] == [1, 1]


def test_duplicate_fetch_alone_is_delivered_once(root, pki, daemon, servers, child_env):
    hook = _webhook(servers, pki)
    dns = servers(Dns(["127.0.0.1"]))
    daemon.set([event(1), event(1), event(2)], ignore_after=True)
    got = run_canary(_spec(root, pki, hook, dns=dns), child_env)
    assert got.result["rc"] == 0, got.stderr[-3000:]
    assert [r.json()["event_id"] for r in hook.requests] == [1, 2]
    assert _snapshot(root) == {"version": 3, "cursor": 2, "high_watermark": 2,
                                "event_cursor_initialized": True}


# (webhook の leaf、status 列、期待の要求数、接続数、warning、最後の reason)
_FAILURES = {
    "redirect": ("good", [302], 1, 1, ["warning redirect"], "redirect"),
    "permanent_4xx": ("good", [404], 1, 1, ["warning http_4xx"], "http_4xx"),
    "too_many_requests": ("good", [429] * MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS,
                          ["warning http_429"], "retry_exhausted"),
    "server_error": ("good", [503] * MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS,
                     ["warning http_5xx"], "retry_exhausted"),
    "bad_certificate": ("rogue", [], 0, MAX_ATTEMPTS, ["warning tls_failed"], "retry_exhausted"),
    "hostname_mismatch": ("other-name", [], 0, MAX_ATTEMPTS, ["warning tls_failed"],
                          "retry_exhausted"),
    "timeout": ("good", ["hang"] * MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS,
                ["warning timeout"], "retry_exhausted"),
}


@pytest.mark.parametrize("case", sorted(_FAILURES))
def test_delivery_failures_keep_the_cursor_and_stop_before_the_next_event(
        case, root, pki, daemon, servers, child_env):
    leaf, statuses, requests, connections, warnings, reason = _FAILURES[case]
    hook = _webhook(servers, pki, leaf, statuses=statuses, hang=1.5)
    dns = servers(Dns(["127.0.0.1"]))
    daemon.set([event(1), event(2)])
    got = run_canary(_spec(root, pki, hook, dns=dns, timeout=0.3), child_env)
    assert got.rc == 0, got.stderr[-3000:]
    assert got.result["rc"] == 1
    assert _stderr_lines(got) == [f"afx-ops: {w}" for w in warnings] + [f"afx-ops: {reason}"]
    assert len(hook.requests) == requests
    assert len(hook.connections) == connections
    assert all(r.json()["event_id"] == 1 for r in hook.requests)
    assert _snapshot(root) == _STARTED
    _assert_no_secret(got)


def test_transient_failures_then_two_xx_commit(root, pki, daemon, servers, child_env):
    hook = _webhook(servers, pki, statuses=[503, 429, 200])
    dns = servers(Dns(["127.0.0.1"]))
    daemon.set([event(1)])
    got = run_canary(_spec(root, pki, hook, dns=dns), child_env)
    assert got.result["rc"] == 0, got.stderr[-3000:]
    assert [r.status for r in hook.requests] == [503, 429, 200]
    assert _snapshot(root)["cursor"] == 1


def test_high_watermark_regression_against_the_saved_cursor(root, pki, daemon, servers,
                                                            child_env):
    hook = _webhook(servers, pki)
    dns = servers(Dns(["127.0.0.1"]))
    daemon.set([event(1), event(2)])
    first = run_canary(_spec(root, pki, hook, dns=dns), child_env)
    assert first.result["rc"] == 0, first.stderr[-3000:]
    saved = (root / "notify" / "state" / "notify.snapshot").read_bytes()
    daemon.set([event(1)], high_watermark=1)
    got = run_canary(_spec(root, pki, hook, dns=dns), child_env)
    assert got.result["rc"] == 1
    assert _stderr_lines(got) == ["afx-ops: high_watermark_regressed"]
    assert len(hook.requests) == 2
    assert (root / "notify" / "state" / "notify.snapshot").read_bytes() == saved


def test_resolv_conf_replaced_by_rename_fails_closed(root, pki, daemon, servers, child_env,
                                                     tmp_path):
    # 実 /etc/resolv.conf と同じ形 (symlink → 実体) を一時領域に作り、規則をそこへ張らせる
    etc, run = tmp_path / "etc", tmp_path / "run"
    etc.mkdir()
    run.mkdir()
    real = run / "stub-resolv.conf"
    real.write_text("nameserver 127.0.0.1\n")
    link = etc / "resolv.conf"
    link.symlink_to(real)
    hook = _webhook(servers, pki)
    dns = servers(Dns(["127.0.0.1"]))
    daemon.set([event(1)])
    spec = _spec(root, pki, hook, dns=dns, resolv_conf=str(link), pause=True,
                 after=[{"op": "read_file", "params": {"path": str(link)}}])
    spec["resolv_conf"] = str(link)
    child = Interactive(spec, child_env)
    try:
        child.wait_line("PAUSED")
        replacement = run / "stub-resolv.conf.new"
        replacement.write_text("nameserver 127.0.0.1\n")
        os.replace(replacement, real)
        child.send()
        got = child.finish()
    finally:
        child.kill()
    assert got.rc == 0, got.stderr[-3000:]
    assert got.result["rc"] == 1
    assert _stderr_lines(got) == ["afx-ops: warning dns_failed", "afx-ops: retry_exhausted"]
    # 置き換わった inode は規則の外で読めない。成功する代替経路で送ってもいない
    assert got.result["after"] == [[-1, EACCES]]
    assert dns.queries == [] and hook.connections == []
    assert _snapshot(root) == _STARTED


def test_resolv_conf_rule_reads_the_file_while_it_is_unchanged(root, pki, daemon, servers,
                                                               child_env, tmp_path):
    # 上の試験の対照: 置換しなければ同じ規則・同じ resolver で送れる
    real = tmp_path / "stub-resolv.conf"
    real.write_text("nameserver 127.0.0.1\n")
    link = tmp_path / "resolv.conf"
    link.symlink_to(real)
    hook = _webhook(servers, pki)
    dns = servers(Dns(["127.0.0.1"]))
    daemon.set([event(1)])
    spec = _spec(root, pki, hook, dns=dns, resolv_conf=str(link))
    spec["resolv_conf"] = str(link)
    got = run_canary(spec, child_env)
    assert got.result["rc"] == 0, got.stderr[-3000:]
    assert [r.json()["event_id"] for r in hook.requests] == [1]


def _fds(lines, prefix: str) -> set[str]:
    """`prefix` で始まる socket() が返した fd。"""
    found = set()
    for line in lines:
        match = re.search(r"\b" + re.escape(prefix) + r".*\) = (\d+)$", line)
        if match:
            found.add(match.group(1))
    return found


@pytest.mark.skipif(not __import__("shutil").which("strace"), reason="strace が無い")
@pytest.mark.parametrize("resolver", ["nss", "udp"])
def test_delivery_network_syscalls_under_the_notify_profile(resolver, root, pki, daemon,
                                                            servers, child_env, tmp_path):
    # 隔離後は PR_SET_DUMPABLE=0 で strace が引数の中身を読めないので、syscall 名・fd・
    # address 長・戻り値で見る
    if resolver == "nss":
        _set_url(root, "localhost")
    hook = _webhook(servers, pki)
    dns = servers(Dns(["127.0.0.1"]))
    daemon.set([event(1)])
    trace = tmp_path / "net.trace"
    got = run_canary(_spec(root, pki, hook, dns=dns, resolver=resolver), child_env,
                     prefix=("strace", "-f", "-qq", "-e", "trace=%network", "-o", str(trace)))
    assert got.result["rc"] == 0, got.stderr[-3000:]
    assert len(hook.requests) == 1
    lines = [re.sub(r"^\d+ +", "", line) for line in trace.read_text().splitlines()]
    tcp = _fds(lines, "socket(AF_INET, SOCK_STREAM")
    netlink = _fds(lines, "socket(AF_NETLINK, SOCK_RAW|SOCK_CLOEXEC, NETLINK_ROUTE")
    udp = _fds(lines, "socket(AF_INET, SOCK_DGRAM")
    denied = [line for line in lines
              if re.search(r"= -1 E(PERM|ACCES|NOSYS|AFNOSUPPORT)\b", line)]
    # 拒否されて送信に影響しないのは、glibc が AI_ADDRCONFIG の確認で開く NETLINK socket の
    # bind だけ (seccomp の bind 拒否。glibc は「v4 / v6 とも有り」とみなして続ける)
    assert all(re.match(r"bind\((\d+),", line) and re.match(r"bind\((\d+),", line).group(1)
               in netlink and "EPERM" in line for line in denied), denied
    assert any(re.match(rf"connect\(({'|'.join(tcp)}), 0x[0-9a-f]+, 16\) += (0|-1 EINPROGRESS)",
                        line) for line in lines)
    if resolver == "nss":
        assert netlink and len(denied) == 1
    else:
        assert udp and denied == []
        assert any(re.match(rf"sendto\(({'|'.join(udp)}), 0x[0-9a-f]+, \d+, 0, 0x[0-9a-f]+, 16\)"
                            r" += \d+", line) for line in lines)
        assert any(re.match(rf"recvfrom\(({'|'.join(udp)}), 0x[0-9a-f]+, 512, 0, 0x[0-9a-f]+",
                            line) for line in lines)
