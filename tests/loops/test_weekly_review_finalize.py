"""週次の確定: 窓終端での切り出し・評価時刻・劣化・棚卸し・原子的な保存。"""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from agentic_fx.loops import weekly_review as wk
from agentic_fx.plugin import read_model, read_model_inputs
from agentic_fx.plugin.read_model import SOURCE_UNAVAILABLE, LiveScan
from agentic_fx.store import signal_forward as sf
from agentic_fx.store import weekly_review as store
from tests.backtest.factories import SETTINGS
from tests.plugin.read_model_world import World
from tests.store.forward_world import Maker, open_db

UTC = timezone.utc
LABEL = date(2027, 1, 29)
END = datetime(2027, 1, 29, 22, tzinfo=UTC)
NOW = datetime(2027, 1, 31, 1, tzinfo=UTC)
WEEK = timedelta(weeks=1)


@pytest.fixture
def world(tmp_path):
    conn = open_db(tmp_path / "data" / "agentic.db")
    w = World(tmp_path, conn)
    yield w
    conn.close()


@pytest.fixture
def make(world):
    return Maker(world.conn)


def put(world, name, kind, *, at, origin="staging"):
    meta = world.deploy(name, kind)
    approval = world.request(meta, status="approved", origin=origin, kind=kind)
    world.journal(name, approval, new_target=f".versions/{name}/{meta.artifact_hash}", at=at)
    return meta


def reader(world, **replacements):
    def read(conn):
        inputs = read_model_inputs.collect_inputs(world.plugins, conn, raise_retryable=True)
        return dataclasses.replace(inputs, **replacements)
    return read


def finalize(world, *, now=NOW, label=LABEL, close=END, **replacements):
    return wk.finalize_week(world.conn, world.conn, label, close, now=now,
                            read_inputs=reader(world, **replacements), settings=SETTINGS)


def rows(world, label="2027-01-29"):
    return store.strategies_of(world.conn, label)


def test_an_empty_world_finalizes_a_normal_completed_week(world):
    assert finalize(world) == store.COMMITTED
    week = store.get_week(world.conn, "2027-01-29")
    assert week["status"] == "completed" and week["evaluation"] == "normal"
    assert week["window_end"] == END.isoformat() and week["evaluated_at"] == NOW.isoformat()
    assert week["inventory_state"] == "normal" and week["degraded"] == {}
    assert rows(world) == [] and store.inventory_of(world.conn, "2027-01-29") == []


def test_a_strategy_with_few_decisions_is_stored_insufficient_and_not_degraded(world, make):
    meta = put(world, "strat", "strategy", at=END - 10 * WEEK)
    for i in range(3):
        make.decide(at=END - timedelta(days=i + 1), content_hash=meta.content_hash)
    finalize(world)
    [row] = rows(world)
    assert row["sign"] == "insufficient" and row["state"] == "ok" and row["n"] == 3
    assert row["generation"][0] == 0 and row["content_hash"] == meta.content_hash
    assert row["series"]["all"]["n"] == 3 and row["r_profit"] > 0
    assert store.get_week(world.conn, "2027-01-29")["evaluation"] == "normal"


def test_a_decision_after_the_window_end_does_not_leak_into_the_week(world, make):
    meta = put(world, "strat", "strategy", at=END - 10 * WEEK)
    make.decide(at=END - timedelta(days=1), content_hash=meta.content_hash)
    make.decide(at=END + timedelta(hours=3), content_hash=meta.content_hash)
    finalize(world)
    assert rows(world)[0]["series"]["all"]["n"] == 1


def test_a_result_that_arrived_before_the_evaluation_time_is_scored(world, make):
    meta = put(world, "strat", "strategy", at=END - 10 * WEEK)
    sid = make.decide(at=END - timedelta(hours=30), result="filled",
                      content_hash=meta.content_hash)
    make.finish(sid, result="tp", pnl=50.0, entry=150.0, at=END + timedelta(hours=2))
    world.conn.commit()
    finalize(world)
    assert rows(world)[0]["series"]["all"]["n"] == 1


def test_a_positive_strategy_with_enough_history_is_above_the_floor(world, make):
    meta = put(world, "strat", "strategy", at=END - 20 * WEEK)
    for i in range(32):
        make.decide(at=END - timedelta(days=2 + i), content_hash=meta.content_hash)
    finalize(world)
    [row] = rows(world)
    assert row["sign"] == "above_floor" and row["n"] == 32 and row["reasons"] == []


def test_a_strategy_stopped_before_the_end_is_listed_and_has_no_row(world):
    meta = put(world, "strat", "strategy", at=END - 10 * WEEK)
    world.deploy_op("strat", op_kind="disable_symlink", at=END - WEEK)
    finalize(world)
    assert rows(world) == []
    assert store.get_week(world.conn, "2027-01-29")["stopped"] == [
        {"name": "strat", "kind": "strategy"}]


def test_a_failing_aggregate_is_stored_as_a_failed_row_and_degrades_the_week(
        world, make, monkeypatch):
    put(world, "strat", "strategy", at=END - 10 * WEEK)

    def broken(*args, **kwargs):
        raise ValueError("boom")
    monkeypatch.setattr(sf, "forward_summary", broken)
    finalize(world)
    [row] = rows(world)
    assert row["state"] == "failed" and row["sign"] == "indeterminate"
    week = store.get_week(world.conn, "2027-01-29")
    assert week["evaluation"] == "degraded" and week["degraded"] == {"strategy_failed": 1}


def test_a_busy_database_aborts_the_attempt_and_stores_nothing(world, monkeypatch):
    put(world, "strat", "strategy", at=END - 10 * WEEK)

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(sf, "forward_summary", busy)
    with pytest.raises(sqlite3.OperationalError):
        finalize(world)
    assert store.get_week(world.conn, "2027-01-29") is None


def test_an_unexpected_error_in_the_inventory_rolls_everything_back(world, monkeypatch):
    put(world, "strat", "strategy", at=END - 10 * WEEK)

    def broken(*args, **kwargs):
        raise RuntimeError("inventory exploded")
    monkeypatch.setattr(read_model, "build_read_model", broken)
    with pytest.raises(RuntimeError):
        finalize(world)
    assert store.get_week(world.conn, "2027-01-29") is None
    assert world.conn.execute("SELECT COUNT(*) FROM strategy_review_weekly").fetchone()[0] == 0


def test_a_transition_time_that_cannot_be_read_is_an_undetermined_row_and_a_degraded_week(
        world):
    meta = world.deploy("strat", "strategy")
    approval = world.request(meta, status="approved", kind="strategy")
    world.journal("strat", approval, new_target=f".versions/strat/{meta.artifact_hash}",
                  at="garbage")
    finalize(world)
    [row] = rows(world)
    assert row["state"] == "undetermined" and row["sign"] == "indeterminate"
    assert store.get_week(world.conn, "2027-01-29")["degraded"] == {"strategy_undetermined": 1}


def test_an_unreadable_deployment_record_degrades_the_week_without_strategy_rows(world):
    put(world, "strat", "strategy", at=END - 10 * WEEK)
    finalize(world, journal_status=SOURCE_UNAVAILABLE)
    week = store.get_week(world.conn, "2027-01-29")
    assert rows(world) == []
    assert week["evaluation"] == "degraded"
    assert week["degraded"]["targets_unavailable"] == 1
    assert week["inventory_state"] == "indeterminate"


def test_a_failed_live_scan_makes_the_inventory_indeterminate_and_the_week_degraded(world):
    put(world, "ind", "indicator", at=END - 20 * WEEK)
    finalize(world, live=LiveScan(status=SOURCE_UNAVAILABLE))
    week = store.get_week(world.conn, "2027-01-29")
    assert week["inventory_state"] == "indeterminate"
    assert week["inventory_reason"] == "scan_failed"
    assert week["degraded"] == {"inventory_unavailable": 1}


def test_an_unused_indicator_is_listed_with_its_origin_and_proposability(world):
    put(world, "ind", "indicator", at=END - 20 * WEEK)
    put(world, "hind", "indicator", at=END - 20 * WEEK, origin="human")
    finalize(world)
    items = {r["name"]: r for r in store.inventory_of(world.conn, "2027-01-29")}
    assert items["ind"]["state"] == "unused" and items["ind"]["proposable"] == 1
    assert items["hind"]["proposable"] == 0 and items["hind"]["not_proposable"] == "human_origin"


def test_the_inventory_is_judged_at_the_evaluation_time(world):
    put(world, "ind", "indicator", at=NOW - 7 * WEEK)
    finalize(world, now=NOW, label=date(2027, 1, 22), close=END - WEEK)
    assert store.inventory_of(world.conn, "2027-01-22") == []
    finalize(world, now=NOW + 3 * WEEK)
    assert [r["name"] for r in store.inventory_of(world.conn, "2027-01-29")] == ["ind"]


def test_a_week_is_finalized_only_once(world, make):
    meta = put(world, "strat", "strategy", at=END - 10 * WEEK)
    make.decide(at=END - timedelta(days=1), content_hash=meta.content_hash)
    assert finalize(world) == store.COMMITTED
    make.decide(at=END - timedelta(days=2), content_hash=meta.content_hash)
    assert finalize(world, now=NOW + timedelta(days=1)) == store.EXISTS
    assert rows(world)[0]["n"] == 1
    assert store.get_week(world.conn, "2027-01-29")["evaluated_at"] == NOW.isoformat()


def test_an_aborted_week_is_recorded_without_evaluation(world):
    assert wk.abort_week(world.conn, LABEL, END, now=NOW + 30 * timedelta(days=1)) == store.COMMITTED
    week = store.get_week(world.conn, "2027-01-29")
    assert week["status"] == "aborted" and week["evaluation"] == "unevaluated"
    assert week["abort_reason"] == "period_exceeded" and rows(world) == []


def test_no_money_amount_is_stored_in_the_strategy_rows(world, make):
    meta = put(world, "strat", "strategy", at=END - 10 * WEEK)
    make.decide(at=END - timedelta(days=1), content_hash=meta.content_hash)
    finalize(world)
    columns = [c[1] for c in world.conn.execute("PRAGMA table_info(strategy_review_weekly)")]
    assert not any(word in col for col in columns
                   for word in ("pnl", "amount", "balance", "equity", "money"))
