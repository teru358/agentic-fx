"""履歴証跡で止まった承認の結果が、shell・ctl・ops・通知の全表示面に固定 reason と次の操作で出る。"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

from agentic_fx import improve_notice_codes as codes
from agentic_fx.ops import client
from agentic_fx.ops.contracts import Principal
from agentic_fx.plugin import history_reasons, switch

from .test_ctl_pins import JOB, _ok, _StubApi
from .test_reevaluation_decision_lane import Fixture, OPERATOR, _run, _shell

REASONS = (history_reasons.EVIDENCE_INVALIDATED, history_reasons.EVIDENCE_REFRESH_REQUIRED,
           history_reasons.CHECK_UNAVAILABLE)


def _afx_ops_module(name: str):
    path = Path(__file__).resolve().parents[2] / "afx-ops" / "afx_ops" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"afx_ops_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    return module


@pytest.fixture
def fx(ops_env):
    return Fixture(ops_env)


def _break_reevaluation(fx):
    payload = json.loads(fx.row()["payload_json"])
    payload.pop("history_guard")
    fx.conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                    (json.dumps(payload), fx.approval_id))
    fx.conn.commit()


def _ordinary_without_evidence(ops_env) -> int:
    return ops_env.raw_approval("plugin", {
        "name": "cand", "kind": "strategy", "candidate_origin": "staging",
        "candidate_path": "plugins/_staging/1/cand", "content_hash": "c" * 64,
        "artifact_hash": "a" * 64, "history_epoch": switch.history_epoch(ops_env.conn)})


def test_every_reason_has_its_text_and_the_afx_ops_copy_matches():
    other = _afx_ops_module("history_text")
    assert set(history_reasons.TEXT) == set(REASONS) == set(other.TEXT)
    for reason in REASONS:
        assert other.TEXT[reason] == history_reasons.TEXT[reason]
        assert history_reasons.text_for(reason) == other.text_for(reason)
    assert history_reasons.text_for("nope") is None and other.text_for(None) is None
    assert "plugins/_human/<name>" in history_reasons.TEXT[history_reasons.EVIDENCE_INVALIDATED]
    assert "afx plugin reevaluate --all-deployed --yes" in history_reasons.TEXT[
        history_reasons.EVIDENCE_REFRESH_REQUIRED]
    assert "afx status" in history_reasons.TEXT[history_reasons.CHECK_UNAVAILABLE]


def test_the_outcome_is_an_allowed_approval_outcome():
    assert history_reasons.OUTCOME in switch.APPROVAL_OUTCOMES


# ---- ops (job の結果と詳細) -------------------------------------------------------

def test_the_ops_result_of_an_invalidated_ordinary_request_is_done_with_the_reason(ops_env):
    approval_id = _ordinary_without_evidence(ops_env)
    job = _run(ops_env, ops_env.service(), "approve", approval_id)
    assert job["state"] == "done"
    assert job["result"] == {"approval_id": approval_id, "outcome": history_reasons.OUTCOME,
                             "status": "invalidated",
                             "reason": history_reasons.EVIDENCE_INVALIDATED}
    detail = ops_env.service().approval_detail(OPERATOR, approval_id)
    assert (detail["status"], detail["reason"]) == (
        "invalidated", history_reasons.EVIDENCE_INVALIDATED)


def test_the_ops_result_of_a_held_reevaluation_keeps_it_pending_with_the_reason(fx, ops_env):
    _break_reevaluation(fx)
    job = _run(ops_env, ops_env.service(), "approve", fx.approval_id)
    assert job["state"] == "failed"
    assert job["result"] == {"approval_id": fx.approval_id, "outcome": history_reasons.OUTCOME,
                             "status": "pending",
                             "reason": history_reasons.EVIDENCE_REFRESH_REQUIRED}
    assert fx.row()["status"] == "pending" and fx.live() == fx.target
    detail = ops_env.service().approval_detail(OPERATOR, fx.approval_id)
    assert (detail["status"], detail["reason"]) == (
        "pending", history_reasons.EVIDENCE_REFRESH_REQUIRED)


# ---- shell -------------------------------------------------------------------------

def test_the_shell_shows_the_text_for_each_outcome(fx, ops_env):
    _break_reevaluation(fx)
    text = _shell(ops_env, ops_env.service()).dispatch(f"approve {fx.approval_id}")
    assert history_reasons.TEXT[history_reasons.EVIDENCE_REFRESH_REQUIRED] in text
    assert "status=pending" in text and "None" not in text
    invalidated = _ordinary_without_evidence(ops_env)
    text = _shell(ops_env, ops_env.service()).dispatch(f"approve {invalidated}")
    assert history_reasons.TEXT[history_reasons.EVIDENCE_INVALIDATED] in text
    assert "既に決着" not in text


def test_the_shell_without_the_ops_lane_shows_it_too(ops_env):
    from agentic_fx.activity import ActivityLog
    from agentic_fx.commands import Commands
    from agentic_fx.core.contracts import SystemClock
    approval_id = _ordinary_without_evidence(ops_env)
    shell = Commands(conn=ops_env.conn, state_store=ops_env.state, broker=None,
                     trade_loop=None, activity=ActivityLog(ops_env.activity_path),
                     log_dir=ops_env.root / "logs", clock=SystemClock(),
                     plugins_root=ops_env.plugins_root, settings=ops_env.settings)
    assert history_reasons.TEXT[history_reasons.EVIDENCE_INVALIDATED] in shell.dispatch(
        f"approve {approval_id}")


def test_an_unknown_history_reason_fails_loudly_in_the_shell(ops_env):
    from agentic_fx.activity import ActivityLog
    from agentic_fx.commands import Commands
    from agentic_fx.core.contracts import SystemClock
    shell = Commands(conn=ops_env.conn, state_store=ops_env.state, broker=None,
                     trade_loop=None, activity=ActivityLog(ops_env.activity_path),
                     log_dir=ops_env.root / "logs", clock=SystemClock(),
                     plugins_root=ops_env.plugins_root, settings=ops_env.settings)
    with pytest.raises(ValueError):
        shell._approval_outcome_text(switch.ApprovalOutcome(
            outcome=history_reasons.OUTCOME, name="x", status="pending", reason=None))


# ---- ctl ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["approve", "retry"])
def test_the_ctl_shows_the_text_for_a_held_request(tmp_path, kind):
    api = _StubApi(tmp_path, [_ok({"job_id": JOB, "state": "queued"}),
                              _ok({"id": JOB, "state": "failed", "result_code": "invalid_state",
                                   "result": {"approval_id": 7, "outcome": history_reasons.OUTCOME,
                                              "status": "pending",
                                              "reason": history_reasons.CHECK_UNAVAILABLE}})],
                   principal=Principal.APPROVER)
    out = io.StringIO()
    with pytest.raises(client.ClientError) as caught:
        client.Runner(api, stdin=io.StringIO(""), stdout=out)._decide(kind, 7, {})
    assert history_reasons.TEXT[history_reasons.CHECK_UNAVAILABLE] in caught.value.message
    assert "status=pending" in caught.value.message


def test_the_ctl_shows_the_text_for_an_invalidated_request(tmp_path):
    api = _StubApi(tmp_path, [_ok({"job_id": JOB, "state": "queued"}),
                              _ok({"id": JOB, "state": "done", "result": {
                                  "approval_id": 7, "outcome": history_reasons.OUTCOME,
                                  "status": "invalidated",
                                  "reason": history_reasons.EVIDENCE_INVALIDATED}})],
                   principal=Principal.APPROVER)
    out = io.StringIO()
    client.Runner(api, stdin=io.StringIO(""), stdout=out)._decide("approve", 7, {})
    assert out.getvalue().strip().splitlines()[-1] == (
        f"approval #7 {history_reasons.TEXT[history_reasons.EVIDENCE_INVALIDATED]}")
    assert "approved" not in out.getvalue()


# ---- 改善の通知 ----------------------------------------------------------------------

def test_the_history_notice_pair_is_allowed_and_the_three_kinds_map_to_it():
    codes.validate_pair("gate_failed", "history_not_ready")
    assert codes.NOTICE_HISTORY_NOT_READY in codes._ALL_NOTICES
    for kind in ("history_stale", "history_gap", "history_check_unavailable"):
        assert codes.notice_for_unevaluable_kind(kind) is codes.NOTICE_HISTORY_NOT_READY
    with pytest.raises(codes.UnknownNoticeCode):
        codes.notice_for_unevaluable_kind("history_unknown")
    assert ("gate_failed", "history_not_ready") in codes.PAIRS_WITH_DEDICATED_HEADING


def test_the_discord_wording_is_the_specified_one_and_not_an_agent_text():
    path = Path(__file__).resolve().parents[2] / "afx-ops" / "afx_ops" / "gate" / "improve_render.py"
    text = path.read_text(encoding="utf-8")
    assert "履歴条件を満たさないため提出しませんでした。`afx status` で確認し、" in text
    assert "履歴補充後に候補を人間submitしてください" in text
    from agentic_fx.loops import improve_context
    assert "history" not in improve_context.NOT_SUBMITTED_LAST_RESULT
