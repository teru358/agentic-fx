"""Deciding a re-evaluation request through the ops decision lane and the shell."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from agentic_fx.commands import Commands
from agentic_fx.core.contracts import SystemClock
from agentic_fx.ops import change_limits as L
from agentic_fx.plugin import deploy_ops, loader, switch
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store

from ._change_fixtures import insert_journal, make_live, make_version
from .conftest import APPROVER, OPERATOR, digest_of, wait_job

NAME = "sma_cross"
CONFIG = ("kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nexit_mode: levels\n"
          "params:\n  period: 14\n")
EPOCH = "time_repair_dst_v1"
THEN = datetime(2026, 11, 1, tzinfo=timezone.utc)


class Fixture:
    def __init__(self, env):
        self.env = env
        self.conn = env.conn
        env.conn.execute("INSERT INTO data_migrations(name, applied_at) VALUES (?, ?)",
                         (EPOCH, THEN.isoformat()))
        env.conn.commit()
        self.artifact = make_version(env.plugins_root, NAME, config=CONFIG)
        make_live(env.plugins_root, NAME, self.artifact)
        self.content = loader.content_hash(
            env.plugins_root / ".versions" / NAME / self.artifact)
        self.basis = approvals_store.create(
            env.conn, "plugin", {"name": NAME, "content_hash": self.content,
                                 "artifact_hash": self.artifact}, THEN)
        approvals_store.apply_decision(env.conn, self.basis, "approved",
                                       decided_by="human", now=THEN)
        insert_journal(env, self.basis, NAME, "absent", None, new_target=self.target)
        self.approval_id = approvals_store.create(
            env.conn, "plugin", self.payload(), THEN)

    @property
    def target(self):
        return f".versions/{NAME}/{self.artifact}"

    def payload(self):
        return {
            "name": NAME, "kind": "strategy", "content_hash": self.content,
            "artifact_hash": self.artifact, "candidate_origin": "reevaluation",
            "candidate_path": f"plugins/{self.target}",
            "in_sample": {}, "holdout": {}, "run_ids": {},
            "history_epoch": switch.history_epoch(self.conn),
            "eval_now": THEN.isoformat(), "reevaluates_approval_id": self.basis,
            "basis_decision_id": self.basis,
            "deploy_generation": switch.deploy_generation(self.conn, NAME),
            "previous": {}, "reason": "history_time_repaired", "indicator_deps": {},
        }

    def row(self):
        return dict(self.env.approval(self.approval_id))

    def live(self):
        live = self.env.plugins_root / NAME
        return os.readlink(live) if live.is_symlink() else None

    def events(self):
        return [(r["code"], json.loads(r["fields_json"])) for r in self.conn.execute(
            "SELECT code, fields_json FROM ops_events ORDER BY id")
            if r["code"] == "approval_state_changed"]

    def stale(self):
        (self.env.plugins_root / NAME).unlink()


@pytest.fixture
def fx(ops_env):
    return Fixture(ops_env)


def _run(env, service, kind, approval_id, reason=None):
    if kind == "reject":
        reply = service.reject(APPROVER, approval_id, reason)
    else:
        digest = digest_of(env, approval_id)
        reply = getattr(service, kind)(APPROVER, approval_id, digest)
    return wait_job(service, reply["job_id"])


def _shell(env, service):
    from agentic_fx.activity import ActivityLog
    commands = Commands(conn=env.conn, state_store=env.state, broker=None,
                        trade_loop=None, activity=ActivityLog(env.activity_path),
                        log_dir=env.root / "logs", clock=SystemClock(),
                        plugins_root=env.plugins_root, settings=env.settings)
    commands.use_ops_service(service)
    return commands


def _activity_events(env):
    text = env.activity_path.read_text() if env.activity_path.exists() else ""
    return [line.split("\t")[2] for line in text.splitlines() if line.count("\t") >= 4]


def test_approve_through_the_lane_reports_the_confirmation(fx, ops_env):
    service = ops_env.service()
    job = _run(ops_env, service, "approve", fx.approval_id)
    assert job["state"] == "done"
    assert job["result"] == {"approval_id": fx.approval_id,
                             "outcome": "reevaluation_confirmed", "status": "approved"}
    assert fx.row()["status"] == "approved" and fx.live() == fx.target
    (_, fields), = fx.events()
    assert (fields["state"], fields["decision_code"], fields["candidate_origin"]) == (
        "approved", "approve", "reevaluation")
    assert "deploy_phase" not in fields


def test_reject_through_the_lane_stops_the_deployment(fx, ops_env):
    service = ops_env.service()
    job = _run(ops_env, service, "reject", fx.approval_id, "no")
    assert job["state"] == "done"
    assert job["result"] == {"approval_id": fx.approval_id, "outcome": "rejected",
                             "status": "rejected", "deploy_phase": "done"}
    assert fx.row()["status"] == "rejected" and fx.live() is None
    (_, fields), = fx.events()
    assert (fields["state"], fields["decision_code"], fields["deploy_phase"],
            fields["candidate_origin"]) == ("rejected", "reject", "done", "reevaluation")


@pytest.mark.parametrize("phase", ["needs_retry", "foreign"])
def test_reject_through_the_lane_reports_a_stop_that_did_not_finish(fx, ops_env, monkeypatch,
                                                                      phase):
    if phase == "needs_retry":
        real = os.unlink

        def failing(path, *args, **kwargs):
            if os.path.basename(str(path)) == NAME:
                raise OSError("denied")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", failing)
    else:
        monkeypatch.setattr(deploy_ops, "live_matches_from", lambda *_a, **_k: False)
    job = _run(ops_env, ops_env.service(), "reject", fx.approval_id)
    assert job["state"] == "done"
    assert job["result"]["deploy_phase"] == phase and job["result"]["status"] == "rejected"
    assert fx.events()[0][1]["deploy_phase"] == phase
    assert fx.row()["status"] == "rejected" and fx.live() == fx.target


@pytest.mark.parametrize("kind", ["approve", "reject"])
def test_a_stale_request_ends_as_invalidated_for_both_decisions(fx, ops_env, kind):
    fx.stale()
    job = _run(ops_env, ops_env.service(), kind, fx.approval_id)
    assert job["state"] == "done"
    assert job["result"] == {"approval_id": fx.approval_id, "outcome": "invalidated",
                             "status": "invalidated", "reason": "reevaluation_stale"}
    row = fx.row()
    assert (row["status"], row["reason"]) == ("invalidated", "reevaluation_stale")
    (_, fields), = fx.events()
    assert fields["state"] == "invalidated" and fields["candidate_origin"] == "reevaluation"
    assert "deploy_phase" not in fields


@pytest.mark.parametrize("kind", ["approve", "reject"])
def test_a_changed_epoch_is_invalid_state_and_leaves_the_request_pending(fx, ops_env, kind):
    ops_env.conn.execute("INSERT INTO data_migrations(name, applied_at) VALUES (?, ?)",
                         ("time_repair_next", datetime(2026, 11, 2).isoformat()))
    ops_env.conn.commit()
    job = _run(ops_env, ops_env.service(), kind, fx.approval_id)
    assert (job["state"], job["result_code"]) == ("failed", "invalid_state")
    assert job["result"]["status"] == "pending"
    assert fx.row()["status"] == "pending" and fx.live() == fx.target
    assert fx.events() == []


def test_retry_through_the_lane_is_refused_and_changes_nothing(fx, ops_env):
    before = (fx.row(), fx.live(),
              [tuple(r) for r in ops_env.conn.execute("SELECT * FROM approval_requests")],
              [tuple(r) for r in ops_env.conn.execute("SELECT * FROM plugin_switch_journal")],
              [tuple(r) for r in ops_env.conn.execute("SELECT * FROM plugin_deploy_ops")])
    job = _run(ops_env, ops_env.service(), "retry", fx.approval_id)
    assert (job["state"], job["result_code"]) == ("failed", "invalid_state")
    assert job["result"]["status"] == "pending"
    after = (fx.row(), fx.live(),
             [tuple(r) for r in ops_env.conn.execute("SELECT * FROM approval_requests")],
             [tuple(r) for r in ops_env.conn.execute("SELECT * FROM plugin_switch_journal")],
             [tuple(r) for r in ops_env.conn.execute("SELECT * FROM plugin_deploy_ops")])
    assert after == before


def test_retry_through_the_shell_is_refused_and_changes_nothing(fx, ops_env):
    shell = _shell(ops_env, ops_env.service())
    text = shell.dispatch(f"approval retry {fx.approval_id}")
    assert "reevaluation_retry_unsupported" in text
    assert fx.row()["status"] == "pending" and fx.live() == fx.target


def test_an_ordinary_reject_that_returns_false_is_still_invalid_state(ops_env, monkeypatch):
    service = ops_env.service()
    approval_id = ops_env.make_plugin_approval()
    monkeypatch.setattr(switch, "reject_candidate", lambda *_a, **_k: False)
    job = _run(ops_env, service, "reject", approval_id)
    assert (job["state"], job["result_code"]) == ("failed", "invalid_state")
    assert ops_env.approval(approval_id)["status"] == "pending"


def test_an_ordinary_reject_still_reports_rejected_without_a_deploy_phase(ops_env):
    approval_id = ops_env.make_plugin_approval()
    job = _run(ops_env, ops_env.service(), "reject", approval_id, "no")
    assert job["result"] == {"approval_id": approval_id, "outcome": "rejected",
                             "status": "rejected"}
    event = [f for c, f in [(r["code"], json.loads(r["fields_json"])) for r in
             ops_env.conn.execute("SELECT code, fields_json FROM ops_events")]
             if c == "approval_state_changed"][-1]
    assert "candidate_origin" not in event and "deploy_phase" not in event


@pytest.mark.parametrize("phase, expected", [
    ("done", "却下し、配備を停止しました"),
    ("needs_retry", "symlink を外せていません"),
    ("foreign", "外していません。確認してください"),
])
def test_shell_reject_text_and_activity_follow_the_result(fx, ops_env, monkeypatch, phase,
                                                          expected):
    if phase == "needs_retry":
        real = os.unlink
        monkeypatch.setattr(os, "unlink", lambda p, *a, **k: (_ for _ in ()).throw(
            OSError("denied")) if os.path.basename(str(p)) == NAME else real(p, *a, **k))
    elif phase == "foreign":
        monkeypatch.setattr(deploy_ops, "live_matches_from", lambda *_a, **_k: False)
    shell = _shell(ops_env, ops_env.service())
    text = shell.dispatch(f"reject {fx.approval_id}")
    assert expected in text
    events = _activity_events(ops_env)
    assert "reevaluation_rejected" in events and "rejected" not in events


def test_shell_reject_of_a_stale_request_does_not_say_rejected(fx, ops_env):
    fx.stale()
    text = _shell(ops_env, ops_env.service()).dispatch(f"reject {fx.approval_id}")
    assert "却下" not in text and "古いため無効" in text
    events = _activity_events(ops_env)
    assert "reevaluation_invalidated" in events and "rejected" not in events


def test_shell_reject_without_the_ops_lane_follows_the_result_too(fx, ops_env):
    from agentic_fx.activity import ActivityLog
    commands = Commands(conn=ops_env.conn, state_store=ops_env.state, broker=None,
                        trade_loop=None, activity=ActivityLog(ops_env.activity_path),
                        log_dir=ops_env.root / "logs", clock=SystemClock(),
                        plugins_root=ops_env.plugins_root, settings=ops_env.settings)
    text = commands.dispatch(f"reject {fx.approval_id}")
    assert "却下し、配備を停止しました" in text
    assert fx.live() is None
    events = _activity_events(ops_env)
    assert "reevaluation_rejected" in events and "rejected" not in events


def test_shell_approve_writes_the_confirmation_not_a_plain_approval(fx, ops_env):
    text = _shell(ops_env, ops_env.service()).dispatch(f"approve {fx.approval_id}")
    assert text.startswith(f"approval #{fx.approval_id} approved: ")
    events = _activity_events(ops_env)
    assert "reevaluation_approved" in events and "approved" not in events


def test_the_approval_detail_reads_the_deployed_version_as_unchanged(fx, ops_env):
    detail = ops_env.service().approval_detail(OPERATOR, fx.approval_id)
    change = detail["change"]
    assert change["reason"] is None
    assert change["classification"] == L.CLASS_SAME
    assert change["candidate_hash"] == fx.artifact


@pytest.mark.parametrize("status", ["approved", "rejected", "invalidated", "expired"])
def test_a_decided_reevaluation_without_a_journal_is_classified_as_the_same_version(
        fx, ops_env, status):
    approvals_store.apply_decision(ops_env.conn, fx.approval_id, status,
                                   decided_by="human", now=THEN)

    change = ops_env.service().approval_detail(OPERATOR, fx.approval_id)["change"]

    assert change["classification"] == L.CLASS_SAME
    assert change["reason"] is None and change["baseline_hash"] == fx.artifact


def test_a_decided_reevaluation_shows_the_same_unchanged_diff_as_a_pending_one(fx, ops_env):
    pending = ops_env.service().approval_change(OPERATOR, fx.approval_id)["diff"]
    approvals_store.apply_decision(ops_env.conn, fx.approval_id, "approved",
                                   decided_by="human", now=THEN)

    decided = ops_env.service().approval_change(OPERATOR, fx.approval_id)["diff"]

    assert pending["files"]
    assert [(f["path"], f["status"]) for f in decided["files"]] == [
        (f["path"], f["status"]) for f in pending["files"]]
    assert all(f["status"] == "unchanged" for f in decided["files"])


def test_a_decided_ordinary_approval_without_a_journal_stays_unknown(ops_env):
    approval_id = ops_env.make_plugin_approval()
    approvals_store.apply_decision(ops_env.conn, approval_id, "rejected",
                                   decided_by="human", now=THEN)

    change = ops_env.service().approval_detail(OPERATOR, approval_id)["change"]

    assert change["classification"] == L.CLASS_UNKNOWN


def test_the_approval_detail_refuses_a_locator_that_is_not_the_canonical_path(fx, ops_env):
    payload = {**fx.payload(), "candidate_path": f"plugins/.versions/{NAME}/{'f' * 64}"}
    ops_env.conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                         (json.dumps(payload), fx.approval_id))
    ops_env.conn.commit()
    change = ops_env.service().approval_detail(OPERATOR, fx.approval_id)["change"]
    assert change["reason"] == L.UNSAFE_SOURCE


def test_a_restart_leaves_a_pending_reevaluation_request_pending(fx, ops_env):
    db_store.init_db(ops_env.conn)
    assert fx.row()["status"] == "pending"
