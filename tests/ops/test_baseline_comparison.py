from __future__ import annotations

from datetime import datetime, timezone

from agentic_fx.core.plugin_files import content_hash_of_dir
from agentic_fx.loops.baseline_monitor import monitor_summary
from agentic_fx.ops.contracts import Principal
from agentic_fx.store import approvals, backtest_runs


NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def test_approval_detail_warns_when_deployed_hash_differs_from_comparison_base(ops_env):
    """Removing the live-hash check must make this warning disappear and fail."""
    deployed = ops_env.plugins_root / "s"
    deployed.mkdir()
    (deployed / "plugin.py").write_text("x = 1\n")
    (deployed / "config.yaml").write_text("kind: strategy\n")
    live_hash = content_hash_of_dir(deployed)
    approval_id = ops_env.raw_approval(payload={
        "name": "s", "baseline": {"base_content_hash": "b" * 64,
        "comparison": {"verdict": "improved", "stages": {}}}})

    detail = ops_env.service().approval_detail(Principal.OPERATOR, approval_id)

    assert detail["baseline"]["base_content_hash"] == "b" * 64
    assert detail["baseline_hash_warning"] == {"expected": "b" * 12, "actual": live_hash[:12]}


def _row(conn, *, content_hash, variant, mission_id, pf=1.2, avg_r=0.2, ref=None,
         period=(datetime(2026, 9, 15, tzinfo=timezone.utc), NOW), outcome="not_improved",
         name="s", pair="USDJPY"):
    backtest_runs.save_harness_run(
        conn, scope="holdout_gate", plugin_ref=f"plugins/{name}", content_hash=content_hash,
        kind="strategy", pair=pair, timeframe="1h", source="test", base_interval="1m",
        period=period, metrics={"pf": pf, "avg_r": avg_r}, settings_hash="x",
        core_commit="x", initial_balance=1.0, now=NOW, variant=variant,
        ref_content_hash=ref, mission_id=mission_id, mission_outcome=outcome)


def _adopted(conn, base="b" * 64, window=(datetime(2026, 9, 1, tzinfo=timezone.utc),
                                          datetime(2026, 10, 1, tzinfo=timezone.utc))):
    approved = approvals.create(conn, "plugin", {"name": "s", "content_hash": base}, NOW)
    conn.execute("UPDATE approval_requests SET status='approved' WHERE id=?", (approved,))
    _row(conn, content_hash=base, variant="candidate", mission_id=900, period=window,
         outcome="improved")


def _compare(conn, mission, candidate, *, pf, avg_r, base="b" * 64, submitted=None, **kw):
    _row(conn, content_hash=candidate, variant="candidate", mission_id=mission, pf=pf,
         avg_r=avg_r, **kw)
    _row(conn, content_hash=base, variant="baseline", mission_id=mission, ref=candidate, **kw)
    if submitted:
        approvals.create(conn, "plugin", {"name": "s", "content_hash": candidate, "baseline": {
            "base_content_hash": base, "comparison": {"verdict": submitted, "stages": {}}}}, NOW)


def test_monitor_reads_candidate_and_baseline_rows_and_adoption_window(ops_env):
    """The adoption window comes from the deployed version's own holdout row; a missing
    record is null and is excluded from n."""
    conn = ops_env.conn
    _adopted(conn)
    _compare(conn, 1, "c" * 64, pf=1.2, avg_r=0.2, submitted="improved")
    summary = monitor_summary(conn)
    assert summary == [{"strategy": "s", "attempts": 1,
                        "by_candidate_hash": [{"candidate": "c" * 12, "base": "b" * 12,
                                               "verdict": "improved", "monotone_run": 1}],
                        "not_submitted_share": 0.0, "unavailable_rate": 0.0,
                        "window_overlap": {"mean": 16 / 45, "n": 1}}]


def test_monitor_window_overlap_is_null_and_not_counted_without_an_adoption_record(ops_env):
    conn = ops_env.conn
    _compare(conn, 1, "c" * 64, pf=1.2, avg_r=0.2)
    (row,) = monitor_summary(conn)
    assert row["window_overlap"] == {"mean": None, "n": 0}
    assert row["not_submitted_share"] == 1.0
    assert row["by_candidate_hash"][0]["verdict"] == "not_improved"


def test_monitor_counts_an_unavailable_submission_that_left_no_baseline_row(ops_env):
    conn = ops_env.conn
    approvals.create(conn, "plugin", {"name": "s", "content_hash": "d" * 64, "mission_id": 5,
                                      "baseline": {"base_content_hash": "b" * 64, "comparison": {
                                          "verdict": "unavailable", "reason": "x",
                                          "stages": {}}}}, NOW)
    (row,) = monitor_summary(conn)
    assert row["attempts"] == 1 and row["unavailable_rate"] == 1.0
    assert row["not_submitted_share"] == 0.0
    assert row["by_candidate_hash"][0]["verdict"] == "unavailable"


def test_monitor_monotone_run_grows_only_while_both_metrics_move_the_same_way(ops_env):
    conn = ops_env.conn
    for mission, (pf, avg_r) in enumerate([(1.1, 0.1), (1.2, 0.2), (1.3, 0.3), (1.3, 0.4),
                                           (1.4, 0.5), (1.2, 0.3)], start=1):
        _compare(conn, mission, f"{mission:x}" * 64, pf=pf, avg_r=avg_r)
    (row,) = monitor_summary(conn)
    assert [h["monotone_run"] for h in row["by_candidate_hash"]] == [1, 2, 3, 1, 2, 1]


def test_monitor_keeps_runs_apart_per_deployed_version(ops_env):
    conn = ops_env.conn
    _compare(conn, 1, "1" * 64, pf=1.1, avg_r=0.1, base="a" * 64)
    _compare(conn, 2, "2" * 64, pf=1.2, avg_r=0.2, base="b" * 64)
    (row,) = monitor_summary(conn)
    assert [h["monotone_run"] for h in row["by_candidate_hash"]] == [1, 1]


# ---- 通知用 DTO (daemon の GET) と承認詳細の各面 ----

import json  # noqa: E402

import pytest  # noqa: E402

from agentic_fx import improve_notice_codes as codes  # noqa: E402
from agentic_fx.ops import client  # noqa: E402

from ._improve_seed import seed  # noqa: E402


def _metrics(pf, avg_r, trades=40):
    return {"pf": pf, "avg_r": avg_r, "trades": trades, "win_rate": 0.51,
            "max_drawdown": 0.0731, "total_pnl": 123.5, "kill_switch_latches": 0,
            "evaluable": True}


def _comparison(verdict="improved", mode="enforce", reason="holdout_exceeds"):
    in_sample = {"USDJPY": {"candidate": _metrics(1.91, 0.41, 700),
                            "deployed": _metrics(1.71, 0.31, 600), "verdict": verdict}}
    holdout = {"USDJPY": {"candidate": _metrics(1.31, 0.21), "deployed": _metrics(1.17, 0.11),
                          "verdict": verdict}}
    return {"mode": mode, "verdict": verdict, "reason": reason,
            "stages": {"in_sample": in_sample, "holdout": holdout}}


def _submitted(env, baseline):
    payload = {"name": "s"}
    if baseline is not None:
        payload["baseline"] = baseline
    approval = env.raw_approval(payload=payload)
    return seed(env, notice=codes.NOTICE_SUBMITTED, approval_id=approval)


def _run(env, mission_id):
    return env.service().improve_run(Principal.OPERATOR, mission_id)


def test_an_improved_submission_returns_only_the_four_holdout_items_per_version(ops_env):
    """Returning the whole comparison, in_sample or other metrics must fail here."""
    mission = _submitted(ops_env, {"base_content_hash": "b" * 64, "comparison": _comparison()})
    assert _run(ops_env, mission)["comparison"] == {"holdout": {"USDJPY": {
        "candidate": {"pf": 1.31, "avg_r": 0.21, "trades": 40, "evaluable": True},
        "deployed": {"pf": 1.17, "avg_r": 0.11, "trades": 40, "evaluable": True}}}}
    text = json.dumps(_run(ops_env, mission))
    for hidden in ("0.0731", "123.5", "1.91", "0.51"):
        assert hidden not in text


@pytest.mark.parametrize("verdict,mode", [("not_improved", "shadow"), ("unavailable", "enforce"),
                                          ("unavailable", "shadow")])
def test_a_submission_that_did_not_improve_carries_no_numbers(ops_env, verdict, mode):
    """Removing the improved-only gate in the DTO must fail here."""
    mission = _submitted(ops_env, {"base_content_hash": "b" * 64,
                                   "comparison": _comparison(verdict, mode, "x")})
    data = _run(ops_env, mission)
    assert data["comparison"] is None
    text = json.dumps(data)
    for hidden in ("1.31", "1.17", "1.91", "0.0731"):
        assert hidden not in text


@pytest.mark.parametrize("baseline", [
    None, {"ref_plugin_ref": "plugins/s", "variant": "baseline"},
    {"base_content_hash": "b" * 64, "comparison": "x"},
    {"base_content_hash": "b" * 64, "comparison": {"verdict": "improved", "stages": []}},
    {"base_content_hash": "b" * 64, "comparison": {"verdict": "improved",
                                                    "stages": {"holdout": {"P": 1}}}}])
def test_a_legacy_or_malformed_payload_gives_no_comparison_and_does_not_fail(ops_env, baseline):
    data = _run(ops_env, _submitted(ops_env, baseline))
    assert data["comparison"] is None and data["approval"] is not None


def test_a_not_improved_rejection_carries_no_comparison(ops_env):
    mission = seed(ops_env, notice=codes.NOTICE_NOT_IMPROVED)
    assert _run(ops_env, mission)["comparison"] is None


def _detail(env, baseline, **payload):
    approval = env.raw_approval(payload={"name": "s", **payload,
                                         **({"baseline": baseline} if baseline else {})})
    return env.service().approval_detail(Principal.OPERATOR, approval)


def test_approval_detail_passes_the_comparison_through_for_the_cli(ops_env):
    detail = _detail(ops_env, {"base_content_hash": "b" * 64, "comparison": _comparison()})
    shown = client.format_approval_detail(detail)
    assert "配備中の版 (同条件で再評価)" in shown
    assert "判定=改善" in shown and "base_content_hash=" + "b" * 12 in shown
    assert "in_sample USDJPY 候補" in shown and "holdout USDJPY 配備中の版" in shown
    assert "pf=1.17" in shown


@pytest.mark.parametrize("verdict,mode,reason,label", [
    ("not_improved", "shadow", "x", "未改善 (観測のみ)"),
    ("unavailable", "enforce", "baseline_unhealthy", "比較不能 (baseline_unhealthy)")])
def test_the_cli_verdict_label(ops_env, verdict, mode, reason, label):
    detail = _detail(ops_env, {"base_content_hash": "b" * 64,
                               "comparison": _comparison(verdict, mode, reason)})
    assert "判定=" + label in client.format_approval_detail(detail)


def test_the_cli_shows_a_hash_mismatch_warning_only_when_the_hashes_differ(ops_env):
    deployed = ops_env.plugins_root / "s"
    deployed.mkdir()
    (deployed / "plugin.py").write_text("x = 1\n")
    (deployed / "config.yaml").write_text("kind: strategy\n")
    live = content_hash_of_dir(deployed)
    stale = _detail(ops_env, {"base_content_hash": "b" * 64, "comparison": _comparison()})
    assert "警告: 配備中の版の hash が比較時と異なります" in client.format_approval_detail(stale)
    fresh = _detail(ops_env, {"base_content_hash": live, "comparison": _comparison()})
    assert fresh["baseline_hash_warning"] is None
    assert "警告" not in client.format_approval_detail(fresh)


@pytest.mark.parametrize("baseline", [None, {"ref_plugin_ref": "plugins/s"},
                                      {"base_content_hash": "b" * 64}])
def test_a_legacy_payload_shows_no_table_on_the_service_and_the_cli(ops_env, baseline):
    detail = _detail(ops_env, baseline)
    assert detail["baseline_hash_warning"] is None
    shown = client.format_approval_detail(detail)
    assert "配備中の版" not in shown and "警告" not in shown


def test_the_cli_formatter_accepts_a_detail_from_an_older_daemon():
    shown = client.format_approval_detail({"id": 1, "kind": "plugin", "status": "pending"})
    assert "配備中の版" not in shown
