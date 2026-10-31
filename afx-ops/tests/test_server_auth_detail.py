"""server 認証の失敗の観測語 (detail) と、fd 枯渇の transient 化。

detail は ``step=<段> errno=<n>`` の固定形で stderr の診断にだけ添える。reason の語彙は
変えない。
"""
from __future__ import annotations

import errno
import io
import re
import socket
from pathlib import Path

import pytest

from afx_ops import api_client, server_auth
from afx_ops.server_auth import step_detail
from afx_ops.api_client import ApiError, with_detail
from afx_ops.server_auth import ServerAuthError
from afx_ops.web.request import Request, handle_request

DETAIL = re.compile(r"step=(getsockopt|fdinfo_open|fdinfo_read|locks_open|locks_read) errno=\d+")


@pytest.fixture
def pair():
    a, b = socket.socketpair()
    yield a
    a.close()
    b.close()


def _raise(exc):
    def fn(*_args, **_kwargs):
        raise exc
    return fn


@pytest.mark.parametrize("err, reason", [
    (errno.ENOMEM, "pidfd_transient"), (errno.EMFILE, "pidfd_transient"),
    (errno.ENFILE, "pidfd_transient"),
    (errno.EACCES, "ops_requires_so_peerpidfd"), (errno.ENOENT, "ops_requires_so_peerpidfd"),
])
def test_check_pidfd_maps_only_resource_exhaustion_to_transient(pair, monkeypatch, err, reason):
    monkeypatch.setattr(server_auth, "peer_credentials", lambda _s: (1, 0))
    monkeypatch.setattr("builtins.open", _raise(OSError(err, "x")))
    with pytest.raises(ServerAuthError) as caught:
        server_auth.check_pidfd(pair, 99)
    assert caught.value.reason == reason
    assert caught.value.detail == f"step=fdinfo_open errno={err}"


def test_pidfd_pid_names_the_failing_step(tmp_path, monkeypatch):
    # open は通り、read で失敗する
    class Broken(io.StringIO):
        def __iter__(self):
            raise OSError(errno.EIO, "x")

    monkeypatch.setattr("builtins.open", lambda *a, **k: Broken())
    with pytest.raises(OSError) as caught:
        server_auth.pidfd_pid(3)
    assert caught.value.detail == f"step=fdinfo_read errno={errno.EIO}"


def test_locks_open_and_read_failures_carry_their_step(tmp_path, monkeypatch):
    with pytest.raises(ServerAuthError) as missing:
        server_auth.check_lock_device(Path(__file__), proc_locks=tmp_path / "none")
    assert missing.value.reason == "not_lock_holder"
    assert missing.value.detail == f"step=locks_open errno={errno.ENOENT}"
    class Unreadable:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            raise OSError(errno.EIO, "x")

    monkeypatch.setattr("builtins.open", lambda *a, **k: Unreadable())
    with pytest.raises(ServerAuthError) as unread:
        server_auth.check_lock_device(Path(__file__), proc_locks=tmp_path)
    assert unread.value.detail == f"step=locks_read errno={errno.EIO}"


def test_peer_credentials_failure_carries_the_getsockopt_step(pair, monkeypatch):
    class Sock:
        def getsockopt(self, *_a):
            raise OSError(errno.EBADF, "x")

    with pytest.raises(ServerAuthError) as caught:
        server_auth.peer_credentials(Sock())
    assert (caught.value.reason, caught.value.detail) == (
        "peer_check", f"step=getsockopt errno={errno.EBADF}")


def test_fetch_pidfd_carries_the_getsockopt_step():
    class Sock:
        def getsockopt(self, *_a):
            raise OSError(errno.ENOMEM, "x")

    with pytest.raises(ServerAuthError) as caught:
        server_auth.fetch_pidfd(Sock())
    assert (caught.value.reason, caught.value.detail) == (
        "pidfd_transient", f"step=getsockopt errno={errno.ENOMEM}")


def test_the_reason_vocabulary_is_unchanged():
    assert server_auth.REASONS == (
        "socket_dir", "socket_file", "peer_uid", "peer_gone", "peer_check", "not_lock_holder",
        "holder_gone", "ops_requires_so_peerpidfd", "instance_lock_dev_mismatch",
        "pidfd_transient")


def test_with_detail_appends_only_the_fixed_shape():
    assert with_detail("a", "step=fdinfo_open errno=13") == "a detail=step=fdinfo_open errno=13"
    assert with_detail("a", "") == "a"
    assert with_detail("a", "step=x errno=13 /home/u/secret") == "a"
    assert with_detail("a", "step=x errno=13") == "a"
    assert with_detail("a", "step=fdinfo_open errno=13") == "a detail=step=fdinfo_open errno=13"
    assert step_detail("x", 13) == "step=unknown errno=13"
    assert with_detail("a", "step=x errno=123456") == "a"


class _Client:
    def __init__(self, error):
        self.error = error

    def status(self):
        raise self.error


def test_web_upstream_line_carries_detail_and_the_response_does_not(capsys):
    error = ApiError("ops_requires_so_peerpidfd", detail="step=fdinfo_open errno=13")
    response = handle_request(Request("GET", "/", {}), _Client(error))
    assert response.status == 502
    assert capsys.readouterr().err == (
        "afx-ops: web_upstream reason=ops_requires_so_peerpidfd "
        "detail=step=fdinfo_open errno=13 route=status\n")
    assert b"errno" not in response.body and b"fdinfo" not in response.body


def test_notify_exit_line_carries_detail():
    from afx_ops.notify import runtime

    class Iso:
        state_dir = Path("/nonexistent")
        secrets = {}

    class Loop:
        def __init__(self, *a, **k):
            pass

        def run(self, _stop):
            raise ApiError("ops_requires_so_peerpidfd", detail="step=fdinfo_open errno=13")

    out = io.StringIO()
    original = runtime._Loop
    runtime._Loop = Loop
    try:
        rc = runtime.run(Iso(), client=object(), send=lambda *a: None, stderr=out)
    finally:
        runtime._Loop = original
    assert rc == runtime.RC_UNREACHABLE
    assert out.getvalue() == (
        "afx-ops: ops_requires_so_peerpidfd detail=step=fdinfo_open errno=13\n")


def test_bot_exit_line_carries_detail(monkeypatch, tmp_path):
    from afx_ops.bot import runtime

    async def main(*_a, **_k):
        raise ApiError("pidfd_transient", detail="step=getsockopt errno=12")

    class Iso:
        state_dir = tmp_path
        secrets = {}

    monkeypatch.setattr(runtime, "parse_token", lambda _v: "t")
    monkeypatch.setattr(runtime, "open_state_dir", lambda _p: __import__("os").open(
        tmp_path, __import__("os").O_RDONLY))
    monkeypatch.setattr(runtime, "_main", main)
    monkeypatch.setattr(runtime, "_quiet_discord_logging", lambda: None)
    out = io.StringIO()
    rc = runtime.run(Iso(), client=object(), stderr=out)
    assert rc == runtime.RC_UNREACHABLE
    assert out.getvalue() == "afx-ops: pidfd_transient detail=step=getsockopt errno=12\n"


def test_startup_run_prints_detail_for_an_api_error():
    from afx_ops.isolation import startup
    assert startup.with_detail("x", "step=getsockopt errno=1") == "x detail=step=getsockopt errno=1"


def test_api_error_defaults_to_no_detail():
    assert ApiError("timeout").detail == "" and api_client.ApiError("a", token_sent=True).detail == ""
