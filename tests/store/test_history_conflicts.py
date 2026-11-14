"""history_conflicts の表・同一性・事前遮断器・batch 原子性 (実 sqlite)。"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.store import history_conflicts as hc
from agentic_fx.store import ohlcv
from agentic_fx.store.db import TABLE_NAMES, connect, init_db

T0 = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SEEN = datetime(2026, 11, 14, 0, 0, tzinfo=timezone.utc)
SEEN2 = datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _t(i):
    return (T0 + timedelta(minutes=i)).isoformat()


def _row(i, close=148.1, volume=100.0, spread=0.012, symbol="USDJPY", opn=148.0):
    return (symbol, "1m", _t(i), opn, 148.2, 147.9, close, volume, spread)


def _seed(conn, rows, source="mt5"):
    res = ohlcv.import_history_bars(conn, rows, source=source)
    assert res.conflicted == 0
    return res


def _cls(conn, rows, source="mt5"):
    return ohlcv.classify_history_rows(conn, rows, source=source)


def _apply(conn, cls, **kw):
    kw.setdefault("origin", "import")
    kw.setdefault("seen_at", SEEN)
    return ohlcv.apply_classified_history(conn, cls, **kw)


def _records(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM history_conflicts ORDER BY id")]


def _record_known(conn, rows, state="kept"):
    """既存行に対する衝突記録を直接置く (遮断器を通さずに既知の記録を作る)。"""
    for row in rows:
        existing = conn.execute(
            "SELECT open, high, low, close, volume, spread FROM ohlcv_history "
            "WHERE symbol=? AND interval=? AND bar_time=? AND source='mt5'",
            (row[0], row[1], row[2])).fetchone()
        hc.upsert(conn, source="mt5", interval=row[1], symbol=row[0], bar_time=row[2],
                  history_epoch="none", existing=tuple(existing), incoming=row[3:],
                  origin="import", seen_at=SEEN.isoformat())
    conn.execute("UPDATE history_conflicts SET state=?", (state,))
    conn.commit()


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---- DDL / migration -----------------------------------------------------

def test_fresh_ddl_has_final_shape(tmp_path):
    conn = _conn(tmp_path)
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(history_conflicts)")}
    assert list(cols) == [
        "id", "source", "interval", "symbol", "bar_time", "history_epoch",
        "existing_json", "incoming_json", "existing_digest", "incoming_digest",
        "origin", "last_origin", "state", "first_seen_at", "last_seen_at",
        "seen_count", "first_run_id", "last_run_id", "resolved_at", "resolved_by",
        "resolution_action", "reason_ref", "revision_id"]
    for name in ("first_run_id", "last_run_id", "resolved_at", "resolved_by",
                 "resolution_action", "reason_ref", "revision_id"):
        assert cols[name]["notnull"] == 0, name
    for name in ("source", "interval", "symbol", "bar_time", "history_epoch",
                 "existing_json", "incoming_json", "existing_digest",
                 "incoming_digest", "origin", "last_origin", "state",
                 "first_seen_at", "last_seen_at", "seen_count"):
        assert cols[name]["notnull"] == 1, name
    assert cols["state"]["dflt_value"] == "'open'"
    uniques = [[c["name"] for c in conn.execute(f"PRAGMA index_info({i['name']})")]
               for i in conn.execute("PRAGMA index_list(history_conflicts)")
               if i["unique"] and i["origin"] == "u"]
    assert uniques == [["source", "interval", "symbol", "bar_time", "history_epoch",
                        "existing_digest", "incoming_digest"]]
    assert "history_conflicts" in TABLE_NAMES
    assert "history_import_conflicts" not in TABLE_NAMES


def test_check_constraints_reject_bad_enums(tmp_path):
    conn = _conn(tmp_path)
    base = ("INSERT INTO history_conflicts (source, interval, symbol, bar_time, "
            "history_epoch, existing_json, incoming_json, existing_digest, "
            "incoming_digest, origin, last_origin, state, first_seen_at, last_seen_at) "
            "VALUES ('mt5','1m','X','t','none','{}','{}','a','b',?,?,?,'s','s')")
    conn.execute(base, ("import", "sync", "open"))
    for args in (("bogus", "import", "open"), ("import", "bogus", "open"),
                 ("import", "import", "bogus")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base.replace("'a','b'", "'c','d'"), args)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE history_conflicts SET seen_count=0")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE history_conflicts SET resolution_action='free text'")


def test_old_db_without_table_gains_it_and_second_init_is_noop(tmp_path):
    path = tmp_path / "old.db"
    conn = connect(path)
    init_db(conn)
    _seed(conn, [_row(0)])
    conn.execute("DROP TABLE history_conflicts")
    conn.commit()
    conn.close()

    conn = connect(path)
    init_db(conn)
    assert _count(conn, "history_conflicts") == 0
    assert _count(conn, "ohlcv_history") == 1
    cls = _cls(conn, [_row(0, close=148.15)])
    _apply(conn, cls)
    before_sql = conn.execute(
        "SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    before_rows = _records(conn)
    before_epoch = conn.execute("SELECT COUNT(*) FROM data_migrations").fetchone()[0]
    init_db(conn)
    assert conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == before_sql
    assert _records(conn) == before_rows
    assert conn.execute("SELECT COUNT(*) FROM data_migrations").fetchone()[0] == before_epoch
    assert not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name LIKE '%schema_version%'").fetchone()


# ---- digest / 二段判定 -----------------------------------------------------

def test_canonical_values_normalizes_numbers():
    a = hc.canonical_values((10, 11, 9, 10, 5, None))
    b = hc.canonical_values((10.0, 11.0, 9.0, 10.0, 5.0, None))
    assert a == b
    assert hc.canonical_values((-0.0, 1.0, -1.0, 0.0, 0.0, None))[0].startswith('{"open":0.0,')
    assert hc.canonical_values((1, 1, 1, 1, 1, None))[0].endswith('"spread":null}')
    assert hc.canonical_values((1, 1, 1, 1, 1, None))[1] != hc.canonical_values((1, 1, 1, 1, 1, 0.0))[1]
    with pytest.raises(ValueError):
        hc.canonical_values((float("nan"), 1, 1, 1, 1, None))
    with pytest.raises(ValueError):
        hc.canonical_values((1, float("inf"), 1, 1, 1, None))


def test_tolerance_boundary_decides_unchanged_vs_conflict(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(0, volume=0.0), _row(1, volume=0.0)])
    inside = _row(0, volume=5e-10)
    exact = _row(1, volume=1e-9)
    cls = _cls(conn, [inside, exact])
    kinds = {e.bar_time: e.kind for e in cls.entries}
    assert kinds == {_t(0): "unchanged", _t(1): "conflict"}


def test_micro_difference_alone_is_unchanged_without_record(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(0)])
    cls = _cls(conn, [_row(0, close=148.1 + 1e-10)])
    assert cls.unchanged == 1 and cls.conflicts_new == 0
    _apply(conn, cls)
    assert _count(conn, "history_conflicts") == 0


def test_micro_difference_on_other_component_makes_separate_records(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(0)])
    _apply(conn, _cls(conn, [_row(0, close=148.15)]))
    _apply(conn, _cls(conn, [_row(0, close=148.15, volume=100.0 + 1e-10)]))
    recs = _records(conn)
    assert len(recs) == 2
    assert recs[0]["incoming_digest"] != recs[1]["incoming_digest"]
    assert {r["existing_digest"] for r in recs} == {recs[0]["existing_digest"]}


def test_digest_equal_values_merge_into_one_record(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(0)])
    incoming = ("USDJPY", "1m", _t(0), 148, 148.2, 147.9, 148.15, 100, 0.012)
    incoming_float = ("USDJPY", "1m", _t(0), 148.0, 148.2, 147.9, 148.15, 100.0, 0.012)
    _apply(conn, _cls(conn, [incoming]))
    _apply(conn, _cls(conn, [incoming_float]))
    recs = _records(conn)
    assert len(recs) == 1 and recs[0]["seen_count"] == 2


# ---- 入力内の重複 ---------------------------------------------------------

def test_duplicate_identical_keys_are_counted_once(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(0)])
    bad = _row(0, close=148.15)
    cls = _cls(conn, [bad, bad, _row(1)])
    assert cls.conflicts_new == 1 and cls.new_rows == 1


def test_duplicate_keys_with_different_values_are_rejected_without_writes(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ohlcv.InconsistentInputError):
        _cls(conn, [_row(0), _row(0, close=148.05)])
    assert _count(conn, "ohlcv_history") == 0 and _count(conn, "history_conflicts") == 0


# ---- upsert の冪等性と closed state ---------------------------------------

def test_reobservation_updates_only_last_observation(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(0)])
    bad = _row(0, close=148.15)
    _apply(conn, _cls(conn, [bad]), origin="import", seen_at=SEEN, run_id=None)
    first = _records(conn)[0]
    assert first["seen_count"] == 1 and first["state"] == "open"
    _apply(conn, _cls(conn, [bad]), origin="sync", seen_at=SEEN2, run_id="run-2")
    recs = _records(conn)
    assert len(recs) == 1
    r = recs[0]
    assert r["id"] == first["id"] and r["seen_count"] == 2
    assert r["origin"] == "import" and r["last_origin"] == "sync"
    assert r["first_seen_at"] == first["first_seen_at"] != r["last_seen_at"]
    assert r["first_run_id"] is None and r["last_run_id"] == "run-2"
    assert (r["existing_json"], r["incoming_json"], r["state"]) == (
        first["existing_json"], first["incoming_json"], "open")
    assert _count(conn, "ohlcv_history") == 1


def test_last_seen_at_never_moves_backwards(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(0)])
    bad = _row(0, close=148.15)
    _apply(conn, _cls(conn, [bad]), seen_at=SEEN2)
    _apply(conn, _cls(conn, [bad]), seen_at=SEEN)
    assert _records(conn)[0]["last_seen_at"] == SEEN2.isoformat()


def test_runless_reobservation_clears_last_run_id(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(0)])
    bad = _row(0, close=148.15)
    _apply(conn, _cls(conn, [bad]), run_id="r1")
    _apply(conn, _cls(conn, [bad]), run_id=None)
    r = _records(conn)[0]
    assert r["first_run_id"] == "r1" and r["last_run_id"] is None


def test_kept_is_never_reopened_and_known(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(0)])
    bad = _row(0, close=148.15)
    _apply(conn, _cls(conn, [bad]))
    conn.execute("UPDATE history_conflicts SET state='kept', resolved_by='human'")
    conn.commit()
    cls = _cls(conn, [bad])
    assert (cls.conflicts_known, cls.conflicts_new) == (1, 0)
    _apply(conn, cls, seen_at=SEEN2)
    r = _records(conn)[0]
    assert r["state"] == "kept" and r["seen_count"] == 2 and r["resolved_by"] == "human"


@pytest.mark.parametrize("state", ["replaced", "superseded"])
def test_closed_states_are_new_for_circuit_but_never_reopened(tmp_path, state):
    conn = _conn(tmp_path)
    _seed(conn, [_row(0)])
    bad = _row(0, close=148.15)
    _apply(conn, _cls(conn, [bad]))
    conn.execute("UPDATE history_conflicts SET state=?", (state,))
    conn.commit()
    cls = _cls(conn, [bad])
    assert (cls.conflicts_known, cls.conflicts_new) == (0, 1)
    _apply(conn, cls, seen_at=SEEN2)
    recs = _records(conn)
    assert len(recs) == 1 and recs[0]["state"] == state and recs[0]["seen_count"] == 2


def test_known_requires_exact_epoch_and_both_digests(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(0)])
    bad = _row(0, close=148.15)
    _apply(conn, _cls(conn, [bad]))
    assert _cls(conn, [bad]).conflicts_known == 1
    # incoming digest が違う
    assert _cls(conn, [_row(0, close=148.16)]).conflicts_new == 1
    # existing digest が違う (既存行が変わった)
    conn.execute("UPDATE ohlcv_history SET volume=101.0")
    conn.commit()
    assert _cls(conn, [bad]).conflicts_new == 1
    conn.execute("UPDATE ohlcv_history SET volume=100.0")
    # epoch が違う
    conn.execute("INSERT INTO data_migrations (name, applied_at) VALUES "
                 "('time_repair_x', '2026-11-14T00:00:00+00:00')")
    conn.commit()
    cls = _cls(conn, [bad])
    assert (cls.conflicts_known, cls.conflicts_new) == (0, 1)
    _apply(conn, cls)
    assert len(_records(conn)) == 2


# ---- 事前遮断器 -----------------------------------------------------------

@pytest.mark.parametrize("c_new,n_test,expected", [
    (0, 0, False), (1, 0, False), (1, 1, False), (1, 100000, False),
    (2, 0, True), (2, 199, True), (2, 200, False), (2, 201, False),
    (3, 299, True), (3, 300, False),
    (20, 2000, False), (20, 0, True), (21, 100000, True), (21, 21, True),
])
def test_circuit_boundaries(c_new, n_test, expected):
    assert ohlcv.conflict_circuit_open(c_new, n_test) is expected


def _series(conn, n_unchanged, n_bad, start=0):
    good = [_row(start + i) for i in range(n_unchanged)]
    bad_base = [_row(start + n_unchanged + i) for i in range(n_bad)]
    _seed(conn, good + bad_base)
    return good + [_row(start + n_unchanged + i, close=148.15) for i in range(n_bad)]


def test_real_rows_exactly_one_percent_passes(tmp_path):
    conn = _conn(tmp_path)
    cls = _cls(conn, _series(conn, 198, 2))
    assert (cls.unchanged, cls.conflicts_new, cls.n_test) == (198, 2, 200)
    assert not cls.circuit_open
    assert _apply(conn, cls).status == "ok"
    assert _count(conn, "history_conflicts") == 2


def test_real_rows_just_over_one_percent_is_blocked_with_zero_writes(tmp_path):
    conn = _conn(tmp_path)
    rows = _series(conn, 197, 2) + [_row(500), _row(501)]
    cls = _cls(conn, rows)
    assert cls.n_test == 199 and cls.circuit_open and cls.new_rows == 2
    with pytest.raises(ValueError):
        _apply(conn, cls)
    assert _count(conn, "history_conflicts") == 0
    assert _count(conn, "ohlcv_history") == 199


def test_real_rows_twenty_pass_and_twenty_one_blocked(tmp_path):
    conn = _conn(tmp_path)
    assert not _cls(conn, _series(conn, 1980, 20)).circuit_open
    conn2 = connect(tmp_path / "t2.db")
    init_db(conn2)
    assert _cls(conn2, _series(conn2, 1979, 21)).circuit_open


def test_single_new_conflict_is_never_ratio_tested(tmp_path):
    conn = _conn(tmp_path)
    cls = _cls(conn, _series(conn, 0, 1))
    assert cls.conflicts_new == 1 and cls.n_test == 1 and not cls.circuit_open


def test_known_conflicts_do_not_dilute_the_denominator(tmp_path):
    conn = _conn(tmp_path)
    rows = _series(conn, 0, 502)
    known, fresh = rows[:500], rows[500:]
    _record_known(conn, known)
    cls = _cls(conn, known + fresh)
    assert (cls.conflicts_known, cls.conflicts_new, cls.unchanged) == (500, 2, 0)
    assert cls.n_overlap == 502 and cls.n_test == 2
    assert cls.circuit_open


def test_many_known_conflicts_alone_never_trip_the_count_limit(tmp_path):
    conn = _conn(tmp_path)
    rows = _series(conn, 0, 30)
    _record_known(conn, rows, state="open")
    cls = _cls(conn, rows)
    assert cls.conflicts_known == 30 and not cls.circuit_open
    res = _apply(conn, cls, seen_at=SEEN2)
    assert (res.status, res.conflicted_known, res.conflicted_new) == ("ok", 30, 0)
    assert {r["seen_count"] for r in _records(conn)} == {2}


def test_inspection_helpers():
    assert [ohlcv.inspection_tail_bars(s) for s in (60, 300, 900)] == [120, 24, 8]
    assert ohlcv.inspection_sufficient(8, 2, 8)
    assert not ohlcv.inspection_sufficient(7, 5, 8)
    assert not ohlcv.inspection_sufficient(8, 1, 8)


# ---- batch 原子性 ---------------------------------------------------------

def test_batches_split_by_row_count_and_commit_history_with_records(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(1)])
    rows = [_row(0), _row(1, close=148.15), _row(2), _row(3), _row(4)]
    progress = []
    res = _apply(conn, _cls(conn, rows), max_rows=2,
                 on_batch=lambda c, p: progress.append(p))
    assert [p.rows_in_batch for p in progress] == [2, 2, 1]
    assert [p.resume_from for p in progress] == [_t(2), _t(4), None]
    assert (res.status, res.inserted, res.conflicted_new) == ("ok", 4, 1)
    assert res.last_committed_bar_time == _t(4) and res.resume_from is None
    assert _count(conn, "ohlcv_history") == 5 and _count(conn, "history_conflicts") == 1


def test_batches_split_by_time_budget(tmp_path):
    conn = _conn(tmp_path)
    ticks = iter(range(0, 10_000))
    progress = []
    _apply(conn, _cls(conn, [_row(i) for i in range(6)]), budget_seconds=1.5,
           clock=lambda: next(ticks) * 1.0, on_batch=lambda c, p: progress.append(p.rows_in_batch))
    assert len(progress) > 1 and sum(progress) == 6


def test_mid_run_failure_rolls_back_only_the_failing_batch_and_rerun_completes(tmp_path):
    conn = _conn(tmp_path)
    steady = [_row(10 + i) for i in range(200)]
    _seed(conn, [_row(0), _row(3), *steady])
    rows = [_row(0, close=148.15), _row(1), _row(2), _row(3, close=148.16), _row(4), *steady]
    calls = []

    def boom(c, p):
        calls.append(p)
        if len(calls) == 2:
            raise RuntimeError("secret-ish detail 148.15")

    res = _apply(conn, _cls(conn, rows), max_rows=2, on_batch=boom)
    assert res.status == "partial" and res.failure == "write_failed"
    assert res.error_type == "RuntimeError" and "148" not in repr(res)
    assert res.last_committed_bar_time == _t(1) and res.resume_from == _t(2)
    assert (res.inserted, res.conflicted_new) == (1, 1)
    # 2 batch 目の挿入 (bar 2) と衝突 (bar 3) は全て rollback
    assert _count(conn, "ohlcv_history") == 203
    assert _count(conn, "history_conflicts") == 1
    assert not conn.in_transaction

    res2 = _apply(conn, _cls(conn, rows), max_rows=2)
    assert res2.status == "ok" and res2.inserted == 2
    assert _count(conn, "ohlcv_history") == 205
    seen = {r["bar_time"]: r["seen_count"] for r in _records(conn)}
    assert seen == {_t(0): 2, _t(3): 1}


def test_first_batch_failure_is_failed_with_nothing_written(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(0)])

    def boom(c, p):
        raise RuntimeError("x")

    res = _apply(conn, _cls(conn, [_row(0, close=148.15), _row(1)]), on_batch=boom)
    assert res.status == "failed" and res.last_committed_bar_time is None
    assert res.resume_from == _t(0)
    assert _count(conn, "history_conflicts") == 0 and _count(conn, "ohlcv_history") == 1


def test_progress_hook_commits_with_the_batch(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("CREATE TABLE prog (at TEXT)")
    conn.commit()

    def hook(c, p):
        c.execute("INSERT INTO prog VALUES (?)", (p.batch_bar_time_end,))

    _apply(conn, _cls(conn, [_row(i) for i in range(3)]), max_rows=2, on_batch=hook)
    assert [r[0] for r in conn.execute("SELECT at FROM prog ORDER BY at")] == [_t(1), _t(2)]


def test_busy_database_is_reported_as_database_busy_without_waiting(tmp_path):
    path = tmp_path / "t.db"
    conn = connect(path)
    init_db(conn)
    conn.execute("PRAGMA busy_timeout=0")
    holder = connect(path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        res = _apply(conn, _cls(conn, [_row(0)]))
    finally:
        holder.rollback()
    assert (res.status, res.failure) == ("failed", "database_busy")
    assert _count(conn, "ohlcv_history") == 0


def test_epoch_change_between_classification_and_apply_writes_nothing(tmp_path):
    conn = _conn(tmp_path)
    cls = _cls(conn, [_row(0)])
    conn.execute("INSERT INTO data_migrations (name, applied_at) VALUES "
                 "('time_repair_y', '2026-11-14T00:00:00+00:00')")
    conn.commit()
    res = _apply(conn, cls)
    assert (res.status, res.failure) == ("failed", "epoch_changed")
    assert _count(conn, "ohlcv_history") == 0


def test_externally_inserted_row_aborts_the_batch(tmp_path):
    conn = _conn(tmp_path)
    cls = _cls(conn, [_row(0), _row(1)])
    _seed(conn, [_row(1)])
    res = _apply(conn, cls)
    assert (res.status, res.failure) == ("failed", "concurrent_change")
    assert _count(conn, "ohlcv_history") == 1


def test_apply_rejects_open_transaction_and_bad_origin(tmp_path):
    conn = _conn(tmp_path)
    cls = _cls(conn, [_row(0)])
    with pytest.raises(ValueError):
        _apply(conn, cls, origin="bogus")
    conn.execute("BEGIN")
    with pytest.raises(ValueError):
        _apply(conn, cls)
    conn.rollback()


def test_import_sources_only(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        _cls(conn, [_row(0)], source="yfinance")


# ---- 系列の混在 / unchanged の並行変更 ----------------------------------------


def test_mixed_series_input_is_rejected_before_classification(tmp_path):
    conn = _conn(tmp_path)
    rows = [_row(i) for i in range(200)]
    _seed(conn, rows)
    bad_b = [_row(i, symbol="EURUSD", close=1.1, opn=1.0) for i in range(2)]
    _seed(conn, [(r[0], r[1], r[2], 1.0, 1.2, 0.9, 1.05, 10.0, 0.01) for r in bad_b])
    incoming = [_row(i) for i in range(200)] + [
        (r[0], r[1], r[2], 1.0, 1.2, 0.9, 1.07, 10.0, 0.01) for r in bad_b]
    with pytest.raises(ohlcv.InconsistentInputError):
        _cls(conn, incoming)
    # 系列ごとに分ければ B は 2/2 で遮断される
    only_b = _cls(conn, incoming[200:])
    assert only_b.circuit_open
    mixed_interval = [_row(0), ("USDJPY", "5m", _t(0), 148.0, 148.2, 147.9, 148.1, 1.0, None)]
    with pytest.raises(ohlcv.InconsistentInputError):
        _cls(conn, mixed_interval)


def _unchanged_apply_setup(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, [_row(0), _row(1), _row(2)])
    cls = _cls(conn, [_row(0), _row(1), _row(2), _row(3)])
    return conn, cls


def test_unchanged_row_deleted_before_apply_rolls_back_the_batch(tmp_path):
    conn, cls = _unchanged_apply_setup(tmp_path)
    conn.execute("DELETE FROM ohlcv_history WHERE bar_time=?", (_t(1),))
    conn.commit()
    seen = []
    res = _apply(conn, cls, on_batch=lambda c, p: seen.append(p))
    assert (res.status, res.failure) == ("failed", "concurrent_change")
    assert seen == [] or res.last_committed_bar_time is None
    assert _count(conn, "ohlcv_history") == 2


def test_unchanged_row_modified_before_apply_rolls_back_the_batch(tmp_path):
    conn, cls = _unchanged_apply_setup(tmp_path)
    conn.execute("UPDATE ohlcv_history SET close=149.5 WHERE bar_time=?", (_t(1),))
    conn.commit()
    res = _apply(conn, cls)
    assert (res.status, res.failure) == ("failed", "concurrent_change")
    assert _count(conn, "ohlcv_history") == 3        # bar 3 は書かれていない
    assert _count(conn, "history_conflicts") == 0


def test_before_batch_can_stop_with_a_failure_code(tmp_path):
    conn, cls = _unchanged_apply_setup(tmp_path)
    calls = iter([None, "stage_expired"])
    res = _apply(conn, cls, max_rows=2, before_batch=lambda: next(calls))
    assert (res.status, res.failure) == ("partial", "stage_expired")
    assert res.resume_from is not None


def test_before_commit_failure_rolls_back_that_batch_only(tmp_path):
    conn, cls = _unchanged_apply_setup(tmp_path)
    before = _count(conn, "ohlcv_history")
    calls = iter([None, "stage_expired"])
    res = _apply(conn, cls, max_rows=2, before_commit=lambda: next(calls))
    assert (res.status, res.failure) == ("partial", "stage_expired")
    assert res.resume_from is not None
    assert _count(conn, "ohlcv_history") == before + res.inserted


# ---- agent への非露出 ------------------------------------------------------
# 公開境界 (agent の tool・prompt・改善記録の投影) が衝突の内部に触れないこと。
# store 内の呼出し (import・CLI) は妨げない。

_INTERNALS = re.compile(
    r"history_conflicts|conflict_circuit|ClassifiedRow|classify_history_rows|"
    r"apply_classified_history|existing_json|incoming_json|existing_digest|"
    r"incoming_digest|reason_ref")


def test_agent_facing_boundaries_never_reference_conflict_internals():
    root = Path(ohlcv.__file__).resolve().parents[1]
    offenders = []
    for pkg in ("tools", "runners", "loops", "plugin"):
        for path in (root / pkg).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".md", ".txt", ".j2", ".yaml"}:
                if _INTERNALS.search(path.read_text(errors="ignore")):
                    offenders.append(path.relative_to(root).as_posix())
    assert offenders == []


def test_conflict_canaries_do_not_reach_the_improve_prompt_or_context(tmp_path):
    from agentic_fx.loops.improve_context import build_improve_context
    from agentic_fx.loops.improve_loop import ImproveLoop
    from agentic_fx.config import load_settings
    import json

    canary_value = 7654321.125
    canary_reason = "CANARY-REASON-5d1f"
    conn = _conn(tmp_path)
    _seed(conn, [_row(0, volume=canary_value)])
    conn.commit()
    res = _apply(conn, _cls(conn, [_row(0, volume=canary_value + 5, close=148.15)]))
    assert res.conflicted_new == 1
    conn.execute("UPDATE history_conflicts SET reason_ref=?", (canary_reason,))
    conn.commit()
    digest = conn.execute("SELECT existing_digest FROM history_conflicts").fetchone()[0]
    assert digest
    settings = load_settings(Path(__file__).resolve().parents[2]
                             / "config" / "settings.yaml.example")
    root = tmp_path / "root"
    root.mkdir()
    ctx_data = build_improve_context(
        conn, settings=settings, now=datetime(2026, 11, 14, 12, 0), root=root,
        allowed_backlog_ids=None,
        inventory_view={"plugins": [], "pin_broken_strategies": []})

    class _Ctx:
        staging_dir = tmp_path / "staging"
        source_snapshot_dir = tmp_path / "source"
        inventory_view = {"plugins": [], "pin_broken_strategies": []}
        baseline_gate_mode = "off"

    loop = ImproveLoop.__new__(ImproveLoop)
    loop._settings = settings
    rendered = loop._render_improve_mission_prompt(ctx_data, ctx=_Ctx())
    blob = rendered + json.dumps(ctx_data, ensure_ascii=False, default=str)
    for canary in (str(canary_value), str(canary_value + 5), canary_reason, digest):
        assert canary not in blob


def test_production_code_never_calls_the_low_level_import_history_bars():
    root = Path(__file__).resolve().parents[2] / "src"
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(errors="ignore")
        for m in re.finditer(r"\bimport_history_bars\s*\(", text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            line = text[line_start:text.find("\n", m.start())]
            if line.lstrip().startswith(("def ", "#")) or "import_history_bars:" in line:
                continue
            offenders.append(f"{path.relative_to(root).as_posix()}: {line.strip()}")
    assert offenders == []


def test_conflict_canaries_do_not_reach_agent_tool_responses(tmp_path):
    import json
    from unittest.mock import MagicMock, patch

    from agentic_fx.config import load_settings
    from agentic_fx.config import ResearchSettings
    from agentic_fx.core.contracts import Bar, FixedClock
    from agentic_fx.datafeed.price_provider import PriceProvider
    from agentic_fx.tools import market_tools
    from agentic_fx.tools.research_tools import build_research_tooldefs

    canary_value = 7654321.125
    canary_reason = "CANARY-REASON-5d1f"
    conn = _conn(tmp_path)
    _seed(conn, [_row(0, volume=canary_value)])
    conn.commit()
    res = _apply(conn, _cls(conn, [_row(0, volume=canary_value + 5, close=148.15)]))
    assert res.conflicted_new == 1
    conn.execute("UPDATE history_conflicts SET reason_ref=?", (canary_reason,))
    conn.commit()
    digest = conn.execute("SELECT existing_digest FROM history_conflicts").fetchone()[0]
    canaries = (str(canary_value), str(canary_value + 5), canary_reason, digest)

    settings = load_settings(Path(__file__).resolve().parents[2]
                             / "config" / "settings.yaml.example")
    settings.datafeed.primary = "mt5"
    settings.datafeed.mt5.enabled = True
    settings.datafeed.yfinance.enabled = False
    now = datetime(2026, 1, 2, 0, 5, tzinfo=timezone.utc)
    last = now.replace(minute=0) - timedelta(hours=1)
    bars = [Bar("USDJPY", "1h", last - timedelta(hours=199 - i), 1.0, 2.0, 0.5, 1.5, 1)
            for i in range(200)]
    provider = PriceProvider(conn, settings, FixedClock(now), readonly=True)
    tools = {t.name: t for t in market_tools.build(
        provider, MagicMock(), settings, sandbox_run=lambda *a, **k: {})}
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_bars", return_value=bars):
        responses = [tools["get_ohlcv"].func("USDJPY", "1h"),
                     tools["get_indicators"].func("USDJPY", "1h")]
    assert responses[0] and "sma_20" in responses[1]

    class _Search:
        def text(self, query, max_results):
            return [{"title": "t", "href": "https://example.com/a", "body": "b"}]

    class _Fetch:
        def fetch(self, url, *, user_agent=None):
            return 200, "article body"

    research = {t.name: t for t in build_research_tooldefs(
        settings=ResearchSettings(max_searches=20, max_fetches=30, min_interval_sec=0.01,
                                  max_per_host=5, fetch_max_bytes=2_097_152,
                                  user_agent="agentic-fx/test"),
        search_backend=_Search(), fetch_backend=_Fetch(), monotonic=iter(
            float(i) for i in range(100000)).__next__)}
    responses.append(research["web_search"].func(query="q", max_results=3))
    responses.append(research["fetch_article"].func(url="https://example.com/a"))
    blob = json.dumps(responses, ensure_ascii=False, default=str)
    for canary in canaries:
        assert canary not in blob
