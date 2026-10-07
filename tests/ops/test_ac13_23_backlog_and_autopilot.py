"""AC-13: selected の backlog は人手遷移を受けない。
AC-23: autopilot 中は GET / reject / ask / jobs / killswitch reconcile だけを API に許す。"""
from __future__ import annotations

import concurrent.futures

import pytest

from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.ops.contracts import ShellCaller
from agentic_fx.store import backlog

from .conftest import APPROVER, OPERATOR, digest_of, wait_job


@pytest.mark.parametrize("action", ["reject", "reopen", "note"])
def test_ac13_selected_backlog_transition_is_409_and_row_unchanged(ops_env, action):
    service = ops_env.service()
    backlog_id = backlog.add(ops_env.conn, "idea", "user", ops_env.wall())
    assert backlog.select_for_mission(ops_env.conn, backlog_id, now=ops_env.wall())
    before = tuple(ops_env.conn.execute(
        "SELECT * FROM improvement_backlog WHERE id=?", (backlog_id,)).fetchone())
    ops_env.wall.advance(minutes=1)
    with pytest.raises(OpsError) as raised:
        service.transition_backlog(OPERATOR, backlog_id, action)
    assert raised.value.code is ErrorCode.INVALID_STATE
    after = tuple(ops_env.conn.execute(
        "SELECT * FROM improvement_backlog WHERE id=?", (backlog_id,)).fetchone())
    assert after == before


def test_ac13_allowed_transitions_follow_the_state_machine(ops_env):
    service = ops_env.service()
    backlog_id = backlog.add(ops_env.conn, "idea", "user", ops_env.wall())
    assert service.transition_backlog(OPERATOR, backlog_id, "note")["status"] == "note"
    assert service.transition_backlog(OPERATOR, backlog_id, "reopen")["status"] == "open"
    row = ops_env.conn.execute("SELECT status,last_result FROM improvement_backlog WHERE id=?",
                               (backlog_id,)).fetchone()
    assert (row["status"], row["last_result"]) == ("open", "human_reopened")
    with pytest.raises(OpsError) as raised:
        service.transition_backlog(OPERATOR, 9999, "reject")
    assert raised.value.code is ErrorCode.NOT_FOUND


def _autopilot_env(ops_env):
    ops_env.state.update(kill_switch_latched=True)
    ops_env.state.update(autopilot=True)
    return ops_env.state.load().kill_switch_generation


def test_ac23_restricted_mutations_are_403_under_autopilot(ops_env):
    order_id = _closed_order(ops_env)
    improve = _Improve()
    service = ops_env.service(data_resume=lambda ack: {"ok": True},
                              improve_supervisor=improve)
    backlog_id = backlog.add(ops_env.conn, "idea", "user", ops_env.wall())
    approval_id = ops_env.make_plugin_approval()
    digest = digest_of(ops_env, approval_id)
    generation = _autopilot_env(ops_env)
    restricted = {
        "policy": lambda: service.add_policy(OPERATOR, "k-policy", "text"),
        "approve": lambda: service.approve(APPROVER, approval_id, digest),
        "retry": lambda: service.retry(APPROVER, approval_id, digest),
        "killswitch_reset": lambda: service.reset_kill_switch(APPROVER, generation),
        "data_resume": lambda: service.resume_data(APPROVER, acknowledge=True),
        "improve": lambda: service.improve(OPERATOR, "k-improve"),
        "backlog_add": lambda: service.add_backlog(OPERATOR, "k-backlog", "idea"),
        "backlog_note": lambda: service.transition_backlog(OPERATOR, backlog_id, "note"),
        "reflection_retry": lambda: service.retry_reflection(
            OPERATOR, "k-reflect", order_id, expected_attempts=0,
            expected_last_attempt_at=None),
    }
    for name, call in restricted.items():
        with pytest.raises(OpsError) as raised:
            call()
        assert raised.value.code is ErrorCode.AUTOPILOT_RESTRICTED, name
    assert ops_env.approval(approval_id)["status"] == "pending"
    assert ops_env.state.load().kill_switch_latched
    assert improve.calls == 0
    assert not ops_env.policy_path.exists()


def test_ac23_reads_reject_ask_jobs_and_reconcile_pass_under_autopilot(ops_env):
    future = concurrent.futures.Future()
    future.set_result("answer")
    service = ops_env.service(ask_submitter=lambda question: future)
    approval_id = ops_env.make_plugin_approval()
    _autopilot_env(ops_env)
    assert service.status(OPERATOR)["autopilot"] is True
    assert service.approval_list(OPERATOR)["approvals"][0]["id"] == approval_id
    assert service.approval_detail(OPERATOR, approval_id)["status"] == "pending"
    assert service.whoami(OPERATOR)["authenticated_principal"] == "operator"
    assert service.events_after(OPERATOR, after=0, limit=10)["high_watermark"] >= 0
    reply = service.reject(APPROVER, approval_id, "no")
    assert wait_job(service, reply["job_id"])["state"] == "done"
    asked = service.ask(OPERATOR, "k-ask", "質問")
    assert wait_job(service, asked["job_id"])["state"] == "done"
    assert service.get_job(OPERATOR, asked["job_id"])["state"] == "done"
    # reconcile は autopilot でも通り、marker が無ければ 409 invalid_state で無変更。
    with pytest.raises(OpsError) as raised:
        service.reconcile_kill_switch(APPROVER)
    assert raised.value.code is ErrorCode.INVALID_STATE


def test_ac23_shell_is_not_restricted_by_autopilot(ops_env):
    service = ops_env.service()
    _autopilot_env(ops_env)
    reply = service.add_backlog(ShellCaller.SHELL, "k-shell", "idea")
    assert reply["id"] > 0


class _Improve:
    def __init__(self):
        self.calls = 0

    def submit_manual(self, *, on_prepared=None):
        self.calls += 1
        return 1


def _closed_order(env):
    now = env.wall().isoformat()
    cur = env.conn.execute(
        "INSERT INTO orders(pair,direction,entry_type,horizon,status,created_at,updated_at) "
        "VALUES ('EURUSD','long','market','x','closed',?,?)", (now, now))
    env.conn.commit()
    return int(cur.lastrowid)


def test_ac23_every_backlog_transition_is_restricted_and_job_cancel_passes_under_autopilot(
        ops_env):
    service = ops_env.service()
    backlog_id = backlog.add(ops_env.conn, "idea", "user", ops_env.wall())
    _autopilot_env(ops_env)
    for action in ("reject", "reopen", "note"):
        with pytest.raises(OpsError) as raised:
            service.transition_backlog(OPERATOR, backlog_id, action)
        assert raised.value.code is ErrorCode.AUTOPILOT_RESTRICTED, action
    with pytest.raises(OpsError) as raised:
        service.cancel_job(OPERATOR, "missing")
    assert raised.value.code is ErrorCode.NOT_FOUND
