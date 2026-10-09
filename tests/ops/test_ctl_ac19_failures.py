"""AC-19: 接続・認証・受付の失敗は規定の文面と rc=3。業務上の失敗は rc=1。"""
from __future__ import annotations

import os
import socket

import pytest

from agentic_fx.ops import keys
from agentic_fx.ops.api_server import ApiLimits
from agentic_fx.ops.contracts import Principal
from agentic_fx.store.instance_lock import acquire_instance_lock

from ._api import make_api
from ._ctl import ctl_api, run_ctl  # noqa: F401


@pytest.fixture
def held_lock(ops_env):
    lock = acquire_instance_lock(ops_env.root / "data")
    yield
    lock.close()


def _started(ops_env, **kwargs):
    api_env = make_api(ops_env, service_pid=kwargs.pop("service_pid", 0), **kwargs)
    return api_env


def test_no_socket(ops_env, held_lock):
    keys.ensure_initialized(keys.key_dir(ops_env.root), root=ops_env.root)
    rc, out, err = run_ctl(ops_env.root, ["status"])
    assert (rc, out) == (3, "")
    assert "サービスに接続できません: socket がありません" in err


def test_connection_refused_on_stale_socket(ops_env, held_lock):
    keys.ensure_initialized(keys.key_dir(ops_env.root), root=ops_env.root)
    run = ops_env.root / "data" / "run"
    run.mkdir(mode=0o700)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(run / "api.sock"))
    stale.close()
    rc, _, err = run_ctl(ops_env.root, ["status"])
    assert rc == 3
    assert "接続を拒否されました" in err


def test_missing_key_is_not_generated(ops_env, held_lock):
    rc, _, err = run_ctl(ops_env.root, ["status"])
    assert rc == 3
    assert "鍵を読めません (not_initialized)" in err
    assert not keys.key_dir(ops_env.root).exists()


def test_unauthenticated_401(ops_env, held_lock):
    api_env = _started(ops_env)
    try:
        keys.rotate(api_env.key_dir, Principal.OPERATOR)
        rc, _, err = run_ctl(ops_env.root, ["status"])
    finally:
        api_env.server.stop(5.0)
    assert rc == 3
    assert "認証に失敗しました" in err


def test_forbidden_403_autopilot(ctl_api):
    ctl_api.env.state.update(autopilot=True)
    rc, _, err = run_ctl(ctl_api.env.root, ["policy", "add", "x"])
    assert rc == 3
    assert "autopilot 中はこの操作を受け付けません" in err
    assert not ctl_api.env.policy_path.exists()


def test_forbidden_403_peer_rejected(ops_env, held_lock):
    # 試験 process を service の子孫に見せる (親 pid を service とする)。
    api_env = _started(ops_env, service_pid=os.getppid())
    try:
        rc, _, err = run_ctl(ops_env.root, ["status"])
    finally:
        api_env.server.stop(5.0)
    assert rc == 3
    assert "daemon が接続元を拒否しました" in err


def test_unavailable_503(ops_env, held_lock):
    api_env = _started(ops_env, limits=ApiLimits(max_connections=0))
    try:
        rc, _, err = run_ctl(ops_env.root, ["status"])
    finally:
        api_env.server.stop(5.0)
    assert rc == 3
    assert "daemon が要求を受け付けられません" in err


def test_server_authentication_failure(ops_env):
    """lock 保持者がいない (= 本物と確かめられない) と鍵を送らない。"""
    api_env = _started(ops_env)
    try:
        rc, _, err = run_ctl(ops_env.root, ["status"])
    finally:
        api_env.server.stop(5.0)
    assert rc == 3
    assert "鍵を送りませんでした (not_lock_holder)" in err
    assert ops_env.ops_rows() == []


def test_operator_asking_for_approver_operation_sends_nothing(ctl_api):
    approval_id = ctl_api.env.raw_approval()
    rc, _, err = run_ctl(ctl_api.env.root, ["approve", str(approval_id)])
    assert rc == 3
    assert "approver の権限が要ります" in err
    assert ctl_api.env.ops_rows() == []


def test_business_refusal_is_rc1(ctl_api):
    rc, _, err = run_ctl(ctl_api.env.root, ["backlog", "reject", "999"])
    assert rc == 1
    assert "backlog #999 は存在しません" in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root は検索権限の無い dir も辿れる")
def test_unreadable_socket_dir_is_server_auth_failure_rc3(ops_env, held_lock):
    keys.ensure_initialized(keys.key_dir(ops_env.root), root=ops_env.root)
    data = ops_env.root / "data"
    mode = data.stat().st_mode & 0o7777
    data.chmod(0)
    try:
        rc, out, err = run_ctl(ops_env.root, ["status"])
    finally:
        data.chmod(mode)
    assert (rc, out) == (3, "")
    assert "接続先を agentic-fx の daemon と確認できないため、鍵を送りませんでした" in err
    assert "socket がありません" not in err
    assert "Traceback" not in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root は書込み権限の無い dir にも書ける")
def test_events_cursor_save_failure_is_a_warning_with_rc1(ctl_api):
    from agentic_fx.ops import client
    env = ctl_api.env
    assert run_ctl(env.root, ["policy", "add", "a"])[0] == 0
    state_dir = client._events_cursor_path(env.root).parent
    state_dir.mkdir(mode=0o700, parents=True)
    state_dir.chmod(0o500)
    try:
        rc, out, err = run_ctl(env.root, ["events"])
    finally:
        state_dir.chmod(0o700)
    assert rc == 1
    assert any(line.startswith("#") for line in out.splitlines())
    assert "cursor を保存できませんでした" in err
    assert "Traceback" not in err
