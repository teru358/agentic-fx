"""GET /v1/improve/runs (一覧) と GET /v1/improve/runs/{id} の `summary`。"""
from __future__ import annotations

import json

import pytest

from agentic_fx import improve_notice_codes as codes
from agentic_fx.loops import improve_run_summary as rs
from agentic_fx.ops.contracts import (PRINCIPAL_SCOPES, ErrorCode, OpsError,
                                      Principal, Scope)
from agentic_fx.ops.service import ENDPOINTS, IMPROVE_RUNS_MAX_LIMIT
from agentic_fx.store import improve_run_summaries as store

from ._improve_seed import END, START, seed

OPERATOR = Principal.OPERATOR


def _record(items):
    return rs.serialize(rs.PreparedSummary(items))


def _floor_items(plugin="london_pullback"):
    pair = {"pair": "USDJPY", "trades": 64, "pf": 0.91, "avg_r": -0.02,
            "max_drawdown": 0.1, "trades_per_eval_bar": 0.01, "evaluable": True,
            "failed": ["pf", "avg_r"]}
    return {
        "tried": {"plugin": plugin, "kind": "strategy", "origin": "new",
                  "indicators": ["atr_band"]},
        "floor": {
            "thresholds": {"min_pf": 1.0, "require_positive_avg_r": True,
                           "require_holdout_evaluable": False,
                           "max_drawdown": 0.2, "max_trades_per_eval_bar": 0.25},
            "failed_stage": "in_sample", "failed_conditions": ["pf", "avg_r"],
            "failed_pair_count": 1,
            "stages": [{"stage": "in_sample", "state": "measured",
                        "pairs": [pair]},
                       {"stage": "holdout", "state": "not_run", "pairs": []}]}}


def test_endpoint_is_a_plain_read_with_the_list_scope():
    endpoint = ENDPOINTS["improve_runs"]
    assert endpoint.route == "GET /v1/improve/runs"
    assert endpoint.scope is Scope.APPROVALS_LIST
    assert endpoint.mutating is False and endpoint.idempotent is False


def test_ops_web_can_read_it():
    assert Scope.APPROVALS_LIST in PRINCIPAL_SCOPES[Principal.OPS_WEB]


def test_every_principal_is_authorized_exactly_by_the_scope(ops_env):
    svc = ops_env.service()
    seed(ops_env)
    for principal in Principal:
        if Scope.APPROVALS_LIST in PRINCIPAL_SCOPES[principal]:
            assert len(svc.improve_runs(principal)["runs"]) == 1
        else:
            with pytest.raises(OpsError) as raised:
                svc.improve_runs(principal)
            assert raised.value.code is ErrorCode.FORBIDDEN


def test_rows_are_newest_first_and_carry_the_notice_and_the_summary(ops_env):
    approval = ops_env.make_plugin_approval("sma", mission_id=1)
    first = seed(ops_env, summary=_record(_floor_items()))
    second = seed(ops_env, notice=codes.NOTICE_SUBMITTED, approval_id=approval,
                  tool_calls=3, summary=_record(
                      {"tried": {"plugin": "sma", "kind": "indicator",
                                 "origin": "update", "indicators": []}}),
                  trigger="manual")
    old = seed(ops_env, no_summary=True, notice=codes.NOTICE_COMMIT_CRASHED)
    data = ops_env.service().improve_runs(OPERATOR)
    assert data["has_more"] is False
    assert [r["id"] for r in data["runs"]] == [old, second, first]
    by_id = {r["id"]: r for r in data["runs"]}
    assert by_id[first] == {
        "id": first, "started_at": START.isoformat(),
        "finished_at": END.isoformat(), "trigger": "scheduled",
        "result_code": "gate_failed", "reason_code": "unprofitable",
        "plugin": "london_pullback", "failed_stage": "in_sample",
        "failed_conditions": ["pf", "avg_r"], "failed_pair_count": 1,
        "approval_id": None}
    assert (by_id[second]["plugin"], by_id[second]["approval_id"],
            by_id[second]["trigger"], by_id[second]["failed_stage"]) \
        == ("sma", approval, "manual", None)
    assert by_id[old]["plugin"] is None and by_id[old]["failed_conditions"] == []
    json.dumps(data)


def test_a_summary_that_is_not_ok_gives_no_plugin_or_floor(ops_env):
    empty = seed(ops_env)
    failed = seed(ops_env, summary=store.SummaryRecord("build_failed", "{}"))
    rows = {r["id"]: r for r in ops_env.service().improve_runs(OPERATOR)["runs"]}
    for mission_id in (empty, failed):
        assert rows[mission_id]["plugin"] is None
        assert rows[mission_id]["failed_stage"] is None
        assert rows[mission_id]["failed_pair_count"] is None


def test_a_broken_stored_summary_is_not_trusted(ops_env):
    mission_id = seed(ops_env, summary=store.SummaryRecord(
        "ok", json.dumps({"tried": {"plugin": "x" * 500}})))
    row = ops_env.service().improve_runs(OPERATOR)["runs"][0]
    assert row["id"] == mission_id and row["plugin"] is None


def test_limit_default_bounds_and_has_more(ops_env):
    ids = [seed(ops_env) for _ in range(55)]
    svc = ops_env.service()
    default = svc.improve_runs(OPERATOR)
    assert len(default["runs"]) == 50 and default["has_more"] is True
    assert default["runs"][0]["id"] == ids[-1]
    exact = svc.improve_runs(OPERATOR, limit=55)
    assert len(exact["runs"]) == 55 and exact["has_more"] is False
    assert len(svc.improve_runs(OPERATOR, limit=1)["runs"]) == 1
    for bad in (0, -1, IMPROVE_RUNS_MAX_LIMIT + 1, True, "5", 2.5, None):
        with pytest.raises(OpsError) as raised:
            svc.improve_runs(OPERATOR, limit=bad)
        assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert IMPROVE_RUNS_MAX_LIMIT == 100


def test_a_hundred_worst_case_rows_fit_in_64_kib(ops_env):
    items = _floor_items(plugin="p" * 64)
    items["floor"]["failed_conditions"] = [
        "pf", "avg_r", "holdout_not_evaluable", "drawdown_too_deep",
        "too_frequent"]
    record = _record(items)
    for _ in range(100):
        seed(ops_env, summary=record, notice=codes.NOTICE_NOT_IMPROVED,
             trigger="scheduled", approval_id=None)
    data = ops_env.service().improve_runs(OPERATOR, limit=100)
    assert len(data["runs"]) == 100
    body = json.dumps({"ok": True, "data": {**data, "display_timezone":
                                            "America/Argentina/ComodRivadavia"}})
    assert len(body.encode("utf-8")) <= 64 * 1024


def test_unfinished_missions_are_not_listed(ops_env):
    seed(ops_env, terminal=False)
    assert ops_env.service().improve_runs(OPERATOR) == {
        "runs": [], "has_more": False}


# ---- 詳細の summary -------------------------------------------------------

def test_detail_carries_the_summary_dto_and_keeps_every_existing_field(ops_env):
    record = _record(_floor_items())
    mission_id = seed(ops_env, summary=record)
    data = ops_env.service().improve_run(OPERATOR, mission_id)
    assert data["summary"] == {"version": 1, "state": "ok",
                               "items": json.loads(record.summary_json)}
    assert set(data) == {"id", "backend", "model", "elapsed_sec", "snapshot",
                         "report_state", "approval", "floor", "summary"}
    assert data["floor"] is None  # 公開済みレポートの逆パースだけが出所
    json.dumps(data)


@pytest.mark.parametrize("state", ["empty", "build_failed"])
def test_detail_summary_for_states_without_items(ops_env, state):
    mission_id = seed(ops_env, summary=store.SummaryRecord(state, "{}"))
    assert ops_env.service().improve_run(OPERATOR, mission_id)["summary"] == {
        "version": 1, "state": state, "items": {}}


def test_a_mission_from_before_the_table_has_a_null_summary(ops_env):
    mission_id = seed(ops_env, no_summary=True)
    assert ops_env.service().improve_run(OPERATOR, mission_id)["summary"] is None


def test_unfinished_mission_has_a_null_summary(ops_env):
    mission_id = seed(ops_env, terminal=False)
    assert ops_env.service().improve_run(OPERATOR, mission_id)["summary"] is None


def test_detail_never_returns_items_that_break_the_shape(ops_env):
    mission_id = seed(ops_env, summary=store.SummaryRecord(
        "ok", json.dumps({"observation": ["not", "text"]})))
    assert ops_env.service().improve_run(OPERATOR, mission_id)["summary"] == {
        "version": 1, "state": "build_failed", "items": {}}
