"""週次の確定の保存: 1 transaction・追記のみ・一意な週・新規の移行。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from agentic_fx.store import weekly_review as wr
from agentic_fx.store.db import connect, init_db

UTC = timezone.utc
END = datetime(2027, 1, 29, 22, tzinfo=UTC)
DONE = datetime(2027, 1, 31, 1, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "w.db")
    init_db(c)
    yield c
    c.close()


def week(label="2027-01-29", **kw):
    base = dict(week=label, window_end=END, finalized_at=DONE, evaluated_at=DONE,
                status="completed", evaluation="normal", degraded={},
                inventory_state="normal", inventory_reason=None, stopped=[],
                abort_reason=None)
    base.update(kw)
    return wr.WeekRecord(**base)


def strategy(name="s1", **kw):
    base = dict(name=name, content_hash="h" * 16, generation_source=0, generation_op_id=1,
                generation_started_at=END, state="ok", state_reason=None,
                first_decided_at=END, n=3, wins=2, losses=1, flats=0, r_profit=2.0,
                r_loss=1.0, series={"all": {}}, holes={}, decisions={}, concentration={},
                short_term={}, approval=None, sign="insufficient", reasons=[])
    base.update(kw)
    return wr.StrategyRow(**base)


def inv(name, state="unused", **kw):
    base = dict(name=name, state=state, origin="staging", conditions=[], proposable=True,
                not_proposable=None)
    base.update(kw)
    return wr.InventoryRow(**base)


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_a_completed_week_is_stored_with_its_strategy_and_inventory_rows(conn):
    out = wr.commit_week(conn, week(), [strategy()], [inv("a")])
    assert out == wr.COMMITTED
    assert count(conn, "review_weeks") == 1
    assert count(conn, "strategy_review_weekly") == 1
    assert count(conn, "plugin_inventory_weekly") == 1


def test_the_three_tables_are_one_transaction(conn):
    # 同じ (名前, hash, 世代) を 2 行入れると 2 行目が一意制約で落ちる。週の行も残らない。
    with pytest.raises(sqlite3.IntegrityError):
        wr.commit_week(conn, week(), [strategy(), strategy()], [])
    assert count(conn, "review_weeks") == 0
    assert count(conn, "strategy_review_weekly") == 0
    assert not conn.in_transaction


def test_an_inventory_failure_also_rolls_back_the_strategy_rows(conn):
    with pytest.raises(sqlite3.IntegrityError):
        wr.commit_week(conn, week(), [strategy()], [inv("a"), inv("a")])
    assert count(conn, "review_weeks") == 0 and count(conn, "strategy_review_weekly") == 0


def test_a_second_commit_for_the_same_week_changes_nothing(conn):
    wr.commit_week(conn, week(), [strategy()], [])
    again = wr.commit_week(conn, week(evaluation="degraded", degraded={"x": 1}),
                           [strategy("other")], [inv("z")])
    assert again == wr.EXISTS
    assert count(conn, "strategy_review_weekly") == 1 and count(conn, "plugin_inventory_weekly") == 0
    assert wr.get_week(conn, "2027-01-29")["evaluation"] == "normal"


@pytest.mark.parametrize("table,column", [
    ("review_weeks", "evaluation"), ("strategy_review_weekly", "n"),
    ("plugin_inventory_weekly", "origin")])
def test_stored_rows_cannot_be_updated_or_deleted(conn, table, column):
    wr.commit_week(conn, week(), [strategy()], [inv("a")])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE {table} SET {column}={column}")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"DELETE FROM {table}")


def test_an_aborted_week_has_no_evaluation_and_no_rows(conn):
    out = wr.commit_aborted(conn, "2027-01-29", window_end=END, finalized_at=DONE,
                            reason=wr.ABORT_PERIOD_EXCEEDED)
    assert out == wr.COMMITTED
    row = wr.get_week(conn, "2027-01-29")
    assert row["status"] == "aborted" and row["evaluation"] == "unevaluated"
    assert row["evaluated_at"] is None and row["inventory_state"] == "none"
    assert row["abort_reason"] == wr.ABORT_PERIOD_EXCEEDED
    assert count(conn, "strategy_review_weekly") == 0


def test_an_aborted_record_cannot_carry_an_evaluation(conn):
    with pytest.raises(sqlite3.IntegrityError):
        wr.commit_week(conn, week(status="aborted", abort_reason="x"), [], [])


def test_a_week_that_is_not_stored_reads_as_none(conn):
    assert wr.get_week(conn, "2027-01-29") is None
    assert wr.recorded_weeks(conn) == set()


def test_degraded_reasons_and_stopped_names_round_trip(conn):
    wr.commit_week(conn, week(evaluation="degraded", degraded={"strategy_failed": 2},
                              stopped=[{"name": "old", "kind": "strategy"}]), [], [])
    row = wr.get_week(conn, "2027-01-29")
    assert row["degraded"] == {"strategy_failed": 2}
    assert row["stopped"] == [{"name": "old", "kind": "strategy"}]


def test_the_first_inventory_marks_nothing_new(conn):
    wr.commit_week(conn, week(), [], [inv("a"), inv("b")])
    assert [r["is_new"] for r in wr.inventory_of(conn, "2027-01-29")] == [0, 0]


def test_a_move_to_unused_since_the_previous_normal_week_is_new(conn):
    wr.commit_week(conn, week("2027-01-22"), [], [inv("a")])
    wr.commit_week(conn, week("2027-01-29"), [], [inv("a"), inv("b")])
    rows = {r["name"]: r["is_new"] for r in wr.inventory_of(conn, "2027-01-29")}
    assert rows == {"a": 0, "b": 1}


def test_an_indeterminate_previous_row_counts_as_a_move(conn):
    wr.commit_week(conn, week("2027-01-22"), [], [inv("a", state="indeterminate",
                                                     proposable=False)])
    wr.commit_week(conn, week("2027-01-29"), [], [inv("a")])
    assert wr.inventory_of(conn, "2027-01-29")[0]["is_new"] == 1


def test_a_week_with_an_indeterminate_inventory_is_not_a_comparison_base(conn):
    wr.commit_week(conn, week("2027-01-15"), [], [inv("a")])
    wr.commit_week(conn, week("2027-01-22", inventory_state="indeterminate",
                              inventory_reason="scan_failed"), [], [])
    wr.commit_week(conn, week("2027-01-29"), [], [inv("a"), inv("b")])
    rows = {r["name"]: r["is_new"] for r in wr.inventory_of(conn, "2027-01-29")}
    assert rows == {"a": 0, "b": 1}


def test_an_indeterminate_inventory_has_no_new_marks_and_no_names(conn):
    wr.commit_week(conn, week("2027-01-22"), [], [inv("a")])
    wr.commit_week(conn, week("2027-01-29", inventory_state="indeterminate",
                              inventory_reason="scan_failed"), [], [inv("b")])
    assert [r["is_new"] for r in wr.inventory_of(conn, "2027-01-29")] == [0]


def test_an_aborted_week_is_not_a_comparison_base(conn):
    wr.commit_week(conn, week("2027-01-15"), [], [inv("a")])
    wr.commit_aborted(conn, "2027-01-22", window_end=END, finalized_at=DONE,
                      reason=wr.ABORT_PERIOD_EXCEEDED)
    wr.commit_week(conn, week("2027-01-29"), [], [inv("a")])
    assert [r["is_new"] for r in wr.inventory_of(conn, "2027-01-29")] == [0]


def test_strategy_rows_are_read_back_as_python_values(conn):
    wr.commit_week(conn, week(), [strategy(series={"all": {"n": 3}}, reasons=["x"])], [])
    row = wr.strategies_of(conn, "2027-01-29")[0]
    assert row["series"] == {"all": {"n": 3}} and row["reasons"] == ["x"]
    assert row["generation"] == (0, 1)


def test_recorded_weeks_lists_every_stored_week(conn):
    wr.commit_week(conn, week("2027-01-22"), [], [])
    wr.commit_aborted(conn, "2027-01-29", window_end=END, finalized_at=DONE,
                      reason=wr.ABORT_PERIOD_EXCEEDED)
    assert wr.recorded_weeks(conn) == {"2027-01-22", "2027-01-29"}
