"""成績応答の機械採点 (配備中の戦略ごとの前方成績) の部分。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from agentic_fx.ops import performance_view as pv
from agentic_fx.ops.contracts import ErrorCode, OpsError

from tests.plugin.read_model_world import World
from tests.store.forward_world import Maker
from .test_performance_strategies import put
from .test_performance_view import FREE, KILL, NOW


def read(env, range_key="30d", *, plugins=True, interrupt=None):
    return pv.read_performance(
        env.db_path, range_key=range_key, now=NOW, kill_pct=KILL, account_currency="JPY",
        kill_switch=FREE, state_fetched_at=NOW, interrupt=interrupt,
        plugins_root=env.plugins_root if plugins else None)


def deploy(env, name="alpha", at=None):
    world = World(env.root, conn=env.conn)
    meta = world.deploy(name, kind="strategy")
    approval = world.request(meta, status="approved")
    world.journal(name, approval, new_target=f".versions/{name}/{meta.artifact_hash}",
                  at=at or NOW - timedelta(weeks=6))
    return meta


def decisions(env, meta, count=3, *, name="alpha"):
    make = Maker(env.conn)
    for i in range(count):
        make.decide(at=NOW - timedelta(days=i + 1), plugin=name,
                    content_hash=meta.content_hash, pnl=100.0)


def forward(data):
    return {row["name"]: row for row in data["forward"]["strategies"]}


def test_without_the_plugin_directory_the_machine_scores_are_unavailable_not_empty(ops_env):
    put(ops_env.conn)
    data = read(ops_env, plugins=False)
    assert data["forward"]["state"] == "unavailable" and data["forward"]["strategies"] == []
    assert len(data["strategies"]) == 1                    # 実取引の側は読める


def test_a_deployed_strategy_carries_its_machine_score(ops_env):
    meta = deploy(ops_env)
    decisions(ops_env, meta, 6)
    data = read(ops_env)
    assert data["forward"]["state"] == "ok"
    row = forward(data)["alpha"]
    assert row["state"] == "ok" and row["series"]["all"]["n"] == 6
    assert row["window"]["weeks"] == 12 and row["generation"]["source"] == "journal"
    assert data["strategies"] == []                         # 実取引は別の系列


def test_the_machine_score_does_not_follow_the_range_of_the_real_trades(ops_env):
    meta = deploy(ops_env)
    decisions(ops_env, meta, 3)
    short, whole = read(ops_env, "7d"), read(ops_env, "all")
    assert forward(short)["alpha"]["series"] == forward(whole)["alpha"]["series"]
    assert forward(short)["alpha"]["window"]["start"] == forward(whole)["alpha"]["window"]["start"]


def test_a_strategy_without_real_orders_still_has_its_row(ops_env):
    meta = deploy(ops_env)
    decisions(ops_env, meta, 1)
    put(ops_env.conn, trigger="signal:other")
    data = read(ops_env)
    assert [r["origin"] for r in data["strategies"]] == ["signal:other"]
    assert "alpha" in forward(data)


def test_a_version_that_was_replaced_is_not_counted_for_the_live_one(ops_env):
    meta = deploy(ops_env)
    decisions(ops_env, meta, 2)
    Maker(ops_env.conn).decide(at=NOW - timedelta(days=1), plugin="alpha",
                               content_hash="f" * 16)
    assert forward(read(ops_env))["alpha"]["decisions"]["total"] == 2


def test_an_unreadable_journal_makes_the_scores_undetermined_and_keeps_the_real_trades(ops_env):
    meta = deploy(ops_env)
    decisions(ops_env, meta, 3)
    put(ops_env.conn)
    ops_env.conn.execute("DROP TABLE plugin_switch_journal")
    ops_env.conn.commit()
    data = read(ops_env)
    row = forward(data)["alpha"]
    assert row["state"] == "undetermined" and row["reason"] == "journal_unreadable"
    assert "series" not in row
    assert len(data["strategies"]) == 1


def test_the_rows_are_limited_and_the_omitted_count_is_returned(ops_env, monkeypatch):
    monkeypatch.setattr(pv, "MAX_FORWARD_ROWS", 2)
    for name in ("alpha", "bravo", "charlie"):
        deploy(ops_env, name)
    data = read(ops_env)
    assert [r["name"] for r in data["forward"]["strategies"]] == ["alpha", "bravo"]
    assert data["forward"]["omitted"] == 1


def test_a_stop_while_reading_the_scores_refuses_the_response(ops_env, monkeypatch):
    meta = deploy(ops_env)
    decisions(ops_env, meta, 2)
    error = OpsError(ErrorCode.UNAVAILABLE)
    stopped = []
    real = pv.signal_forward.forward_summary

    def summary_then_stop(*args, **kwargs):
        result = real(*args, **kwargs)
        stopped.append(True)
        return result

    monkeypatch.setattr(pv.signal_forward, "forward_summary", summary_then_stop)
    with pytest.raises(OpsError) as raised:
        read(ops_env, interrupt=lambda: error if stopped else None)
    assert stopped and raised.value.code is ErrorCode.UNAVAILABLE


def test_the_response_carries_counts_and_values_only(ops_env):
    meta = deploy(ops_env)
    decisions(ops_env, meta, 2)
    row = forward(read(ops_env))["alpha"]
    assert set(row) == {"name", "content_hash", "state", "reason", "generation", "window",
                        "thresholds", "series", "decisions", "outcomes", "holes", "reach",
                        "concentration", "comparison"}


def test_a_local_sql_failure_of_one_strategy_makes_only_that_row_unavailable(ops_env, monkeypatch):
    import sqlite3
    for name in ("alpha", "bravo"):
        meta = deploy(ops_env, name)
        decisions(ops_env, meta, 2, name=name)
    real = pv.signal_forward.forward_summary

    def broken_for_alpha(conn, target, **kwargs):
        if target.name == "alpha":
            raise sqlite3.OperationalError("database disk image is malformed")
        return real(conn, target, **kwargs)

    monkeypatch.setattr(pv.signal_forward, "forward_summary", broken_for_alpha)
    data = read(ops_env)
    rows = forward(data)
    assert rows["alpha"]["state"] == "failed" and rows["alpha"]["reason"] == "summary_failed"
    assert rows["bravo"]["state"] == "ok" and rows["bravo"]["decisions"]["total"] == 2
    assert data["forward"]["state"] == "ok"


def test_an_interrupted_query_still_refuses_the_whole_response(ops_env, monkeypatch):
    import sqlite3
    meta = deploy(ops_env)
    decisions(ops_env, meta, 2)
    error = OpsError(ErrorCode.UNAVAILABLE)
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(True)
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(pv.signal_forward, "forward_summary", interrupted)
    with pytest.raises(OpsError) as raised:
        read(ops_env, interrupt=lambda: error if calls else None)
    assert raised.value.code is ErrorCode.UNAVAILABLE
