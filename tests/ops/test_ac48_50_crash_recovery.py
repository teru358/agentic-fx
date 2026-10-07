"""AC-48 (journal 結果の追記・reflect 冪等の部分) / AC-50 (回復規則) / AC-35 の crash 点。

子 process を実際に落とし (os._exit / SIGKILL)、親で起動時回復を呼ぶ。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentic_fx.ops import ErrorCode, OpsError
from agentic_fx.plugin import switch
from agentic_fx.store import backlog

from .conftest import APPROVER, OPERATOR, digest_of
from .test_ac13_23_backlog_and_autopilot import _closed_order

CHILD = Path(__file__).with_name("_crash_child.py")
ATTEMPT_AT = "2026-10-05T00:00:00+00:00"
IDEMPOTENT = ("policy", "backlog_add", "reflection_retry", "ask", "improve")
ENDPOINTS = IDEMPOTENT + ("backlog_note", "killswitch_reset", "data_resume")


def _prepare(env, endpoint):
    args = {}
    if endpoint == "reflection_retry":
        order_id = _closed_order(env)
        env.conn.execute(
            "INSERT INTO reflection_attempts(order_id,attempts,last_attempt_at,last_reason) "
            "VALUES (?,?,?,?)", (order_id, 1, ATTEMPT_AT, "x"))
        env.conn.commit()
        args["order_id"] = order_id
        args["attempt_at"] = ATTEMPT_AT
    elif endpoint == "backlog_note":
        args["backlog_id"] = backlog.add(env.conn, "seed", "user", env.wall())
    elif endpoint == "killswitch_reset":
        env.state.update(kill_switch_latched=True)
        args["generation"] = env.state.load().kill_switch_generation
    (env.root / "args.json").write_text(json.dumps(args))
    return args


def _effects(env, endpoint, args):
    if endpoint == "policy":
        return env.conn.execute("SELECT COUNT(*) AS n FROM ops_policies").fetchone()["n"]
    if endpoint == "backlog_add":
        return env.conn.execute("SELECT COUNT(*) AS n FROM improvement_backlog").fetchone()["n"]
    if endpoint == "reflection_retry":
        row = env.conn.execute("SELECT 1 FROM reflection_attempts WHERE order_id=?",
                               (args["order_id"],)).fetchone()
        return 0 if row is not None else 1
    if endpoint == "backlog_note":
        status = env.conn.execute("SELECT status FROM improvement_backlog WHERE id=?",
                                  (args["backlog_id"],)).fetchone()["status"]
        return int(status == "note")
    if endpoint == "killswitch_reset":
        return int(not env.state.load().kill_switch_latched)
    path = env.root / "effects" / endpoint
    return len(path.read_text().splitlines()) if path.exists() else 0


def _run_child(env, endpoint, point):
    proc = subprocess.Popen([sys.executable, str(CHILD), str(env.root), endpoint, point],
                            stdin=subprocess.DEVNULL)
    if point == "sigkill_after_accept":
        ready = env.root / "child-ready"
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert time.monotonic() < deadline and proc.poll() is None
            time.sleep(0.01)
        os.kill(proc.pid, signal.SIGKILL)
    returncode = proc.wait(60)
    if point == "sigkill_after_accept":
        assert returncode == -signal.SIGKILL
    else:
        assert returncode == 17, f"child did not crash at {point}: rc={returncode}"


def _resend(env, service, endpoint, args):
    if endpoint == "policy":
        return service.add_policy(OPERATOR, "key-1", "protect capital")
    if endpoint == "backlog_add":
        return service.add_backlog(OPERATOR, "key-1", "idea")
    if endpoint == "reflection_retry":
        return service.retry_reflection(OPERATOR, "key-1", args["order_id"],
                                         expected_attempts=1,
                                         expected_last_attempt_at=ATTEMPT_AT)
    if endpoint == "ask":
        return service.ask(OPERATOR, "key-1", "question")
    if endpoint == "improve":
        return service.improve(OPERATOR, "key-1")
    raise AssertionError(endpoint)


def _terminals(env):
    return [r for r in env.ops_rows() if r["phase"] != "accepted"]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("point", ["after_accept", "after_effect", "sigkill_after_accept"])
def test_ac48_accepted_only_request_ends_outcome_unknown_and_is_not_replayed(
        ops_env, endpoint, point):
    args = _prepare(ops_env, endpoint)
    _run_child(ops_env, endpoint, point)
    effects_before = _effects(ops_env, endpoint, args)
    assert effects_before == (1 if point == "after_effect" else 0)
    accepted = [r for r in ops_env.ops_rows() if r["phase"] == "accepted"]
    assert len(accepted) == 1 and _terminals(ops_env) == []

    service = ops_env.service()
    assert service.recover_after_journal() == [accepted[0]["id"]]
    terminal = _terminals(ops_env)
    assert [(t["phase"], t["result_code"], t["target_ref"]) for t in terminal] == [
        ("outcome_unknown", "outcome_unknown", f"audit:{accepted[0]['id']}")]
    # 2 度目の回復は何も足さない。
    assert service.recover_after_journal() == []
    assert len(_terminals(ops_env)) == 1
    if endpoint in IDEMPOTENT:
        with pytest.raises(OpsError) as raised:
            _resend(ops_env, service, endpoint, args)
        assert raised.value.code is ErrorCode.OUTCOME_UNKNOWN
        state = ops_env.conn.execute("SELECT state FROM ops_idempotency").fetchone()["state"]
        assert state == "outcome_unknown"
    assert _effects(ops_env, endpoint, args) == effects_before


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_ac48_crash_before_accept_leaves_nothing(ops_env, endpoint):
    args = _prepare(ops_env, endpoint)
    _run_child(ops_env, endpoint, "before_accept")
    assert ops_env.ops_rows() == []
    assert _effects(ops_env, endpoint, args) == 0
    assert ops_env.service().recover_after_journal() == []


@pytest.mark.parametrize("endpoint", IDEMPOTENT)
def test_ac48_crash_after_terminal_keeps_result_and_replays_it(ops_env, endpoint):
    args = _prepare(ops_env, endpoint)
    _run_child(ops_env, endpoint, "after_terminal")
    terminal = _terminals(ops_env)
    assert len(terminal) == 1 and terminal[0]["phase"] == "succeeded"
    service = ops_env.service()
    assert service.recover_after_journal() == []
    reply = _resend(ops_env, service, endpoint, args)
    assert reply == json.loads(terminal[0]["response_json"])
    assert _effects(ops_env, endpoint, args) == 1


def test_ac35_policy_crash_regenerates_one_line_from_records(ops_env):
    args = _prepare(ops_env, "policy")
    _run_child(ops_env, "policy", "after_effect")
    service = ops_env.service()
    service.recover_after_journal()
    with pytest.raises(OpsError):
        _resend(ops_env, service, "policy", args)
    service.regenerate_policy()
    assert ops_env.policy_path.read_text().splitlines().count("- protect capital") == 1


# ------------------------------------------------------- approve と journal


def _approve_child(env, point):
    approval_id = env.make_plugin_approval()
    (env.root / "args.json").write_text(json.dumps(
        {"approval_id": approval_id, "digest": digest_of(env, approval_id)}))
    _run_child(env, "approve", point)
    return approval_id


def _reconcile(env):
    # 子は実時刻で受理を記録する。再起動後の reconcile はそれより後の時刻に走る。
    from datetime import datetime, timezone
    switch.reconcile_switch_journals(env.conn, plugins_root=env.plugins_root,
                                     now=datetime.now(timezone.utc), settings=env.settings)


def test_ac50_journal_switched_crash_is_completed_by_reconcile_and_appended(ops_env):
    approval_id = _approve_child(ops_env, "after_switch_live")
    assert (ops_env.plugins_root / "sma").is_symlink()
    assert ops_env.approval(approval_id)["status"] == "pending"
    accepted = [r for r in ops_env.ops_rows() if r["phase"] == "accepted"]
    _reconcile(ops_env)
    assert ops_env.approval(approval_id)["decided_by"] == "system_reconcile"
    service = ops_env.service()
    assert service.recover_after_journal() == [accepted[0]["id"]]
    terminal = _terminals(ops_env)
    assert [(t["phase"], t["result_code"]) for t in terminal] == [("succeeded", "ok")]
    response = json.loads(terminal[0]["response_json"])
    assert response == {"approval_id": approval_id, "status": "approved",
                        "decided_by": "system_reconcile"}


@pytest.mark.parametrize("phase", ["preparing", "versioned", "recorded"])
def test_ac50_journal_phase_the_reconcile_cannot_decide_is_outcome_unknown(ops_env, phase):
    approval_id = _approve_child(ops_env, f"journal_{phase}")
    _reconcile(ops_env)
    journal_before = [tuple(r) for r in ops_env.conn.execute(
        "SELECT * FROM plugin_switch_journal").fetchall()]
    service = ops_env.service()
    service.recover_after_journal()
    assert [(t["phase"], t["result_code"]) for t in _terminals(ops_env)] == [
        ("outcome_unknown", "outcome_unknown")]
    # 回復は決定を再実行しない: approval は pending、journal も触らない。
    assert ops_env.approval(approval_id)["status"] == "pending"
    assert [tuple(r) for r in ops_env.conn.execute(
        "SELECT * FROM plugin_switch_journal").fetchall()] == journal_before
    assert not (ops_env.plugins_root / "sma").exists() or phase == "recorded"


def test_ac50_own_decision_done_but_terminal_missing_is_not_promoted_to_success(ops_env):
    approval_id = _approve_child(ops_env, "after_effect")
    row = ops_env.approval(approval_id)
    assert (row["status"], row["decided_by"]) == ("approved", "api:approver")
    _reconcile(ops_env)
    ops_env.service().recover_after_journal()
    assert [(t["phase"], t["result_code"]) for t in _terminals(ops_env)] == [
        ("outcome_unknown", "outcome_unknown")]


def test_ac50_journal_reverted_by_reconcile_is_recorded_as_not_decided(ops_env):
    # journal は switched だが live は切り替わる前 (旧状態 absent) に落ちた。
    approval_id = _approve_child(ops_env, "journal_switched")
    assert not os.path.lexists(ops_env.plugins_root / "sma")
    _reconcile(ops_env)
    assert ops_env.approval(approval_id)["status"] == "pending"
    ops_env.service().recover_after_journal()
    terminal = _terminals(ops_env)
    assert [(t["phase"], t["result_code"]) for t in terminal] == [("failed", "invalid_state")]
    assert json.loads(terminal[0]["response_json"])["journal"] == "reverted"


def test_ac48_recovered_terminal_is_projected_as_an_event(ops_env):
    service = ops_env.service()
    accepted = service.audit.accept(endpoint="POST /v1/backlog", principal=OPERATOR,
                                    asserted_actor=None, peer_pid=None, peer_exe=None,
                                    body={"idea": "x"}, target_ref="backlog")
    assert service.recover_after_journal() == [accepted.audit_id]
    events = service.events_after(OPERATOR, after=0, limit=10)["events"]
    assert [(e["code"], e["ref"], e["fields"]) for e in events] == [
        ("ops_request_terminal", f"audit:{accepted.audit_id}",
         {"endpoint_code": "backlog_add", "result_code": "outcome_unknown",
          "authenticated_principal": "operator"})]
