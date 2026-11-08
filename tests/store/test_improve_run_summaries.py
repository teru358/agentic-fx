"""改善 mission の要約の表 (`improve_run_summaries`): 制約・同一 tx・移行・回収。"""
from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone

import pytest

from agentic_fx import improve_notice_codes as codes
from agentic_fx.store import improve_run_summaries as summaries
from agentic_fx.store import improve_runs, missions
from agentic_fx.store.db import TABLE_NAMES, connect, init_db

NOW = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 11, 8, 3, 5, tzinfo=timezone.utc)
OK = summaries.SummaryRecord(summaries.STATE_OK, '{"observation":"x"}')


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    yield c
    c.close()


def _start(c, *, trigger="manual"):
    mid = missions.start(c, "improve", "local", "m", now=NOW, commit=False)
    rid = improve_runs.start(c, None, now=NOW, mission_id=mid, trigger=trigger,
                             commit=False)
    c.commit()
    return mid, rid


def _finish(c, mid, rid, summary=OK, output=None):
    missions.finish_improve_mission(
        c, mission_id=mid, run_id=rid, slot_key=None, mission_status="failed",
        run_result=None, backlog_transition=None, now=LATER,
        notice=codes.NOTICE_COMMIT_CRASHED, tool_calls=None,
        summary=summary, output=output)


def _row(c, mid):
    return c.execute("SELECT * FROM improve_run_summaries WHERE mission_id=?",
                     (mid,)).fetchone()


def test_summary_has_no_default_in_the_signature():
    params = inspect.signature(missions.finish_improve_mission).parameters
    assert params["summary"].default is inspect.Parameter.empty
    assert params["summary"].kind is inspect.Parameter.KEYWORD_ONLY


def test_table_is_registered():
    assert "improve_run_summaries" in TABLE_NAMES


def test_finish_writes_one_row_in_the_same_call(conn):
    mid, rid = _start(conn)
    _finish(conn, mid, rid)
    row = _row(conn, mid)
    assert (row["summary_version"], row["state"], row["summary_json"],
            row["created_at"]) == (1, "ok", OK.summary_json, LATER.isoformat())


def test_mission_id_is_a_foreign_key(conn):
    with pytest.raises(sqlite3.IntegrityError):
        summaries.insert(conn, 9999, OK, now=NOW)
        conn.commit()


@pytest.mark.parametrize("stmt", [
    "UPDATE improve_run_summaries SET state='empty'",
    "UPDATE improve_run_summaries SET summary_json='{}'",
    "DELETE FROM improve_run_summaries",
])
def test_rows_are_append_only(conn, stmt):
    mid, rid = _start(conn)
    _finish(conn, mid, rid)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(stmt)
    assert _row(conn, mid)["state"] == "ok"


def test_state_is_a_closed_set(conn):
    mid, _ = _start(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO improve_run_summaries VALUES (?,?,?,?,?)",
            (mid, 1, "weird", "{}", NOW.isoformat()))
    with pytest.raises(ValueError):
        summaries.SummaryRecord("weird", "{}")


def test_second_finish_adds_no_row(conn):
    mid, rid = _start(conn)
    _finish(conn, mid, rid)
    before = dict(_row(conn, mid))
    with pytest.raises(RuntimeError, match="not 'running'"):
        _finish(conn, mid, rid, summaries.EMPTY_SUMMARY)
    conn.rollback()
    rows = conn.execute("SELECT * FROM improve_run_summaries").fetchall()
    assert [dict(r) for r in rows] == [before]


def test_insert_failure_rolls_the_whole_terminal_tx_back(conn):
    other, orid = _start(conn)
    conn.execute("BEGIN IMMEDIATE")
    summaries.insert(conn, other, OK, now=NOW)  # 先に同じ行があり INSERT が衝突
    with pytest.raises(sqlite3.IntegrityError):
        _finish(conn, other, orid)
    conn.rollback()
    status = conn.execute("SELECT status FROM missions WHERE id=?",
                          (other,)).fetchone()["status"]
    assert status == "running"
    assert conn.execute("SELECT COUNT(*) AS c FROM improve_run_notices "
                        "WHERE mission_id=?", (other,)).fetchone()["c"] == 0
    assert _row(conn, other) is None


def test_init_db_is_idempotent_and_adds_the_table_to_an_existing_db(tmp_path):
    path = tmp_path / "old.db"
    c = connect(path)
    init_db(c)
    mid, rid = _start(c)
    c.execute("DROP TABLE improve_run_summaries")
    c.commit()
    assert c.execute("SELECT name FROM sqlite_master WHERE name="
                     "'improve_run_summaries'").fetchone() is None
    init_db(c)
    schema_once = [tuple(r) for r in c.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name LIKE "
        "'improve_run_summaries%' ORDER BY name")]
    init_db(c)
    schema_twice = [tuple(r) for r in c.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name LIKE "
        "'improve_run_summaries%' ORDER BY name")]
    assert schema_once == schema_twice
    assert {r[1] for r in schema_once} == {
        "improve_run_summaries", "improve_run_summaries_no_update",
        "improve_run_summaries_no_delete"}
    assert _row(c, mid) is None  # 古い mission は行を持たない
    c.close()


def test_recover_interrupted_writes_an_empty_row_per_improve_mission(conn):
    m1, _ = _start(conn)
    m2, _ = _start(conn, trigger="scheduled")
    trade = missions.start(conn, "trade", "local", "m", now=NOW)
    missions.recover_interrupted(conn, now=LATER, max_requeue=3)
    for mid in (m1, m2):
        row = _row(conn, mid)
        assert (row["state"], row["summary_json"]) == ("empty", "{}")
    assert _row(conn, trade) is None


def test_recover_interrupted_keeps_an_existing_row(conn):
    mid, _ = _start(conn)
    summaries.insert(conn, mid, OK, now=NOW)
    conn.commit()
    missions.recover_interrupted(conn, now=LATER, max_requeue=3)
    assert _row(conn, mid)["state"] == "ok"


def test_recover_interrupted_row_and_terminal_share_a_tx(conn, monkeypatch):
    mid, _ = _start(conn)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(summaries, "insert", boom)
        with pytest.raises(sqlite3.OperationalError):
            missions.recover_interrupted(conn, now=LATER, max_requeue=3)
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (mid,)).fetchone()["status"] == "running"


def test_output_json_is_written_by_the_terminal_tx(conn):
    mid, rid = _start(conn)
    _finish(conn, mid, rid, output={"selected": {"idea": "x"}})
    row = conn.execute("SELECT output_json, transcript_json FROM missions "
                       "WHERE id=?", (mid,)).fetchone()
    assert row["output_json"] == '{"selected": {"idea": "x"}}'
    assert row["transcript_json"] == "[]"
