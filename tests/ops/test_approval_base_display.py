"""承認時の土台照合の見え方: ops の結果と詳細 (base_check)、shell と ctl の文面。"""
from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest

from agentic_fx.ops import base_view, client
from agentic_fx.ops.contracts import Principal
from agentic_fx.plugin import switch

from .test_ctl_pins import JOB, _ok, _StubApi
from .test_reevaluation_decision_lane import OPERATOR, _run, _shell

NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)
TEXT = "土台の版が配備中の版と違うため配備しませんでした。配備中の版から作り直して提出してください"


def _deploy(ops_env, approval_id: int) -> None:
    switch.approve_candidate(ops_env.conn, approval_id, decided_by="h", now=NOW,
                             plugins_root=ops_env.plugins_root, settings=ops_env.settings)


@pytest.fixture
def two_candidates(ops_env):
    """v1 を配備した後、同じ v1 から作った候補 first・second。"""
    _deploy(ops_env, ops_env.make_plugin_approval(value=1.0, mission_id=1))
    first = ops_env.make_plugin_approval(value=2.0, mission_id=2)
    second = ops_env.make_plugin_approval(value=3.0, mission_id=3)
    return first, second


def test_the_detail_has_no_warning_state_for_a_matching_base(ops_env, two_candidates):
    first, _second = two_candidates

    detail = ops_env.service().approval_detail(OPERATOR, first)

    assert detail["base_check"]["state"] == "match"
    assert base_view.lines(detail["base_check"]) == []


def test_the_detail_reports_a_mismatch_with_both_short_hashes(ops_env, two_candidates):
    first, second = two_candidates
    _deploy(ops_env, first)

    detail = ops_env.service().approval_detail(OPERATOR, second)

    view = detail["base_check"]
    assert view["state"] == "mismatch"
    assert len(view["base"]) == 12 and len(view["live"]) == 12 and view["base"] != view["live"]
    assert base_view.lines(view)[0].startswith("警告: この候補の土台は配備中の版と違います (土台 ")
    assert base_view.lines(view)[0].endswith("このままでは配備できません")


def test_the_detail_of_a_decided_request_has_no_base_check(ops_env, two_candidates):
    first, _second = two_candidates
    _deploy(ops_env, first)

    assert ops_env.service().approval_detail(OPERATOR, first)["base_check"] is None


def test_the_detail_reports_none_but_live_and_unreadable(ops_env):
    # first は live が無い間に作られた (土台なし)。その後に別の版が配備された。
    first = ops_env.make_plugin_approval(value=2.0, mission_id=2)
    _deploy(ops_env, ops_env.make_plugin_approval(value=1.0, mission_id=1))
    other = ops_env.make_plugin_approval(value=5.0, mission_id=5)

    view = ops_env.service().approval_detail(OPERATOR, first)["base_check"]
    assert view["state"] == "none_but_live"
    assert base_view.lines(view) == [
        "警告: この候補は同名の配備中の版を見ずに作られました。このままでは配備できません"]

    live = ops_env.plugins_root / "sma"
    live.unlink()
    live.symlink_to("../elsewhere/sma")
    assert ops_env.service().approval_detail(OPERATOR, other)["base_check"][
        "state"] == "unreadable"
    assert base_view.lines({"state": "unreadable"}) == ["警告: 配備中の版を確かめられません"]


@pytest.mark.parametrize("state", ["match", "legacy", "unchecked", None])
def test_no_line_is_shown_for_match_legacy_and_unchecked(state):
    assert base_view.lines({"state": state, "base": None, "live": None}) == []
    assert base_view.lines(None) == []


def test_the_detail_shows_nothing_for_a_request_without_base_or_a_blessed_one(ops_env):
    approval_id = ops_env.make_plugin_approval(value=2.0, mission_id=2)
    ops_env.conn.execute(
        "UPDATE approval_requests SET payload_json=json_remove(payload_json,'$.base') "
        "WHERE id=?", (approval_id,))
    ops_env.conn.commit()
    assert ops_env.service().approval_detail(OPERATOR, approval_id)["base_check"][
        "state"] == "legacy"
    ops_env.conn.execute(
        "UPDATE approval_requests SET payload_json=json_set(payload_json,'$.base',"
        "json('{\"kind\":\"unchecked\",\"by\":\"bless\"}')) WHERE id=?", (approval_id,))
    ops_env.conn.commit()
    assert ops_env.service().approval_detail(OPERATOR, approval_id)["base_check"][
        "state"] == "unchecked"


@pytest.mark.parametrize("kind", ["approve", "retry"])
def test_the_ops_result_carries_base_changed(ops_env, two_candidates, kind):
    first, second = two_candidates
    _deploy(ops_env, first)

    job = _run(ops_env, ops_env.service(), kind, second)

    assert job["state"] == "done"
    assert job["result"] == {"approval_id": second, "outcome": "invalidated",
                             "status": "invalidated", "reason": "base_changed"}


def test_the_shell_detail_shows_the_warning_for_a_request_without_comparison(
        ops_env, two_candidates):
    first, second = two_candidates
    _deploy(ops_env, first)

    text = _shell(ops_env, ops_env.service()).dispatch(f"approval {second}")

    assert "警告: この候補の土台は配備中の版と違います" in text
    assert "このままでは配備できません" in text


def test_the_shell_detail_shows_no_warning_when_the_base_matches(ops_env, two_candidates):
    first, _second = two_candidates

    text = _shell(ops_env, ops_env.service()).dispatch(f"approval {first}")

    assert "土台" not in text


def test_the_shell_approve_says_why_it_was_not_deployed(ops_env, two_candidates):
    first, second = two_candidates
    _deploy(ops_env, first)

    text = _shell(ops_env, ops_env.service()).dispatch(f"approve {second}")

    assert TEXT in text
    assert "既に決着" not in text


def test_the_shell_without_the_ops_lane_says_it_too(ops_env, two_candidates):
    from agentic_fx.activity import ActivityLog
    from agentic_fx.commands import Commands
    from agentic_fx.core.contracts import SystemClock
    first, second = two_candidates
    _deploy(ops_env, first)
    shell = Commands(conn=ops_env.conn, state_store=ops_env.state, broker=None,
                     trade_loop=None, activity=ActivityLog(ops_env.activity_path),
                     log_dir=ops_env.root / "logs", clock=SystemClock(),
                     plugins_root=ops_env.plugins_root, settings=ops_env.settings)

    assert TEXT in shell.dispatch(f"approve {second}")


def test_the_ctl_approve_says_why_it_was_not_deployed(tmp_path):
    api = _StubApi(tmp_path, [_ok({"job_id": JOB, "state": "queued"}),
                              _ok({"id": JOB, "state": "done", "result": {
                                  "approval_id": 7, "outcome": "invalidated",
                                  "status": "invalidated", "reason": "base_changed"}})],
                   principal=Principal.APPROVER)
    out = io.StringIO()

    client.Runner(api, stdin=io.StringIO(""), stdout=out)._decide("approve", 7, {})

    assert out.getvalue().strip().splitlines()[-1] == f"approval #7 {TEXT}"


def test_the_ctl_retry_says_why_too(tmp_path):
    api = _StubApi(tmp_path, [_ok({"job_id": JOB, "state": "queued"}),
                              _ok({"id": JOB, "state": "done", "result": {
                                  "approval_id": 7, "outcome": "invalidated",
                                  "status": "invalidated", "reason": "base_changed"}})],
                   principal=Principal.APPROVER)
    out = io.StringIO()

    client.Runner(api, stdin=io.StringIO(""), stdout=out)._decide("retry", 7, {})

    assert out.getvalue().strip().splitlines()[-1] == f"approval #7 {TEXT}"


def test_the_ctl_detail_shows_the_warning_and_stays_silent_for_a_match():
    base = {"id": 7, "kind": "plugin", "status": "pending", "payload_sha256": "ab" * 32}
    warned = client.format_approval_detail({**base, "base_check": {
        "state": "mismatch", "base": "a" * 12, "live": "none"}})
    assert ("警告: この候補の土台は配備中の版と違います (土台 aaaaaaaaaaaa / 配備中 無し)。"
            "このままでは配備できません") in warned.split("\n")
    quiet = client.format_approval_detail({**base, "base_check": {
        "state": "match", "base": "a" * 12, "live": "a" * 12}})
    assert "土台" not in quiet
    assert "土台" not in client.format_approval_detail(base)
