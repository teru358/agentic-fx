"""終端 snapshot (`improve_run_notices`) の不変性・同一 tx・旧 DB の移行。"""
from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone

import pytest

from agentic_fx import improve_notice_codes as codes
from agentic_fx.store import backlog, improve_runs, improve_waves, missions
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 25, 3, 5, tzinfo=timezone.utc)


def _start(c, *, trigger=None, slot=None, commit=True):
    mid = missions.start(c, "improve", "local", "m", now=NOW, commit=False)
    rid = improve_runs.start(c, None, now=NOW, mission_id=mid, trigger=trigger,
                             commit=False)
    if slot is not None:
        improve_waves.create_wave_and_slots(c, period_key=slot[0], now=NOW,
                                            expected=1)
        improve_waves.claim_slot(c, period_key=slot[0], k=slot[1],
                                 mission_id=mid, now=NOW, commit=False)
    if commit:
        c.commit()
    return mid, rid


def _finish(c, mid, rid, notice=codes.NOTICE_COMMIT_CRASHED, **kw):
    kw.setdefault("tool_calls", None)
    missions.finish_improve_mission(
        c, mission_id=mid, run_id=rid, slot_key=None, mission_status="failed",
        run_result=None, backlog_transition=None, now=LATER, notice=notice, **kw)


def _snap(c, mid):
    return c.execute("SELECT * FROM improve_run_notices WHERE mission_id=?",
                     (mid,)).fetchone()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    yield c
    c.close()


def test_notice_and_tool_calls_have_no_default():
    params = inspect.signature(missions.finish_improve_mission).parameters
    assert params["notice"].default is inspect.Parameter.empty
    assert params["tool_calls"].default is inspect.Parameter.empty


def test_finish_writes_snapshot_in_same_call(conn):
    mid, rid = _start(conn, trigger="scheduled")
    _finish(conn, mid, rid, codes.NOTICE_PYTEST_FAILED, tool_calls=7,
            approval_id=None)
    row = _snap(conn, mid)
    assert (row["trigger"], row["result_code"], row["reason_code"],
            row["tool_calls"], row["observation_text"], row["approval_id"],
            row["created_at"]) == (
        "scheduled", "gate_failed", "pytest_failed", 7, None, None,
        LATER.isoformat())


def test_snapshot_trigger_null_run_is_manual(conn):
    mid, rid = _start(conn, trigger=None)
    _finish(conn, mid, rid)
    assert _snap(conn, mid)["trigger"] == "manual"


def test_snapshot_without_matching_run_row_is_rejected_by_run_cas(conn):
    mid, rid = _start(conn)
    with pytest.raises(RuntimeError):
        _finish(conn, mid, rid + 100)
    conn.rollback()
    assert _snap(conn, mid) is None


def test_snapshot_carries_approval_id(conn):
    mid, rid = _start(conn, trigger="manual")
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, created_at) "
        "VALUES ('plugin','{}','pending',?)", (NOW.isoformat(),))
    aid = conn.execute("SELECT id FROM approval_requests").fetchone()["id"]
    missions.finish_improve_mission(
        conn, mission_id=mid, run_id=rid, slot_key=None,
        mission_status="completed", run_result="approval",
        backlog_transition=None, now=LATER, approval_id=aid,
        notice=codes.NOTICE_SUBMITTED, tool_calls=3)
    assert _snap(conn, mid)["approval_id"] == aid


@pytest.mark.parametrize("stmt", [
    "UPDATE improve_run_notices SET tool_calls=1",
    "UPDATE improve_run_notices SET observation_text='x'",
    "DELETE FROM improve_run_notices",
])
def test_snapshot_is_append_only(conn, stmt):
    mid, rid = _start(conn)
    _finish(conn, mid, rid)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(stmt)
    assert _snap(conn, mid)["result_code"] == "failed"


def test_cas_failure_raises_and_adds_no_snapshot(conn):
    mid, rid = _start(conn)
    _finish(conn, mid, rid, codes.NOTICE_COMMIT_CRASHED, tool_calls=4)
    before = dict(_snap(conn, mid))
    with pytest.raises(RuntimeError, match="not 'running'"):
        _finish(conn, mid, rid, codes.NOTICE_LAUNCH_CRASHED, tool_calls=9)
    conn.rollback()
    rows = conn.execute("SELECT * FROM improve_run_notices").fetchall()
    assert len(rows) == 1 and dict(rows[0]) == before


def test_rollback_removes_snapshot_with_mission_end(conn):
    mid, rid = _start(conn)
    conn.execute("BEGIN IMMEDIATE")
    _finish(conn, mid, rid, commit=False)
    assert _snap(conn, mid) is not None
    conn.rollback()
    assert _snap(conn, mid) is None
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (mid,)).fetchone()["status"] == "running"


@pytest.mark.parametrize("notice", [
    codes.NoticeCode("failed", "pf:7.7777"),
    codes.NoticeCode("failed", ":"),
    codes.NoticeCode("failed", "gate_other"),
    codes.NoticeCode("gate_failed", "timeout"),
    codes.NoticeCode("done", None),
])
def test_invalid_pair_is_value_error_and_leaves_mission_running(conn, notice):
    mid, rid = _start(conn)
    with pytest.raises(ValueError):
        _finish(conn, mid, rid, notice)
    conn.rollback()
    assert _snap(conn, mid) is None
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (mid,)).fetchone()["status"] == "running"


@pytest.mark.parametrize("bad", [-1, True, 1.5, "3", 1_000_001])
def test_invalid_tool_calls_is_value_error(conn, bad):
    mid, rid = _start(conn)
    with pytest.raises(ValueError):
        _finish(conn, mid, rid, tool_calls=bad)


@pytest.mark.parametrize("ok", [0, 1_000_000, None])
def test_tool_calls_boundaries_are_stored(conn, ok):
    mid, rid = _start(conn)
    _finish(conn, mid, rid, tool_calls=ok)
    assert _snap(conn, mid)["tool_calls"] == ok


@pytest.mark.parametrize("size,expected", [(1999, 1999), (2000, 2000),
                                           (2001, 2000), (5000, 2000)])
def test_observation_text_is_truncated_by_code_points(conn, size, expected):
    mid, rid = _start(conn)
    text = "あ" * size
    _finish(conn, mid, rid, codes.NOTICE_OBSERVATION, observation_text=text)
    assert _snap(conn, mid)["observation_text"] == "あ" * expected
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (mid,)).fetchone()["status"] == "failed"


def test_observation_text_is_scrubbed_of_urls_and_keys(conn):
    mid, rid = _start(conn)
    _finish(conn, mid, rid, codes.NOTICE_OBSERVATION,
            observation_text="see https://example.com/x?token=abc apikey=SECRET1")
    stored = _snap(conn, mid)["observation_text"]
    assert "example.com" not in stored and "SECRET1" not in stored


def test_observation_text_only_for_observation(conn):
    mid, rid = _start(conn)
    with pytest.raises(ValueError):
        _finish(conn, mid, rid, codes.NOTICE_COMMIT_CRASHED,
                observation_text="x")


def test_later_backlog_rewrite_does_not_change_snapshot(conn):
    bid = backlog.add(conn, "idea", "user", NOW)
    mid, rid = _start(conn)
    conn.execute("BEGIN IMMEDIATE")
    missions.finish_improve_mission(
        conn, mission_id=mid, run_id=rid, slot_key=None,
        mission_status="completed", run_result=None, now=LATER,
        backlog_transition={"backlog_id": bid, "status": "observation",
                            "last_result": "observation:first"},
        notice=codes.NOTICE_OBSERVATION, tool_calls=2,
        observation_text="first", commit=True)
    backlog.set_status(conn, bid, "observation", LATER,
                       last_result="observation:rewritten")
    assert _snap(conn, mid)["observation_text"] == "first"


# --- recover_interrupted ---------------------------------------------------

def test_recover_interrupted_writes_snapshot_with_run_trigger(conn):
    m_sched, _ = _start(conn, trigger="scheduled", slot=("2026-W43", 0))
    m_manual, _ = _start(conn, trigger="manual")
    m_null, _ = _start(conn, trigger=None)
    missions.recover_interrupted(conn, now=LATER, max_requeue=3)
    for mid, want in ((m_sched, "scheduled"), (m_manual, "manual"),
                      (m_null, "manual")):
        row = _snap(conn, mid)
        assert (row["trigger"], row["result_code"], row["reason_code"],
                row["tool_calls"], row["observation_text"],
                row["approval_id"], row["created_at"]) == (
            want, "failed", "interrupted", None, None, None, LATER.isoformat())
        assert conn.execute("SELECT status FROM missions WHERE id=?",
                            (mid,)).fetchone()["status"] == "interrupted"


def test_recover_interrupted_trigger_survives_slot_cleanup(conn):
    mid, _ = _start(conn, trigger="scheduled", slot=("2026-W43", 0))
    missions.recover_interrupted(conn, now=LATER, max_requeue=3)
    slot = improve_waves.get_slot(conn, period_key="2026-W43", k=0)
    assert slot["mission_id"] is None
    assert _snap(conn, mid)["trigger"] == "scheduled"


def test_recover_interrupted_improve_mission_without_run_row_is_manual(conn):
    mid = missions.start(conn, "improve", "local", "m", now=NOW)
    missions.recover_interrupted(conn, now=LATER, max_requeue=3)
    row = _snap(conn, mid)
    assert (row["trigger"], row["reason_code"]) == ("manual", "interrupted")


def test_recover_interrupted_with_already_finished_run_row(conn):
    mid, rid = _start(conn, trigger="scheduled")
    improve_runs.finish(conn, rid, result=None, now=LATER)
    missions.recover_interrupted(conn, now=LATER, max_requeue=3)
    assert _snap(conn, mid)["trigger"] == "scheduled"


def test_recover_interrupted_reads_improvement_runs_with_a_single_select(conn):
    _start(conn, trigger="scheduled")
    _start(conn, trigger="manual")
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        missions.recover_interrupted(conn, now=LATER, max_requeue=3)
    finally:
        conn.set_trace_callback(None)
    selects = [q for q in statements
               if q.lstrip().upper().startswith("SELECT")
               and "FROM improvement_runs" in q]
    assert len(selects) == 1, selects


def test_recover_interrupted_skips_non_improve_missions(conn):
    missions.start(conn, "trade", "local", "m", now=NOW, trigger="cron")
    missions.recover_interrupted(conn, now=LATER, max_requeue=3)
    assert conn.execute(
        "SELECT COUNT(*) c FROM improve_run_notices").fetchone()["c"] == 0


def test_recover_interrupted_rolls_back_snapshots_with_the_rest(conn, monkeypatch):
    mid, _ = _start(conn, trigger="manual")

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(improve_waves, "recover_stale_slots", boom)
    with pytest.raises(RuntimeError):
        missions.recover_interrupted(conn, now=LATER, max_requeue=3)
    assert _snap(conn, mid) is None
    assert conn.execute("SELECT status FROM missions WHERE id=?",
                        (mid,)).fetchone()["status"] == "running"


# --- 旧 DB への init_db ----------------------------------------------------

def _all_rows(c):
    out = {}
    for t in (r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")):
        if t == "improve_run_notices":
            continue
        cols = [r["name"] for r in c.execute(f"PRAGMA table_info({t})")
                if r["name"] != "trigger" or t == "missions"]
        out[t] = [tuple(r) for r in c.execute(
            f"SELECT {','.join(cols)} FROM {t} ORDER BY 1")]
    return out


def _triggers(c):
    return {r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}


def test_init_db_on_old_db_adds_table_triggers_and_column(tmp_path):
    c = connect(tmp_path / "old.db")
    init_db(c)
    mid, rid = _start(c, trigger="scheduled")
    c.execute("DROP TABLE improve_run_notices")
    c.execute("ALTER TABLE improvement_runs DROP COLUMN trigger")
    c.commit()
    assert "improve_run_notices_no_update" not in _triggers(c)
    before = _all_rows(c)

    init_db(c)

    assert {"improve_run_notices_no_update",
            "improve_run_notices_no_delete"} <= _triggers(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(improvement_runs)")}
    assert "trigger" in cols
    assert c.execute("SELECT COUNT(*) c FROM improve_run_notices"
                     ).fetchone()["c"] == 0
    assert _all_rows(c) == before
    assert c.execute("SELECT trigger FROM improvement_runs WHERE id=?",
                     (rid,)).fetchone()["trigger"] is None
    init_db(c)
    assert _all_rows(c) == before


def test_init_db_on_fk_less_old_db_keeps_trigger_column(tmp_path):
    """mission_id に FK が無い旧形の improvement_runs は作り直される。その後でも
    trigger 列が残り、既存行の値は変わらない。"""
    from agentic_fx.store import db as db_module

    cols = ("id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "backlog_id INTEGER REFERENCES improvement_backlog(id),"
            "result TEXT CHECK (result IN ('approval','report')),"
            "approval_id INTEGER, report_path TEXT, mission_id INTEGER,"
            "started_at TEXT NOT NULL, finished_at TEXT,"
            "report_state TEXT NOT NULL DEFAULT 'none' "
            "CHECK(report_state IN ('none','prepared','published','failed'))")
    c = connect(tmp_path / "legacy.db")
    c.execute(f"CREATE TABLE improvement_runs ({cols})")
    c.commit()
    c.executescript(db_module._SCHEMA)
    c.execute(
        "INSERT INTO missions (id, loop, runner, model, status, started_at) "
        "VALUES (1, 'improve', 'local', 'm', 'running', ?)", (NOW.isoformat(),))
    c.execute(
        "INSERT INTO improvement_runs (mission_id, result, started_at, "
        "report_state) VALUES (1, 'report', ?, 'published')", (NOW.isoformat(),))
    c.commit()

    init_db(c)

    assert any(fk["table"] == "missions" and fk["from"] == "mission_id"
               for fk in c.execute("PRAGMA foreign_key_list(improvement_runs)"))
    row = c.execute("SELECT * FROM improvement_runs").fetchone()
    assert "trigger" in row.keys()
    assert row["trigger"] is None
    assert (row["mission_id"], row["result"], row["report_state"]) == (
        1, "report", "published")
    mid, rid = _start(c, trigger="scheduled")
    assert c.execute("SELECT trigger FROM improvement_runs WHERE id=?",
                     (rid,)).fetchone()["trigger"] == "scheduled"


def test_fresh_db_has_trigger_column_and_table(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(improvement_runs)")}
    assert "trigger" in cols
    assert "improve_run_notices" in {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
