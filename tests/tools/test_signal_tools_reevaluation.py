"""get_signals attaches the numbers of the approval that is in force, by the signal's plugin."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentic_fx.core.contracts import FixedClock
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import backtest_runs, signals
from agentic_fx.store.db import connect, init_db
from agentic_fx.tools import signal_tools
from tests.tools.test_signal_tools import SETTINGS

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
CONTENT = "c" * 64


def _run(conn, outcome, metrics):
    return backtest_runs.save_harness_run(
        conn, scope="in_sample", plugin_ref="plugins/x", content_hash=CONTENT,
        kind="strategy", pair="USDJPY", timeframe="1h", source="dukascopy",
        base_interval="1m", period=(NOW, NOW), metrics=metrics, settings_hash="s",
        core_commit="c", initial_balance=1e6, now=NOW, mission_outcome=outcome)


def _signal(conn, plugin):
    return signals.add(
        conn, plugin=plugin, content_hash=CONTENT, pair="USDJPY", timeframe="1h",
        bar_ts=(NOW - timedelta(hours=1)).isoformat(), kind="strategy",
        payload={"action": "hold"}, now=NOW, contract_version="executable_v1")


def _attached(conn):
    tools = signal_tools.build(conn, SETTINGS, FixedClock(NOW))
    return {row["plugin"]: row["in_sample_metrics"] for row in tools[0].func(pair="USDJPY")}


def test_a_signal_gets_the_re_evaluated_numbers_of_its_own_plugin(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    _run(conn, "approval", {"trades": 40, "pf": 1.3})
    new = _run(conn, "reevaluation", {"trades": 41, "pf": 0.9})
    approval_id = approvals_store.create(
        conn, "plugin", {"name": "mine", "content_hash": CONTENT,
                         "candidate_origin": "reevaluation",
                         "run_ids": {"in_sample": {"USDJPY": new}, "holdout": {}}}, NOW)
    approvals_store.apply_decision(conn, approval_id, "approved", decided_by="human", now=NOW)
    _signal(conn, "mine")
    _signal(conn, "copy_of_mine")

    attached = _attached(conn)

    assert attached["mine"] == {"trades": 41, "pf": 0.9}
    assert attached["copy_of_mine"] == {"trades": 40, "pf": 1.3}
