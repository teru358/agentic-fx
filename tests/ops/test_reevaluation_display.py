"""再評価の依頼の見え方: 承認の詳細 (ops / shell / ctl)、却下の確認文と結果の文言。"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

from agentic_fx.loops import approval_facts
from agentic_fx.core.display_time import effective_tz_name
from agentic_fx.ops import client, reevaluation_view
from agentic_fx.ops.contracts import Principal
from agentic_fx.store import approvals as approvals_store

from ._change_fixtures import insert_journal
from .test_ctl_pins import JOB, _ok, _StubApi, _Yes
from .test_reevaluation_decision_lane import (  # noqa: F401
    NAME, OPERATOR, Fixture, _run, _shell, fx)

M = {"pf": 1.25, "trades": 40, "avg_r": 0.12, "max_drawdown": 0.08}
OLD = {"pf": 1.5, "trades": 30, "avg_r": 0.2, "max_drawdown": 0.05}
EVAL_NOW = "2026-11-08T03:17:00+00:00"


def _with_comparison(fx, ops_env, **extra):
    payload = {**fx.payload(), "pairs": ["USDJPY", "EURUSD"],
               "in_sample": {"USDJPY": M, "EURUSD": M}, "holdout": {"USDJPY": M},
               "eval_now": EVAL_NOW,
               "previous": {
                   "approval_id": fx.basis,
                   "in_sample": {"USDJPY": {"metrics": OLD, "source": "payload", "run_id": None},
                                 "EURUSD": {"metrics": OLD, "source": "payload", "run_id": None}},
                   "holdout": {"USDJPY": {"metrics": OLD, "source": "run", "run_id": 55},
                               "EURUSD": {"metrics": None, "source": "unknown", "run_id": None}}},
               **extra}
    ops_env.conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                         (json.dumps(payload), fx.approval_id))
    ops_env.conn.commit()
    return payload


# ---------------------------------------------------------------- 詳細の材料 (ops)

def test_the_detail_of_a_reevaluation_carries_the_origin_and_a_comparison_per_pair(fx, ops_env):
    _with_comparison(fx, ops_env)

    detail = ops_env.service().approval_detail(OPERATOR, fx.approval_id)

    assert detail["candidate_origin"] == "reevaluation" and detail["deploy_phase"] is None
    view = detail["reevaluation"]
    assert (view["name"], view["reevaluates_approval_id"], view["eval_now"]) == (
        NAME, fx.basis, EVAL_NOW)
    assert view["previous_at"] is not None
    by_key = {(r["pair"], r["scope"]): r for r in view["rows"]}
    assert set(by_key) == {("USDJPY", "in_sample"), ("USDJPY", "holdout"),
                           ("EURUSD", "in_sample"), ("EURUSD", "holdout")}
    assert by_key["USDJPY", "holdout"]["previous"] == {"metrics": OLD, "source": "run",
                                                       "run_id": 55}
    assert by_key["USDJPY", "in_sample"]["previous"]["source"] == "payload"
    assert by_key["EURUSD", "holdout"]["previous"] == {"metrics": None, "source": "unknown",
                                                       "run_id": None}
    assert by_key["USDJPY", "holdout"]["current"] == M
    assert by_key["EURUSD", "holdout"]["current"] is None


def test_an_ordinary_approval_detail_has_no_reevaluation_field(ops_env):
    approval_id = ops_env.make_plugin_approval()

    detail = ops_env.service().approval_detail(OPERATOR, approval_id)

    assert not {"candidate_origin", "reevaluation", "deploy_phase"} & set(detail)


def test_an_ordinary_approval_detail_does_not_build_a_reevaluation_view(ops_env, monkeypatch):
    approval_id = ops_env.make_plugin_approval()
    calls = []
    monkeypatch.setattr(reevaluation_view, "build_view",
                        lambda *args: calls.append(args))

    ops_env.service().approval_detail(OPERATOR, approval_id)

    assert calls == []


def test_a_view_keeps_only_finite_numbers_and_known_sources(fx, ops_env):
    payload = _with_comparison(fx, ops_env)
    payload["in_sample"]["USDJPY"] = {"pf": "x", "trades": True, "avg_r": float("inf"),
                                      "max_drawdown": 0.1, "extra": "secret"}
    payload["previous"]["in_sample"]["USDJPY"]["source"] = "<script>"
    ops_env.conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                         (json.dumps(payload), fx.approval_id))
    ops_env.conn.commit()

    view = reevaluation_view.build_view(ops_env.conn, payload)

    row = next(r for r in view["rows"] if (r["pair"], r["scope"]) == ("USDJPY", "in_sample"))
    assert row["current"] == {"max_drawdown": 0.1}
    assert row["previous"]["source"] == "payload" or row["previous"]["metrics"] is not None
    assert "<script>" not in json.dumps(view) and "secret" not in json.dumps(view)


@pytest.mark.parametrize("deploy_phase", ["done"])
def test_a_rejected_request_reports_how_far_the_stop_went(fx, ops_env, deploy_phase):
    _run(ops_env, ops_env.service(), "reject", fx.approval_id)

    detail = ops_env.service().approval_detail(OPERATOR, fx.approval_id)

    assert detail["status"] == "rejected" and detail["deploy_phase"] == deploy_phase


def test_a_later_disable_of_the_same_version_does_not_change_the_rejected_requests_phase(
        fx, ops_env):
    from agentic_fx.store import plugin_deploy_ops
    _run(ops_env, ops_env.service(), "reject", fx.approval_id)
    plugin_deploy_ops.insert_intent(
        ops_env.conn, op_kind="disable_symlink", name=NAME, from_kind="symlink",
        to_kind="absent", from_target=fx.target, to_target=None, retired_path=None,
        subject_content_hash=fx.content, subject_artifact_hash=fx.artifact,
        target_content_hash=None, target_artifact_hash=None, approval_id=fx.basis,
        revoke=False, revoke_row_id=None, force=False, actor="human_cli",
        now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    ops_env.conn.execute("UPDATE plugin_deploy_ops SET phase='needs_retry', "
                         "fail_reason='syscall_failed' WHERE op_id=("
                         "SELECT MAX(op_id) FROM plugin_deploy_ops)")
    ops_env.conn.commit()

    detail = ops_env.service().approval_detail(OPERATOR, fx.approval_id)

    assert detail["deploy_phase"] == "done"


# ---------------------------------------------------------------- 数値の丸め・事実表の欄

ROUGH = {"pf": 1.0165433487012487, "trades": 40, "avg_r": 0.123456789,
         "max_drawdown": 0.14966326623788673}


def test_the_comparison_cells_round_floats_to_four_significant_digits_and_keep_ints():
    row = {"pair": "USDJPY", "scope": "holdout",
           "previous": {"metrics": ROUGH, "source": "payload", "run_id": None},
           "current": {"pf": 1.0165433487012487, "max_drawdown": 0.14966326623788673,
                       "trades": 7}}

    text = reevaluation_view.row_text(row)

    assert "pf=1.017 trades=40 avg_r=0.1235 max_drawdown=0.1497 (承認時の記録)" in text
    assert "今回 pf=1.017 trades=7 avg_r=- max_drawdown=0.1497" in text
    assert "1.0165" not in text and "0.14966" not in text


def test_the_shell_detail_rounds_the_comparison(fx, ops_env):
    _with_comparison(fx, ops_env)
    payload = json.loads(ops_env.conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (fx.approval_id,)).fetchone()[0])
    payload["in_sample"]["USDJPY"] = ROUGH
    ops_env.conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                         (json.dumps(payload), fx.approval_id))
    ops_env.conn.commit()

    text = _shell(ops_env, ops_env.service()).dispatch(f"approval {fx.approval_id}")

    comparison = [line for line in text.split("\n") if "前回" in line and "今回" in line]
    assert any("今回 pf=1.017 trades=40 avg_r=0.1235 max_drawdown=0.1497" in line
               for line in comparison)
    assert not any("1.0165433" in line for line in comparison)


def test_a_reevaluation_request_has_no_facts_lines():
    payload = {"candidate_origin": "reevaluation", "name": NAME, "summary": "x"}

    assert approval_facts.render_facts_lines(payload) == []


def test_an_ordinary_request_keeps_the_legacy_note():
    lines = approval_facts.render_facts_lines({"name": NAME})

    assert lines and approval_facts.LEGACY_LINE in lines


def test_the_reevaluation_detail_and_shell_do_not_carry_the_legacy_note(fx, ops_env):
    _with_comparison(fx, ops_env)

    detail = ops_env.service().approval_detail(OPERATOR, fx.approval_id)
    text = _shell(ops_env, ops_env.service()).dispatch(f"approval {fx.approval_id}")

    assert detail["facts_lines"] == []
    assert "旧形式" not in text


def test_the_ctl_detail_without_facts_lines_renders_cleanly():
    text = client.format_approval_detail({"id": 3, "kind": "plugin", "status": "approved",
                                          "facts_lines": []})

    assert "payload_sha256=" in text


# ---------------------------------------------------------------- shell の承認詳細

def test_the_shell_detail_lists_previous_and_current_with_the_evaluation_time_in_the_zone(
        fx, ops_env):
    _with_comparison(fx, ops_env)
    shell = _shell(ops_env, ops_env.service())
    shell._tz = lambda: "Asia/Tokyo"

    text = shell.dispatch(f"approval {fx.approval_id}")

    lines = text.split("\n")
    assert f"再評価 (前回の承認 #{fx.basis})" in lines
    assert "コード変更なし。承認すると承認の根拠だけを差し替え、配備は変わりません" in lines
    assert (f"却下すると strategy {NAME} の配備を停止します (position は保護を続けます)"
            in lines)
    evaluated = next(line for line in lines if line.startswith("評価時点"))
    assert "今回 2026-11-08 12:17:00 +09:00" in evaluated and "期間が違う比較です" in evaluated
    assert ("USDJPY holdout: 前回 pf=1.5 trades=30 avg_r=0.2 max_drawdown=0.05 (run #55) → "
            "今回 pf=1.25 trades=40 avg_r=0.12 max_drawdown=0.08") in lines
    assert "EURUSD holdout: 前回 不明 → 今回 -" in lines
    assert ("USDJPY in_sample: 前回 pf=1.5 trades=30 avg_r=0.2 max_drawdown=0.05 (承認時の記録)"
            in text)


def test_the_eval_now_example_of_the_design_is_shown_as_the_user_zone_wall_clock(fx, ops_env):
    start = datetime(2026, 11, 8, 3, 17, 42, tzinfo=timezone.utc)
    eval_now = start.replace(second=0).isoformat()
    _with_comparison(fx, ops_env, eval_now=eval_now)
    shell = _shell(ops_env, ops_env.service())
    shell._tz = lambda: effective_tz_name("Asia/Tokyo")

    text = shell.dispatch(f"approval {fx.approval_id}")

    assert "今回 2026-11-08 12:17:00 +09:00" in text


def test_the_shell_detail_of_an_ordinary_approval_is_unchanged(ops_env):
    approval_id = ops_env.make_plugin_approval()

    text = _shell(ops_env, ops_env.service()).dispatch(f"approval {approval_id}")

    assert "再評価" not in text and "前回" not in text


# ---------------------------------------------------------------- shell の却下の終端文言

def test_a_rejection_blocked_by_an_open_journal_names_the_command_that_stops_the_deployment(
        fx, ops_env):
    insert_journal(ops_env, fx.basis, NAME, "symlink", fx.target, phase="switched")

    text = _shell(ops_env, ops_env.service()).dispatch(f"reject {fx.approval_id}")

    assert text.endswith(f"\n配備を止めるなら `afx plugin disable {NAME}`")
    assert fx.row()["status"] == "pending" and fx.live() == fx.target


def test_a_stale_rejection_and_a_changed_epoch_name_the_command_that_stops_the_deployment(
        fx, ops_env):
    fx.stale()
    stale = _shell(ops_env, ops_env.service()).dispatch(f"reject {fx.approval_id}")

    assert "依頼が古いため無効にしました。配備は変えていません" in stale
    assert stale.endswith(f"\n配備を止めるなら `afx plugin disable {NAME}`") or (
        f"。配備を止めるなら `afx plugin disable {NAME}`" in stale)


def test_a_changed_epoch_rejection_names_the_command_too(fx, ops_env):
    ops_env.conn.execute("INSERT INTO data_migrations(name, applied_at) VALUES (?, ?)",
                         ("time_repair_next", datetime(2026, 11, 2).isoformat()))
    ops_env.conn.commit()

    text = _shell(ops_env, ops_env.service()).dispatch(f"reject {fx.approval_id}")

    assert "履歴が変わったため決定していません" in text
    assert f"。配備を止めるなら `afx plugin disable {NAME}`" in text


def test_an_ordinary_failed_rejection_has_no_stop_hint(ops_env, monkeypatch):
    from agentic_fx.plugin import switch
    approval_id = ops_env.make_plugin_approval()
    monkeypatch.setattr(switch, "reject_candidate", lambda *_a, **_k: False)

    text = _shell(ops_env, ops_env.service()).dispatch(f"reject {approval_id}")

    assert "afx plugin disable" not in text


# ---------------------------------------------------------------- ctl

DETAIL = {"id": 7, "kind": "plugin", "status": "pending", "payload_sha256": "ab" * 32,
          "display_timezone": "Asia/Tokyo", "candidate_origin": "reevaluation",
          "change": {"name": NAME},
          "reevaluation": {
              "name": NAME, "reevaluates_approval_id": 3,
              "previous_at": "2026-10-01T00:00:00+00:00", "eval_now": EVAL_NOW,
              "rows": [{"pair": "USDJPY", "scope": "holdout",
                        "previous": {"metrics": OLD, "source": "run", "run_id": 55},
                        "current": M}]}}


def test_the_ctl_detail_shows_the_heading_the_comparison_and_the_zone_time():
    text = client.format_approval_detail(DETAIL)

    lines = text.split("\n")
    assert "再評価 (前回の承認 #3)" in lines
    assert any(line.startswith("評価時点: 前回 2026-10-01 09:00:00 +09:00 / "
                               "今回 2026-11-08 12:17:00 +09:00") for line in lines)
    assert ("USDJPY holdout: 前回 pf=1.5 trades=30 avg_r=0.2 max_drawdown=0.05 (run #55) → "
            "今回 pf=1.25 trades=40 avg_r=0.12 max_drawdown=0.08") in lines
    assert "コード変更なし。承認すると承認の根拠だけを差し替え、配備は変わりません" in lines


@pytest.mark.parametrize("data", [
    {"id": 7, "kind": "plugin", "status": "pending"},
    {"id": 7, "kind": "plugin", "status": "pending", "candidate_origin": "reevaluation"},
    {"id": 7, "kind": "plugin", "status": "pending", "candidate_origin": "reevaluation",
     "reevaluation": {"rows": [None, {"pair": 1}]}},
])
def test_the_ctl_detail_of_an_old_daemon_or_a_malformed_view_does_not_fail(data):
    assert "approval #7" in client.format_approval_detail(data)


def _reject(tmp_path, detail, job):
    api = _StubApi(tmp_path, [_ok(detail), _ok({"job_id": JOB, "state": "queued"}), _ok(job)],
                   principal=Principal.APPROVER)
    out = io.StringIO()
    asked = []

    class Terminal:
        def yes(self, prompt):
            asked.append(prompt)
            return True

    runner = client.Runner(api, stdin=io.StringIO(""), stdout=out)
    rc = runner._op_reject(7, None, terminal=Terminal())
    return rc, out.getvalue(), asked


def _done(**result):
    return {"id": JOB, "state": "done", "result": {"approval_id": 7, **result}}


def test_the_ctl_reject_prompt_says_the_deployment_will_stop(tmp_path):
    _rc, _out, asked = _reject(tmp_path, DETAIL, _done(outcome="rejected", status="rejected",
                                                       deploy_phase="done"))
    assert asked == [f"却下すると strategy {NAME} の配備を停止します (position は保護を続けます)。"
                     "approval #7 を却下しますか"]


def test_the_ctl_reject_prompt_of_an_ordinary_approval_is_unchanged(tmp_path):
    plain = {"id": 7, "kind": "plugin", "status": "pending", "payload_sha256": "ab" * 32}
    _rc, _out, asked = _reject(tmp_path, plain, _done(outcome="rejected", status="rejected"))
    assert asked == ["approval #7 を却下しますか"]


@pytest.mark.parametrize("result,expected", [
    ({"outcome": "rejected", "status": "rejected", "deploy_phase": "done"},
     "approval #7 却下し、配備を停止しました"),
    ({"outcome": "rejected", "status": "rejected", "deploy_phase": "needs_retry"},
     f"approval #7 却下しました。取引は止まりましたが symlink を外せていません。"
     f"`afx plugin disable {NAME}` で完了してください"),
    ({"outcome": "rejected", "status": "rejected", "deploy_phase": "foreign"},
     f"approval #7 却下しました。取引は止まりましたが plugins/{NAME} が想定と違うため"
     "外していません。確認してください"),
    ({"outcome": "invalidated", "status": "invalidated", "reason": "reevaluation_stale"},
     "approval #7 依頼が古いため無効にしました。配備は変えていません。"
     f"再評価をやり直してください。配備を止めるなら `afx plugin disable {NAME}`"),
])
def test_the_ctl_reject_result_follows_the_shape_of_the_result(tmp_path, result, expected):
    rc, out, _ = _reject(tmp_path, DETAIL, _done(**result))
    assert rc == 0
    assert out.strip().splitlines()[-1] == expected
    assert "rejected" not in out.strip().splitlines()[-1].split(" ")[2:3]


def test_the_ctl_reject_of_an_ordinary_approval_still_says_rejected(tmp_path):
    plain = {"id": 7, "kind": "plugin", "status": "pending", "payload_sha256": "ab" * 32}
    _rc, out, _ = _reject(tmp_path, plain, _done(outcome="rejected", status="rejected"))
    assert out.strip().splitlines()[-1] == "approval #7 rejected"


def test_the_ctl_reject_stopped_by_a_changed_epoch_says_nothing_was_decided(tmp_path):
    job = {"id": JOB, "state": "failed", "result_code": "invalid_state",
           "result": {"approval_id": 7, "outcome": "history_epoch_changed",
                      "status": "pending"}}
    with pytest.raises(client.ClientError) as caught:
        _reject(tmp_path, DETAIL, job)
    assert caught.value.message == (
        "approval #7 履歴が変わったため決定していません。"
        f"配備を止めるなら `afx plugin disable {NAME}`")


def test_the_ctl_reject_blocked_for_another_reason_names_the_command_for_a_reevaluation(tmp_path):
    job = {"id": JOB, "state": "failed", "result_code": "internal"}
    with pytest.raises(client.ClientError) as caught:
        _reject(tmp_path, DETAIL, job)
    assert f"配備を止めるなら `afx plugin disable {NAME}`" in caught.value.message

    plain = {"id": 7, "kind": "plugin", "status": "pending", "payload_sha256": "ab" * 32}
    with pytest.raises(client.ClientError) as caught:
        _reject(tmp_path, plain, job)
    assert "afx plugin disable" not in caught.value.message


def test_the_ctl_approve_of_a_reevaluation_says_the_basis_was_replaced(tmp_path):
    api = _StubApi(tmp_path, [_ok({"job_id": JOB, "state": "queued"}),
                              _ok(_done(outcome="reevaluation_confirmed", status="approved"))],
                   principal=Principal.APPROVER)
    out = io.StringIO()

    assert client.Runner(api, stdin=io.StringIO(""), stdout=out)._decide("approve", 7, {}) == 0

    assert out.getvalue().strip().splitlines()[-1] == (
        "approval #7 approved: 再評価の結果で承認の根拠を差し替えました (配備は変わりません)")


def test_the_ctl_approve_of_a_stale_reevaluation_says_it_was_invalidated(tmp_path):
    api = _StubApi(tmp_path, [_ok({"job_id": JOB, "state": "queued"}),
                              _ok(_done(outcome="invalidated", status="invalidated",
                                        reason="reevaluation_stale"))],
                   principal=Principal.APPROVER)
    out = io.StringIO()

    client.Runner(api, stdin=io.StringIO(""), stdout=out)._decide("approve", 7, {})

    assert out.getvalue().strip().splitlines()[-1] == (
        "approval #7 依頼が古いため無効にしました。配備は変えていません。"
        "再評価をやり直してください")


# ---------------------------------------------------------------- afx-ops との文言一致

def test_the_wording_in_afx_ops_matches_the_daemon_side():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "afx-ops" / "afx_ops" / "reevaluation_text.py"
    spec = importlib.util.spec_from_file_location("afx_ops_reevaluation_text", path)
    import sys
    other = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = other
    try:
        spec.loader.exec_module(other)
    finally:
        del sys.modules[spec.name]
    assert other.ORIGIN == reevaluation_view.ORIGIN
    assert other.REJECT_TEXT == reevaluation_view.REJECT_TEXT
    assert other.STALE_TEXT == reevaluation_view.STALE_TEXT
    assert other.STOP_HINT == reevaluation_view.STOP_HINT
    assert other.CONFIRMED_TEXT == reevaluation_view.CONFIRMED_TEXT
    assert other.reject_warning("x", "却下") == reevaluation_view.reject_warning("x")
    assert other.NO_CODE_CHANGE in "\n".join(reevaluation_view.lines(
        {"reevaluates_approval_id": 1}, lambda v: v))
    assert other.METRIC_KEYS == reevaluation_view.METRIC_KEYS
    assert other.SCOPES == reevaluation_view.SCOPES
    assert other._SOURCE_TEXT == reevaluation_view._SOURCE_TEXT
    daemon_lines = "\n".join(reevaluation_view.lines(
        {"reevaluates_approval_id": 1}, lambda v: v))
    assert daemon_lines.startswith(other.HEADING)
    assert other.PERIOD_NOTE.strip("()") in daemon_lines
