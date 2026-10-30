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


def test_monitor_reads_candidate_and_baseline_rows_and_adoption_window(ops_env):
    """Changing the baseline-row query or using an equal monotonic step must fail here."""
    conn = ops_env.conn
    approved = approvals.create(conn, "plugin", {
        "name": "s", "content_hash": "b" * 64,
        "holdout": {"USDJPY": {"period_start": "2026-09-01T00:00:00+00:00",
                                   "period_end": "2026-10-01T00:00:00+00:00"}}}, NOW)
    conn.execute("UPDATE approval_requests SET status='approved' WHERE id=?", (approved,))
    payload = {"name": "s", "content_hash": "c" * 64, "baseline": {
        "base_content_hash": "b" * 64, "comparison": {
            "verdict": "unavailable", "reason": "baseline_unhealthy", "stages": {
                "holdout": {"USDJPY": {"candidate": {"pf": 1.2, "avg_r": 0.2}}}}}}}
    approval = approvals.create(conn, "plugin", payload, NOW)
    for variant, content_hash, ref_hash in (("candidate", "c" * 64, None),
                                            ("baseline", "b" * 64, "c" * 64)):
        backtest_runs.save_harness_run(
            conn, scope="holdout_gate", plugin_ref="plugins/s", content_hash=content_hash,
            kind="strategy", pair="USDJPY", timeframe="1h", source="test", base_interval="1m",
            period=(datetime(2026, 9, 15, tzinfo=timezone.utc), NOW),
            metrics={"pf": 1.2, "avg_r": 0.2}, settings_hash="x", core_commit="x",
            initial_balance=1.0, now=NOW, variant=variant, ref_content_hash=ref_hash,
            mission_id=approval, mission_outcome="not_improved")
    summary = monitor_summary(conn)
    assert summary == [{"strategy": "s", "attempts": 1,
                        "by_candidate_hash": [{"candidate": "c" * 12, "base": "b" * 12,
                                               "verdict": "unavailable", "monotone_run": 1}],
                        "not_submitted_share": 0.0, "unavailable_rate": 1.0,
                        "window_overlap": {"mean": 16 / 30, "n": 1}}]
