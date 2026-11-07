"""A re-evaluation request that a real history repair finds pending.

The repair invalidates every pending strategy approval before it moves the history epoch,
so a request decided afterwards is refused as already decided and nothing else changes.
"""
from __future__ import annotations

import hashlib

import pytest

from agentic_fx.backtest import time_repair
from agentic_fx.plugin import switch
from agentic_fx.store import approvals as approvals_store
from tests.plugin.test_reevaluation_decision import NAME, Case


def _history_for_the_repair(case: Case, monkeypatch) -> None:
    conn = case.conn
    conn.executemany("INSERT INTO ohlcv_history VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("EURUSD", "5m", "2026-01-05T12:00:00+00:00", 1, 2, .5, 1.5, 7, "mt5", None),
        ("USDJPY", "5m", "2026-01-05T12:05:00+00:00", 2, 3, 1.5, 2.5, 8, "mt5", None)])
    rows = conn.execute("SELECT * FROM ohlcv_history ORDER BY symbol, bar_time").fetchall()

    def digest(values: bool) -> str:
        lines = ["|".join(str(row[name]) for name in ("symbol", "interval", "bar_time", "source"))
                 + ("|" + "|".join(repr(float(row[name])) for name in (
                     "open", "high", "low", "close", "volume")) if values else "")
                 for row in rows]
        return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()

    monkeypatch.setattr(time_repair, "HISTORY_FINGERPRINT", time_repair.HistoryFingerprint(
        "2026-01-05T12:00:00+00:00", "2026-01-05T12:05:00+00:00",
        {"EURUSD": 1, "USDJPY": 1}, digest(False), digest(True)))
    monkeypatch.setattr(time_repair, "_cache_status", lambda _: [
        {"name": item.name, "status": "absent_before_cache_window"}
        for item in time_repair.CACHE_SETS])
    monkeypatch.setattr(time_repair, "_cache_changes", lambda _: ([
        {"name": item.name, "status": "absent_before_cache_window", "delete": 0, "replace": 0}
        for item in time_repair.CACHE_SETS], []))
    monkeypatch.setattr(time_repair, "_anchors", lambda _: [{"week": "synthetic", "passed": True}])
    conn.commit()


@pytest.fixture
def repaired(tmp_path, monkeypatch):
    case = Case(tmp_path, epoch=False)
    case.world.signal(NAME, case.content)
    case.world.signal(NAME, case.content, claimed=True)
    _history_for_the_repair(case, monkeypatch)
    before = case.state()
    assert case.row()["status"] == "pending"
    assert switch.history_epoch(case.conn) == "none"

    plan = time_repair.build_plan(case.conn, indicator_pending="keep")
    assert [item["id"] for item in plan["approval_changes"]] == [case.approval_id]
    time_repair.apply_plan(case.conn, case.world.db_path, plan, confirm=plan["plan_hash"])
    return case, before


def test_the_repair_invalidates_the_pending_request_and_moves_the_epoch(repaired):
    case, before = repaired

    row = case.row()
    assert (row["status"], row["reason"], row["decided_by"]) == (
        "invalidated", "history_time_repaired", "system:time_repair")
    assert switch.history_epoch(case.conn) == "time_repair_dst_v1"
    # Nothing about the deployment moved.
    after = case.state()
    assert after[0] == before[0] and after[1] == before[1]
    assert after[3:] == before[3:]
    assert case.decision() == (case.basis_id, "approved")


def test_approving_after_the_repair_is_refused_as_already_decided(repaired):
    case, _before = repaired
    state = case.state()

    outcome = case.approve()

    assert (outcome.outcome, outcome.status) == ("already_decided", "invalidated")
    assert case.state() == state


def test_rejecting_after_the_repair_is_refused_and_stops_nothing(repaired):
    case, _before = repaired
    state = case.state()

    with pytest.raises(approvals_store.AlreadyDecidedError):
        case.reject()

    assert case.state() == state
    assert case.live_target() is not None
    assert case.decision() == (case.basis_id, "approved")
    assert case.conn.execute("SELECT COUNT(*) FROM plugin_deploy_ops").fetchone()[0] == 0
