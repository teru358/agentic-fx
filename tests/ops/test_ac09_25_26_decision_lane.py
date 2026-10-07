"""AC-9 / AC-25 / AC-26: 決定レーンの一意性、同期失敗、FIFO と容量。"""
from __future__ import annotations

import threading
import time

import pytest

from agentic_fx.commands import Commands
from agentic_fx.core.contracts import SystemClock
from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.ops.jobs import DECISION_QUEUE_CAPACITY

from .conftest import APPROVER, digest_of, hold_plugin_lock, wait_job


def _shell(env, service):
    from agentic_fx.activity import ActivityLog
    commands = Commands(conn=env.conn, state_store=env.state, broker=None,
                        trade_loop=None, activity=ActivityLog(env.activity_path),
                        log_dir=env.root / "logs", clock=SystemClock(),
                        plugins_root=env.plugins_root, settings=env.settings)
    commands.use_ops_service(service)
    return commands


def _decision_count(env, approval_id):
    return env.conn.execute(
        "SELECT COUNT(*) AS n FROM plugin_switch_journal WHERE approval_id=?",
        (approval_id,)).fetchone()["n"]


def test_ac9_two_api_approves_decide_once(ops_env):
    service = ops_env.service()
    approval_id = ops_env.make_plugin_approval()
    digest = digest_of(ops_env, approval_id)
    release = hold_plugin_lock(ops_env.plugins_root, "sma")
    try:
        first = service.approve(APPROVER, approval_id, digest)
        with pytest.raises(OpsError) as raised:
            service.approve(APPROVER, approval_id, digest)
        assert raised.value.code is ErrorCode.DECISION_IN_PROGRESS
    finally:
        release()
    assert wait_job(service, first["job_id"])["state"] == "done"
    with pytest.raises(OpsError) as raised:
        service.approve(APPROVER, approval_id, digest)
    assert raised.value.code is ErrorCode.ALREADY_DECIDED
    assert _decision_count(ops_env, approval_id) == 1


def test_ac9_shell_and_api_share_the_decision_lane(ops_env):
    service = ops_env.service()
    shell = _shell(ops_env, service)
    approval_id = ops_env.make_plugin_approval()
    release = hold_plugin_lock(ops_env.plugins_root, "sma")
    try:
        api = service.approve(APPROVER, approval_id, digest_of(ops_env, approval_id))
        text = shell.dispatch(f"approve {approval_id}")
    finally:
        release()
    assert "エラー: decision_in_progress" == text
    assert wait_job(service, api["job_id"])["state"] == "done"
    assert shell.dispatch(f"approve {approval_id}") == "その approval は決定済みです"
    assert ops_env.approval(approval_id)["decided_by"] == "api:approver"
    assert _decision_count(ops_env, approval_id) == 1


def test_ac9_shell_decision_via_lane_keeps_texts_and_decided_by(ops_env):
    service = ops_env.service()
    shell = _shell(ops_env, service)
    approval_id = ops_env.make_plugin_approval()
    text = shell.dispatch(f"approve {approval_id}")
    assert text.startswith(f"approval #{approval_id} approved: ")
    row = ops_env.approval(approval_id)
    assert (row["status"], row["decided_by"]) == ("approved", "shell")
    other = ops_env.make_plugin_approval(name="ema", mission_id=2)
    assert shell.dispatch(f"reject {other} 理由") == f"approval #{other} rejected"
    assert ops_env.approval(other)["decided_by"] == "shell"


def test_ac25_sync_failures_create_no_job(ops_env):
    service = ops_env.service()
    approval_id = ops_env.make_plugin_approval()
    with pytest.raises(OpsError) as raised:
        service.approve(APPROVER, approval_id, "f" * 64)
    assert raised.value.code is ErrorCode.PAYLOAD_CHANGED
    with pytest.raises(OpsError) as raised:
        service.approve(APPROVER, 9999, "f" * 64)
    assert raised.value.code is ErrorCode.NOT_FOUND
    reply = service.reject(APPROVER, approval_id)
    wait_job(service, reply["job_id"])
    with pytest.raises(OpsError) as raised:
        service.reject(APPROVER, approval_id)
    assert raised.value.code is ErrorCode.ALREADY_DECIDED
    # 受理された 1 件以外に job は作られていない。
    assert [j.id for j in service.jobs._jobs.values()] == [reply["job_id"]]


def test_ac25_job_completes_without_polling_and_duplicate_is_in_progress(ops_env):
    service = ops_env.service()
    approval_id = ops_env.make_plugin_approval()
    release = hold_plugin_lock(ops_env.plugins_root, "sma")
    try:
        reply = service.reject(APPROVER, approval_id, "no")
        with pytest.raises(OpsError) as raised:
            service.reject(APPROVER, approval_id, "no")
        assert raised.value.code is ErrorCode.DECISION_IN_PROGRESS
    finally:
        release()
    # client は poll しない (切断相当)。job は worker だけで終端まで進む。
    deadline = time.monotonic() + 10
    while ops_env.approval(approval_id)["status"] == "pending" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ops_env.approval(approval_id)["status"] == "rejected"
    assert service.jobs.lookup(reply["job_id"]).state.value == "done"


def test_ac26_ninth_queued_decision_is_unavailable_and_eight_run_fifo(ops_env):
    service = ops_env.service()
    blocker = ops_env.make_plugin_approval(name="aaa", mission_id=1)
    ids = [ops_env.make_plugin_approval(name=f"p{n}", mission_id=10 + n) for n in range(9)]
    order: list[int] = []
    original = service._decision_action

    def recording(kind, approval_id, digest, reason, decided_by):
        run = original(kind, approval_id, digest, reason, decided_by)

        def wrapped(job):
            order.append(approval_id)
            return run(job)
        return wrapped

    service._decision_action = recording
    release = hold_plugin_lock(ops_env.plugins_root, "aaa")
    try:
        running = service.reject(APPROVER, blocker)
        for _ in range(500):
            if service.jobs.lookup(running["job_id"]).state.value == "running":
                break
            time.sleep(0.01)
        queued = [service.reject(APPROVER, approval_id)
                  for approval_id in ids[:DECISION_QUEUE_CAPACITY]]
        with pytest.raises(OpsError) as raised:
            service.reject(APPROVER, ids[DECISION_QUEUE_CAPACITY])
        assert raised.value.code is ErrorCode.UNAVAILABLE
    finally:
        release()
    for reply in [running] + queued:
        assert wait_job(service, reply["job_id"])["state"] == "done"
    assert order == [blocker] + ids[:DECISION_QUEUE_CAPACITY]
    assert ops_env.approval(ids[DECISION_QUEUE_CAPACITY])["status"] == "pending"


def _legacy_shell(env, **extra):
    from agentic_fx.activity import ActivityLog
    return Commands(conn=env.conn, state_store=env.state, broker=None,
                    trade_loop=None, activity=ActivityLog(env.activity_path),
                    log_dir=env.root / "logs", clock=SystemClock(),
                    plugins_root=env.plugins_root, settings=env.settings, **extra)


def _shell_routes(env):
    return [r["endpoint"] for r in env.ops_rows()
            if r["phase"] == "accepted" and r["authenticated_principal"] == "shell"]


def _order(env, status, attempts_at=None):
    now = env.wall().isoformat()
    cur = env.conn.execute(
        "INSERT INTO orders(pair,direction,entry_type,horizon,status,created_at,updated_at) "
        "VALUES ('EURUSD','long','market','x',?,?,?)", (status, now, now))
    order_id = int(cur.lastrowid)
    if attempts_at is not None:
        env.conn.execute("INSERT INTO reflection_attempts(order_id,attempts,last_attempt_at,"
                         "last_reason) VALUES (?,?,?,?)", (order_id, 1, attempts_at, "x"))
    env.conn.commit()
    return order_id


def test_shell_through_the_lane_keeps_the_legacy_backlog_and_reflect_texts(ops_env):
    from agentic_fx.store import backlog
    service = ops_env.service()
    lane, legacy = _shell(ops_env, service), _legacy_shell(ops_env)
    selected = []
    for _ in range(2):
        backlog_id = backlog.add(ops_env.conn, "idea", "user", ops_env.wall())
        assert backlog.select_for_mission(ops_env.conn, backlog_id, now=ops_env.wall())
        selected.append(backlog_id)
    at = "2026-10-05T00:00:00+00:00"
    closed = [_order(ops_env, "closed", at), _order(ops_env, "closed", at)]
    still_open = [_order(ops_env, "open"), _order(ops_env, "open")]

    def same(line, ours, theirs):
        expected = legacy.dispatch(line.format(theirs)).replace(f"#{theirs}", f"#{ours}")
        assert lane.dispatch(line.format(ours)) == expected, line

    same("backlog reject {}", *selected)
    same("reflect retry {}", *closed)
    same("reflect retry {}", *still_open)
    assert ops_env.conn.execute("SELECT COUNT(*) AS n FROM reflection_attempts "
                                "WHERE order_id IN (?,?)", tuple(closed)).fetchone()["n"] == 0
    assert sorted(_shell_routes(ops_env)) == ["POST /v1/backlog/{id}/reject",
                                              "POST /v1/reflections/{order_id}/retry"]


def test_shell_approval_retry_goes_through_the_lane_with_the_legacy_text(ops_env):
    service = ops_env.service()
    lane = _shell(ops_env, service)
    approval_id = ops_env.make_plugin_approval()
    text = lane.dispatch(f"approval retry {approval_id}")
    assert text.startswith(f"approval #{approval_id} を再試行しました: ")
    assert _shell_routes(ops_env) == ["POST /v1/approvals/{id}/retry"]
    assert ops_env.approval(approval_id)["decided_by"] == "shell"


def test_shell_policy_backlog_and_improve_go_through_the_lane(ops_env):
    class Improve:
        def submit_manual(self, *, on_prepared=None):
            return 5

    improve = Improve()
    service = ops_env.service(improve_supervisor=improve)
    lane = _legacy_shell(ops_env, improve_supervisor=improve)
    lane.use_ops_service(service)
    assert lane.dispatch("policy add 方針を守る") == "policy に追記しました"
    assert lane.dispatch("improve add 新しい案").startswith("backlog #")
    assert lane.dispatch("improve").startswith("improve job ")
    assert sorted(_shell_routes(ops_env)) == ["POST /v1/backlog", "POST /v1/improve/runs",
                                              "POST /v1/policy"]
