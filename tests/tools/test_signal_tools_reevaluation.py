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


def _approved_reevaluation(conn, name, run_id):
    approval_id = approvals_store.create(
        conn, "plugin", {"name": name, "content_hash": CONTENT,
                         "candidate_origin": "reevaluation",
                         "run_ids": {"in_sample": {"USDJPY": run_id}, "holdout": {}}}, NOW)
    approvals_store.apply_decision(conn, approval_id, "approved", decided_by="human", now=NOW)
    return approval_id


def test_a_broken_approval_row_leaves_the_earlier_numbers_in_place(tmp_path, caplog):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    _run(conn, "approval", {"trades": 40, "pf": 1.3})
    new = _run(conn, "reevaluation", {"trades": 41, "pf": 0.9})
    approval_id = _approved_reevaluation(conn, "mine", new)
    conn.execute("UPDATE approval_requests SET decided_at='2026-11-08T12:00:00' WHERE id=?",
                 (approval_id,))
    conn.commit()
    _signal(conn, "mine")

    with caplog.at_level("WARNING"):
        attached = _attached(conn)

    assert attached["mine"] == {"trades": 40, "pf": 1.3}
    assert sum("reevaluation lookup failed" in r.getMessage() for r in caplog.records) == 1


def test_one_call_scans_the_approvals_once_per_plugin(tmp_path, monkeypatch):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    new = _run(conn, "reevaluation", {"trades": 41, "pf": 0.9})
    _approved_reevaluation(conn, "mine", new)
    for hours in (1, 2, 3):
        signals.add(
            conn, plugin="mine", content_hash=CONTENT, pair="USDJPY", timeframe="1h",
            bar_ts=(NOW - timedelta(hours=hours)).isoformat(), kind="strategy",
            payload={"action": "hold"}, now=NOW, contract_version="executable_v1")
    _signal(conn, "other")
    calls = []
    real = approvals_store.latest_approved_for_identity

    def counting(conn_, name, content_hash):
        calls.append(name)
        return real(conn_, name, content_hash)

    monkeypatch.setattr(approvals_store, "latest_approved_for_identity", counting)

    _attached(conn)

    assert sorted(calls) == ["mine", "other"]
