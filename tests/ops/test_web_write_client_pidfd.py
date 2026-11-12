"""afx-ops の書込み client (`ops-web-write`) の決定は、pidfd で相手を確かめられる接続だけが通る。

web の決定 (承認・却下) は DECIDE で、server は接続相手の pidfd を確かめられないとき 403 にする。
実 UDS・実 service・afx-ops の実 client で、pidfd を使えない server と使える server を比べる。
"""
from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path

from agentic_fx.ops import keys
from agentic_fx.ops.contracts import Principal

from ._api import make_api

AFX_OPS = Path(__file__).resolve().parents[2] / "afx-ops"
sys.path.insert(0, str(AFX_OPS))

from afx_ops.api_client import Endpoint, WebWriteClient  # noqa: E402

ACTOR = "web:afx"


def _client(api) -> WebWriteClient:
    endpoint = Endpoint(socket_path=api.socket_path, lock_path=api.socket_path.parents[1]
                        / "instance.lock")
    return WebWriteClient.from_token(endpoint, api.token(Principal.OPS_WEB_WRITE).encode())


def _serve(ops_env, **server_kwargs):
    directory = keys.key_dir(ops_env.root)
    keys.initialize(directory, root=ops_env.root)
    keys.enroll(directory, Principal.OPS_WEB_WRITE)
    api = make_api(ops_env, **server_kwargs)
    # client は lock の保持者と接続相手の pid の一致を確かめる (この process が server)
    api.lock_fd = os.open(api.socket_path.parents[1] / "instance.lock",
                          os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(api.lock_fd, fcntl.LOCK_EX)
    return api


def _stop(api) -> None:
    os.close(api.lock_fd)
    for client in api.clients:
        client.close()
    api.server.stop(5.0)


def _reject(api, approval_id):
    return _client(api).reject(ACTOR, approval_id, reason=None)


def test_a_server_that_cannot_verify_the_peer_refuses_the_web_decision(ops_env):
    approval = ops_env.make_plugin_approval("p1", 1.0)
    api = _serve(ops_env, pidfd_supported=False)
    try:
        reply = _reject(api, approval)
        assert (reply.status, reply.code) == (403, "forbidden")
        assert ops_env.approval(approval)["status"] == "pending"
    finally:
        _stop(api)


def test_a_server_that_verifies_the_peer_accepts_the_same_decision(ops_env):
    approval = ops_env.make_plugin_approval("p1", 1.0)
    api = _serve(ops_env)
    try:
        reply = _reject(api, approval)
        assert reply.status == 202 and reply.code is None, reply
    finally:
        _stop(api)
