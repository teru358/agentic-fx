"""afx 再起動後の再認証と、起動時の readiness 待ち。

client は別 process で、要求ごとに socket path を確かめ直す。認証できなければ
固定 reason で非零終了し (process 内で再試行しない)、鍵は送らない。
"""
from __future__ import annotations

import inspect
import os
import socket
import time

import pytest
from _ops_harness import (daemon, finish, make_layout, pidfd_supported, run_client,
                          spawn_client, start, stop, token_in, wait_until)
from _oracle_harness import received

from afx_ops import api_client

pytestmark = pytest.mark.skipif(not pidfd_supported(),
                                reason="SO_PEERPIDFD の無い kernel (< 6.5)")


def _restart_steps(layout, go):
    return [{"op": "call", "call": "status"}, {"op": "touch", "path": str(layout.tmp / "a")},
            {"op": "wait", "path": str(go)}, {"op": "call", "call": "status"}]


def _replace_server(layout) -> tuple[int, int]:
    """socket と lock file を消して作り直せるようにし、旧 (ino) を返す。"""
    old = (os.lstat(layout.socket_path).st_ino, os.stat(layout.lock_path).st_ino)
    os.unlink(layout.socket_path)
    os.unlink(layout.lock_path)
    layout.record.unlink(missing_ok=True)
    return old


def test_reauthenticates_after_daemon_restart_with_new_inode(tmp_path, child_env):
    layout = make_layout(tmp_path)
    go = tmp_path / "go"
    first = start(layout)
    proc = spawn_client(layout, layout.spec("ops-web", _restart_steps(layout, go)), child_env)
    second = None
    try:
        assert wait_until(lambda: (tmp_path / "a").exists())
        assert token_in(received(layout.record, expect_any=True), "ops-web")
        stop(first)
        old = _replace_server(layout)
        second = start(layout)
        assert (os.lstat(layout.socket_path).st_ino,
                os.stat(layout.lock_path).st_ino) != old
        go.touch()
        rc, lines, err = finish(proc)
        got = received(layout.record, expect_any=True)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        stop(second)
    assert rc == 0, err
    assert lines == [{"op": "call", "ok": True, "status": 200}] * 2
    assert token_in(got, "ops-web")


def test_unauthenticatable_after_restart_exits_nonzero(tmp_path, child_env):
    layout = make_layout(tmp_path)
    go = tmp_path / "go"
    first = start(layout)
    proc = spawn_client(layout, layout.spec("ops-web", _restart_steps(layout, go)), child_env)
    second = None
    try:
        assert wait_until(lambda: (tmp_path / "a").exists())
        stop(first)
        _replace_server(layout)
        # 偽物: socket は同 uid・0700 の dir にあるが、instance lock を持っていない。
        layout.lock_path.write_text("")
        second = start(layout, lock=False)
        go.touch()
        rc, lines, err = finish(proc)
        got = received(layout.record, expect_any=False)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        stop(second)
    assert rc == api_client.RC_UNREACHABLE
    assert err == "afx-ops: not_lock_holder\n"
    assert lines == [{"op": "call", "ok": True, "status": 200}]
    assert sum(map(len, got)) == 0


def test_readiness_defaults_are_pinned():
    assert api_client.READINESS_INTERVAL == 0.25
    assert api_client.READINESS_LIMIT == 30.0
    params = inspect.signature(api_client.wait_ready).parameters
    assert params["interval"].default == api_client.READINESS_INTERVAL
    assert params["limit"].default == api_client.READINESS_LIMIT


def test_readiness_caps_each_connection_attempt_at_the_remaining_budget(monkeypatch, tmp_path):
    clock = [0.0]
    timeouts = []

    class _Opened:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    def fake_open(endpoint, *, timeout):
        timeouts.append(timeout)
        clock[0] += timeout
        raise api_client.ApiError("refused")

    monkeypatch.setattr(api_client._Request, "open", staticmethod(fake_open))
    with pytest.raises(api_client.ApiError, match="readiness_timeout"):
        api_client.wait_ready(api_client.Endpoint(tmp_path / "socket", tmp_path / "lock"),
                              interval=0.25, limit=30.0, sleep=lambda value: None,
                              now=lambda: clock[0])

    assert timeouts == [15.0, 15.0]
    assert sum(timeouts) <= api_client.READINESS_LIMIT


def test_readiness_waits_until_socket_appears_within_limit(tmp_path, child_env):
    layout = make_layout(tmp_path)
    steps = [{"op": "ready", "interval": 0.05, "limit": 10.0}, {"op": "call", "call": "status"}]
    proc = spawn_client(layout, layout.spec("ops-web", steps), child_env)
    started = None
    try:
        time.sleep(1.0)
        assert proc.poll() is None
        started = start(layout)
        rc, lines, err = finish(proc)
        got = layout.received_at_least(2)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        stop(started)
    assert rc == 0, err
    assert lines == [{"op": "ready", "ok": True}, {"op": "call", "ok": True, "status": 200}]
    # readiness の接続は何も送らず、鍵が届いたのは後の要求だけ。
    assert got[0] == b""
    assert token_in(got[1:], "ops-web")


def test_readiness_gives_up_after_limit_without_sending_key(tmp_path, child_env):
    layout = make_layout(tmp_path)
    # listen していない古い socket file (接続は拒否される)。
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(layout.socket_path))
    stale.close()
    steps = [{"op": "ready", "interval": 0.05, "limit": 1.0}, {"op": "call", "call": "status"}]
    began = time.monotonic()
    rc, lines, err = run_client(layout, layout.spec("ops-web", steps), child_env)
    elapsed = time.monotonic() - began
    assert rc == api_client.RC_UNREACHABLE
    assert err == "afx-ops: readiness_timeout\n"
    assert lines == []
    assert 0.9 <= elapsed < 10
    assert not layout.record.exists()


def test_readiness_fails_fast_when_server_cannot_be_authenticated(tmp_path, child_env):
    layout = make_layout(tmp_path)
    layout.lock_path.write_text("")
    steps = [{"op": "ready", "interval": 0.05, "limit": 20.0}, {"op": "call", "call": "status"}]
    with daemon(layout, lock=False):
        began = time.monotonic()
        rc, lines, err = run_client(layout, layout.spec("ops-web", steps), child_env)
        elapsed = time.monotonic() - began
        got = received(layout.record, expect_any=False)
    assert rc == api_client.RC_UNREACHABLE
    assert err == "afx-ops: not_lock_holder\n"
    assert elapsed < 10
    assert sum(map(len, got)) == 0
