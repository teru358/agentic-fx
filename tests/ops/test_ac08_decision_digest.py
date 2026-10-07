"""AC-8: approve / retry の digest は受理時と plugin flock 内の二地点で照合する。"""
from __future__ import annotations

import json

import pytest

from agentic_fx.ops import ErrorCode, OpsError

from .conftest import APPROVER, digest_of, hold_plugin_lock, wait_job


def _live(env, name="sma"):
    return env.plugins_root / name


def test_ac8_correct_digest_approves_and_deploys(ops_env):
    service = ops_env.service()
    approval_id = ops_env.make_plugin_approval()
    reply = service.approve(APPROVER, approval_id, digest_of(ops_env, approval_id))
    job = wait_job(service, reply["job_id"])
    assert job["state"] == "done"
    assert job["result"]["outcome"] == "deployed"
    row = ops_env.approval(approval_id)
    assert (row["status"], row["decided_by"]) == ("approved", "api:approver")
    assert _live(ops_env).is_symlink()


@pytest.mark.parametrize("digest", ["0" * 64, None, "abc"])
def test_ac8_wrong_or_missing_digest_is_refused_and_stays_pending(ops_env, digest):
    service = ops_env.service()
    approval_id = ops_env.make_plugin_approval()
    with pytest.raises(OpsError) as raised:
        service.approve(APPROVER, approval_id, digest)
    expected = ErrorCode.PAYLOAD_CHANGED if digest == "0" * 64 else ErrorCode.INVALID_ARGUMENT
    assert raised.value.code is expected
    assert ops_env.approval(approval_id)["status"] == "pending"
    assert not _live(ops_env).exists()


def test_ac8_payload_changed_after_acceptance_fails_job_and_stays_pending(ops_env):
    """受理後・実行前の改変。実行時の照合で止まる。"""
    service = ops_env.service()
    approval_id = ops_env.make_plugin_approval()
    digest = digest_of(ops_env, approval_id)
    blocker = ops_env.make_plugin_approval(name="zzz_blocker", mission_id=2)
    release = hold_plugin_lock(ops_env.plugins_root, "zzz_blocker")
    try:
        first = service.reject(APPROVER, blocker)
        second = service.approve(APPROVER, approval_id, digest)
        payload = json.loads(ops_env.approval(approval_id)["payload_json"])
        payload["content_hash"] = "tampered"
        ops_env.conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                             (json.dumps(payload), approval_id))
        ops_env.conn.commit()
    finally:
        release()
    wait_job(service, first["job_id"])
    job = wait_job(service, second["job_id"])
    assert (job["state"], job["result_code"]) == ("failed", "payload_changed")
    assert ops_env.approval(approval_id)["status"] == "pending"
    assert not _live(ops_env).exists()


def test_ac8_payload_changed_while_waiting_for_plugin_lock_is_caught_inside_lock(ops_env):
    """実行時の照合を通った後、flock を待つ間の改変。lock 内の再読で止まる。"""
    service = ops_env.service()
    approval_id = ops_env.make_plugin_approval()
    digest = digest_of(ops_env, approval_id)
    release = hold_plugin_lock(ops_env.plugins_root, "sma")
    try:
        reply = service.retry(APPROVER, approval_id, digest)
        # worker が job を取り、lock 待ちに入るまで待つ。
        for _ in range(500):
            if service.get_job(APPROVER, reply["job_id"])["state"] == "running":
                break
            __import__("time").sleep(0.01)
        __import__("time").sleep(0.1)
        payload = json.loads(ops_env.approval(approval_id)["payload_json"])
        payload["content_hash"] = "tampered"
        ops_env.conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                             (json.dumps(payload), approval_id))
        ops_env.conn.commit()
    finally:
        release()
    job = wait_job(service, reply["job_id"])
    assert (job["state"], job["result_code"]) == ("failed", "payload_changed")
    assert ops_env.approval(approval_id)["status"] == "pending"
    assert not _live(ops_env).exists()
    journal = ops_env.conn.execute("SELECT COUNT(*) AS n FROM plugin_switch_journal").fetchone()
    assert journal["n"] == 0
