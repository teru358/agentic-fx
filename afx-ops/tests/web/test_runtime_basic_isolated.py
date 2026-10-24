"""隔離済みの実 process で Basic 認証を回す: password は隔離前に読まれ、loopback の実接続で
401 / 429 / 200 を観測し、失敗した要求は daemon に届かず、秘密は出力に出ない。"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE.parent / "isolation") not in sys.path:
    sys.path.insert(0, str(HERE.parent / "isolation"))

from _afx_isolation_support import Interactive, needs_landlock, needs_x86_64  # noqa: E402
from test_runtime_isolated import (  # noqa: E402,F401
    assert_finished,
    daemon,
    exchange,
    free_port,
    get,
    parse,
    root,
    spec,
)

pytestmark = [needs_x86_64, needs_landlock]

PASSWORD = b"Isolated:pass-word-0042"
TOKEN = base64.b64encode(b"afx:" + PASSWORD)
WRONG = base64.b64encode(b"afx:definitely-wrong-1")


def install_password(root: Path, data: bytes = PASSWORD) -> Path:
    path = root / "web" / "secret" / "web.password"
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def authed(token: bytes | None, path: str = "/", method: str = "GET") -> bytes:
    extra = b"" if token is None else b"Authorization: Basic " + token + b"\r\n"
    return get(path, method, extra)


def talk(raws: list[bytes], root, port, child_env, **extra):
    web = Interactive(spec(root, port, requests=len(raws), web_auth="basic", **extra), child_env)
    try:
        web.wait_line("SERVING")
        replies = [exchange(port, raw) for raw in raws]
        done = web.finish()
    finally:
        web.kill()
    return replies, done


def test_basic_authenticates_in_the_isolated_process_and_failures_never_reach_the_daemon(
        root, daemon, child_env):
    install_password(root)
    port = free_port()
    raws = [authed(None), authed(WRONG), authed(TOKEN), authed(None, method="HEAD"),
            authed(TOKEN, "/nowhere"), authed(None, "/nowhere"), authed(TOKEN, method="POST")]
    replies, done = talk(raws, root, port, child_env)
    assert_finished(done)
    statuses = [parse(reply)[0] for reply in replies]
    assert statuses == [401, 401, 200, 401, 404, 401, 405]
    status, headers, body = parse(replies[0])
    assert headers["www-authenticate"] == 'Basic realm="afx"'
    assert replies[0].partition(b"\r\n\r\n")[2] == replies[1].partition(b"\r\n\r\n")[2]
    head_status, head_headers, head_body = parse(replies[3])
    assert head_body == b"" and head_headers["content-length"] == headers["content-length"]
    assert [r["target"] for r in daemon.requests()] == ["/v1/status"]
    assert done.stdout.count(PASSWORD.decode()) == 0
    for text in (done.stdout, done.stderr):
        assert PASSWORD.decode() not in text and TOKEN.decode() not in text
    # Authorization が無い要求は 401 だが失敗には数えない。数えるのは WRONG の 1 本だけ
    assert done.stderr.splitlines() == ["afx-ops: web_auth_failed"]


def test_a_configured_user_authenticates_and_the_default_afx_does_not(root, daemon, child_env):
    install_password(root)
    port = free_port()
    custom = base64.b64encode(b"ops-web:" + PASSWORD)
    raws = [authed(custom), authed(TOKEN), authed(base64.b64encode(b"ops-web:" + PASSWORD + b"x"))]
    replies, done = talk(raws, root, port, child_env, web_user="ops-web")
    assert_finished(done)
    assert [parse(reply)[0] for reply in replies] == [200, 401, 401]
    assert [r["target"] for r in daemon.requests()] == ["/v1/status"]
    assert done.stderr.splitlines() == ["afx-ops: web_auth_failed"] * 2


def test_the_sixth_failure_is_429_and_a_correct_password_is_refused_while_limited(
        root, daemon, child_env):
    install_password(root)
    port = free_port()
    raws = [authed(WRONG)] * 5 + [authed(TOKEN), authed(TOKEN, method="HEAD")]
    replies, done = talk(raws, root, port, child_env)
    assert_finished(done)
    assert [parse(reply)[0] for reply in replies] == [401] * 5 + [429, 429]
    _status, headers, body = parse(replies[5])
    assert 1 <= int(headers["retry-after"]) <= 60
    _hs, head_headers, head_body = parse(replies[6])
    assert head_body == b"" and head_headers["content-length"] == headers["content-length"]
    assert daemon.requests() == []
    assert done.stderr.splitlines() == (["afx-ops: web_auth_failed"] * 5
                                        + ["afx-ops: web_auth_limited"] * 2)


@pytest.mark.parametrize(("data", "reason"), [(b"short", "secret_invalid"),
                                              (b"has space in password!!", "secret_invalid")])
def test_an_unsafe_password_stops_the_process_before_isolation(root, daemon, child_env, data, reason):
    install_password(root, data)
    web = Interactive(spec(root, free_port(), requests=1, web_auth="basic"), child_env)
    try:
        done = web.finish()
    finally:
        web.kill()
    assert done.rc != 0
    assert done.stderr.strip().splitlines()[-1] == f"afx-ops: {reason}"
    assert "ISOLATED" not in done.stdout


def test_a_missing_password_stops_the_process_before_isolation(root, daemon, child_env):
    web = Interactive(spec(root, free_port(), requests=1, web_auth="basic"), child_env)
    try:
        done = web.finish()
    finally:
        web.kill()
    assert done.rc != 0
    assert done.stderr.strip().splitlines()[-1] == "afx-ops: secret_missing"
    assert "ISOLATED" not in done.stdout


def test_no_secret_fd_survives_isolation_and_nothing_identifying_is_written(root, daemon, child_env):
    install_password(root)
    port = free_port()
    intruder = base64.b64encode(b"intruder-canary:" + PASSWORD)
    raws = [get("/", "GET", b"Authorization: Basic " + intruder + b"\r\nX-Forwarded-For: 203.0.113.77\r\n"),
            authed(TOKEN)]
    replies, done = talk(raws, root, port, child_env,
                         params={"after": [{"op": "web_fds", "params": {}}]})
    result = assert_finished(done)
    assert [parse(reply)[0] for reply in replies] == [401, 200]
    fds = result["after"][0]
    assert fds["fds"] == sorted({0, 1, 2, fds["listener"]}) == fds["kept"]
    assert fds["children"] == 0
    output = done.stdout + done.stderr
    for canary in ("intruder-canary", "203.0.113.77", "127.0.0.1", intruder.decode(),
                   PASSWORD.decode(), TOKEN.decode(), "Authorization"):
        assert canary not in output.replace(f"{port}", ""), canary
    assert [r["target"] for r in daemon.requests()] == ["/v1/status"]
