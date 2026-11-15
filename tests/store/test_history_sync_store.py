"""同期の表・epoch・queue の状態遷移・保持 GC (実 sqlite)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.history_epoch import history_epoch
from agentic_fx.store import history_sync as hs
from agentic_fx.store.db import TABLE_NAMES, connect, init_db

NEW_TABLES = ("history_sync_runs", "history_sync_series", "history_sync_series_state",
              "history_sync_gaps", "history_sync_resume", "history_revisions")
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
SRC, IV = "mt5", "15m"


def _conn(tmp_path, name="t.db"):
    conn = connect(tmp_path / name)
    init_db(conn)
    return conn


def _txn(conn, fn):
    return hs.write_txn(conn, fn, sleep=lambda _s: None)


# ---- 表の導入 -------------------------------------------------------------

def test_new_tables_are_in_table_names_and_created_fresh(tmp_path):
    conn = _conn(tmp_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in NEW_TABLES:
        assert table in TABLE_NAMES and table in names
    assert {n for n in names if not n.startswith("sqlite_")} == set(TABLE_NAMES)


def test_init_db_twice_is_a_no_op_for_the_new_tables(tmp_path):
    conn = _conn(tmp_path)
    _txn(conn, lambda c: hs.insert_run(
        c, run_id="r1", owner="o", trigger="manual", source=SRC, interval=IV,
        calendar_version="v", lookback_days=7, epoch="none", t=NOW, b=NOW, e=NOW,
        started_at=NOW))
    before = [tuple(r) for r in conn.execute("SELECT * FROM history_sync_runs")]
    schema_before = sorted(tuple(r) for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE name LIKE 'history_%'"))
    init_db(conn)
    assert [tuple(r) for r in conn.execute("SELECT * FROM history_sync_runs")] == before
    assert sorted(tuple(r) for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE name LIKE 'history_%'")) == schema_before
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0   # DB の schema version は新設しない


def test_an_old_database_without_the_tables_gains_them_and_keeps_its_rows(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO ohlcv_history (symbol, interval, bar_time, open, high, low, close, "
                 "volume, source) VALUES ('USDJPY','15m','2026-11-02T00:00:00+00:00',1,2,0.5,1.5,0,'mt5')")
    conn.commit()
    for table in NEW_TABLES:
        conn.execute(f"DROP TABLE {table}")
    conn.commit()
    init_db(conn)
    for table in NEW_TABLES:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name=?",
                            (table,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 1


def test_run_series_rows_have_no_foreign_keys(tmp_path):
    conn = _conn(tmp_path)
    for table in NEW_TABLES:
        assert conn.execute(f"PRAGMA foreign_key_list({table})").fetchall() == []


def test_check_constraints_reject_unknown_words(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO history_sync_gaps (source, interval, symbol, day_utc, kind, "
                     "first_seen_at, last_observed_at) VALUES ('mt5','15m','X','2026-11-01',"
                     "'bogus','a','a')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO history_revisions (kind, source, interval, symbol, bar_time, "
                     "history_epoch, conflict_id, actor, created_at) VALUES "
                     "('other','mt5','15m','X','t','none',1,'a','b')")


# ---- epoch ----------------------------------------------------------------

def test_epoch_is_byte_identical_to_the_legacy_value_while_no_revision_exists(tmp_path):
    conn = _conn(tmp_path)
    assert history_epoch(conn) == "none"
    conn.execute("INSERT INTO data_migrations (name, applied_at) VALUES "
                 "('time_repair_2026_11_02', '2026-11-02T00:00:00+00:00')")
    conn.commit()
    assert history_epoch(conn) == "time_repair_2026_11_02"


def test_epoch_gains_a_versioned_token_only_after_a_revision(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO history_revisions (kind, source, interval, symbol, bar_time, "
                 "history_epoch, conflict_id, actor, created_at) VALUES "
                 "('replace_with_incoming','mt5','15m','X','t','none',1,'a','b')")
    conn.commit()
    first = history_epoch(conn)
    assert first == "v2;time_repair=none;revision=1"
    conn.execute("INSERT INTO history_revisions (kind, source, interval, symbol, bar_time, "
                 "history_epoch, conflict_id, actor, created_at) VALUES "
                 "('replace_with_incoming','mt5','15m','X','t','none',2,'a','b')")
    conn.commit()
    assert history_epoch(conn) != first


def test_epoch_does_not_raise_on_a_database_without_the_revisions_table(tmp_path):
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.execute("CREATE TABLE data_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    assert history_epoch(conn) == "none"
    conn.execute("INSERT INTO data_migrations VALUES ('time_repair_x', '2026-01-01')")
    assert history_epoch(conn) == "time_repair_x"


# ---- write_txn ------------------------------------------------------------

def test_write_txn_rolls_back_on_error_and_retries_busy_outside_the_transaction(tmp_path):
    conn = _conn(tmp_path)
    other = connect(tmp_path / "t.db")
    other.execute("PRAGMA busy_timeout=0")
    conn.execute("PRAGMA busy_timeout=0")
    other.execute("BEGIN IMMEDIATE")
    sleeps = []
    with pytest.raises(sqlite3.OperationalError):
        hs.write_txn(conn, lambda c: None, sleep=sleeps.append)
    assert sleeps[:4] == [0.010, 0.020, 0.040, 0.050] and set(sleeps[3:]) == {0.050}
    assert sum(sleeps) >= hs.BUSY_BUDGET_SEC - 1e-9
    other.rollback()

    def boom(c):
        c.execute("INSERT INTO history_sync_state_missing VALUES (1)")
    with pytest.raises(sqlite3.OperationalError):
        _txn(conn, boom)
    assert not conn.in_transaction


# ---- 系列状態・backoff ----------------------------------------------------

def test_failure_backoff_steps_and_a_manual_failure_never_shortens_a_wait(tmp_path):
    conn = _conn(tmp_path)
    waits = []
    for i in range(5):
        _txn(conn, lambda c: hs.record_series_failure(
            c, SRC, IV, "USDJPY", error_code="bridge_unavailable", run_id="r", now=NOW))
        row = hs.get_state(conn, SRC, IV, "USDJPY")
        waits.append(datetime.fromisoformat(row["next_retry_at"]) - NOW)
        assert row["attempts"] == i + 1
    assert waits == [timedelta(minutes=m) for m in (5, 15, 30, 60, 60)]
    # 既に長い待ち (daemon が付けた 7 日) があれば、手動の失敗で短くしない
    far = (NOW + timedelta(days=7)).isoformat()
    conn.execute("UPDATE history_sync_series_state SET next_retry_at=?", (far,))
    conn.commit()
    _txn(conn, lambda c: hs.record_series_failure(
        c, SRC, IV, "USDJPY", error_code="x", run_id="r", now=NOW))
    assert hs.get_state(conn, SRC, IV, "USDJPY")["next_retry_at"] == far


def test_a_success_clears_attempts_and_the_wait_and_the_cursor_never_moves_back(tmp_path):
    conn = _conn(tmp_path)
    _txn(conn, lambda c: hs.record_series_failure(
        c, SRC, IV, "USDJPY", error_code="x", run_id="r", now=NOW))
    later = NOW + timedelta(hours=2)
    _txn(conn, lambda c: hs.advance_cursor(c, SRC, IV, "USDJPY", last_processed_e=later,
                                           run_id="r2", now=NOW))
    row = hs.get_state(conn, SRC, IV, "USDJPY")
    assert (row["attempts"], row["next_retry_at"], row["last_processed_e"]) == (
        0, None, later.isoformat())
    _txn(conn, lambda c: hs.advance_cursor(c, SRC, IV, "USDJPY", last_processed_e=NOW,
                                           run_id="r3", now=NOW))
    assert hs.get_state(conn, SRC, IV, "USDJPY")["last_processed_e"] == later.isoformat()


# ---- gap queue ------------------------------------------------------------

def _observe(conn, day, kind="primary_exact", symbol="USDJPY", missing=100):
    return _txn(conn, lambda c: hs.observe_gap(
        c, source=SRC, interval=IV, symbol=symbol, day_utc=day, kind=kind,
        missing_count=missing, run_id="r", now=NOW))


def _gap(conn, day, kind="primary_exact", symbol="USDJPY"):
    return conn.execute("SELECT * FROM history_sync_gaps WHERE symbol=? AND day_utc=? AND kind=?",
                        (symbol, day, kind)).fetchone()


def test_a_gap_key_is_one_row_per_day_and_kind_and_resolved_reopens_on_reobservation(tmp_path):
    conn = _conn(tmp_path)
    assert _observe(conn, "2026-10-20") == "new"
    assert _observe(conn, "2026-10-20") == "updated"
    assert _observe(conn, "2026-10-20", kind="unprocessed") == "new"
    assert conn.execute("SELECT COUNT(*) FROM history_sync_gaps").fetchone()[0] == 2
    _txn(conn, lambda c: hs.resolve_gap(
        c, source=SRC, interval=IV, symbol="USDJPY", day_utc="2026-10-20",
        kind="primary_exact", resolution="refetched", run_id="r", now=NOW))
    assert _gap(conn, "2026-10-20")["state"] == "resolved"
    assert _observe(conn, "2026-10-20") == "reopened"
    row = _gap(conn, "2026-10-20")
    assert (row["state"], row["attempts"], row["no_improve_count"]) == ("open", 0, 0)


def test_no_improvement_backs_off_24_hours_after_three_and_7_days_after_six(tmp_path):
    conn = _conn(tmp_path)
    _observe(conn, "2026-10-20")
    retries = []
    for _ in range(6):
        outcome = _txn(conn, lambda c: hs.record_gap_attempt(
            c, source=SRC, interval=IV, symbol="USDJPY", day_utc="2026-10-20",
            kind="primary_exact", missing_before=100, missing_after=100, run_id="r", now=NOW))
        assert outcome == "no_improvement"
        retries.append(_gap(conn, "2026-10-20")["next_retry_at"])
    assert retries[:2] == [None, None]
    assert datetime.fromisoformat(retries[2]) - NOW == timedelta(hours=24)
    assert datetime.fromisoformat(retries[4]) - NOW == timedelta(hours=24)
    assert datetime.fromisoformat(retries[5]) - NOW == timedelta(days=7)


def test_an_improvement_returns_to_the_normal_cycle_and_a_clean_refetch_resolves(tmp_path):
    conn = _conn(tmp_path)
    _observe(conn, "2026-10-20")
    for _ in range(3):
        _txn(conn, lambda c: hs.record_gap_attempt(
            c, source=SRC, interval=IV, symbol="USDJPY", day_utc="2026-10-20",
            kind="primary_exact", missing_before=100, missing_after=100, run_id="r", now=NOW))
    assert _gap(conn, "2026-10-20")["next_retry_at"] is not None
    out = _txn(conn, lambda c: hs.record_gap_attempt(
        c, source=SRC, interval=IV, symbol="USDJPY", day_utc="2026-10-20",
        kind="primary_exact", missing_before=100, missing_after=40, run_id="r", now=NOW))
    row = _gap(conn, "2026-10-20")
    assert (out, row["no_improve_count"], row["next_retry_at"], row["missing_count"]) == (
        "improved", 0, None, 40)
    out = _txn(conn, lambda c: hs.record_gap_attempt(
        c, source=SRC, interval=IV, symbol="USDJPY", day_utc="2026-10-20",
        kind="primary_exact", missing_before=40, missing_after=0, run_id="r", now=NOW))
    row = _gap(conn, "2026-10-20")
    assert (out, row["state"], row["attempts"]) == ("resolved", "resolved", 0)


def test_expiry_uses_the_day_end_and_costs_no_retry_and_an_expired_row_is_not_reobserved(tmp_path):
    conn = _conn(tmp_path)
    for day in ("2026-10-10", "2026-10-11", "2026-10-12"):
        _observe(conn, day)
    boundary = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    expired = _txn(conn, lambda c: hs.expire_gaps(c, source=SRC, interval=IV,
                                                  boundary=boundary, now=NOW))
    assert expired == 1                                   # 10-10 の終端 10-11 00:00 <= B だけ
    assert _gap(conn, "2026-10-10")["state"] == "expired"
    assert _gap(conn, "2026-10-10")["next_retry_at"] is None
    assert _gap(conn, "2026-10-11")["state"] == "open"    # B をまたぐ日は open
    assert _observe(conn, "2026-10-10") == "expired_kept"
    assert _gap(conn, "2026-10-10")["state"] == "expired"
    due = hs.due_gaps(conn, source=SRC, interval=IV, now=NOW)
    assert {r["day_utc"] for r in due} == {"2026-10-11", "2026-10-12"}


def test_reopen_expired_only_brings_back_days_after_the_new_boundary(tmp_path):
    conn = _conn(tmp_path)
    for day in ("2026-10-10", "2026-10-01"):
        _observe(conn, day)
    _txn(conn, lambda c: hs.expire_gaps(
        c, source=SRC, interval=IV, boundary=datetime(2026, 10, 20, tzinfo=timezone.utc),
        now=NOW))
    reopened = _txn(conn, lambda c: hs.reopen_expired(
        c, source=SRC, interval=IV, boundary=datetime(2026, 10, 5, tzinfo=timezone.utc),
        now=NOW))
    assert reopened == 1
    assert _gap(conn, "2026-10-10")["state"] == "open"
    assert _gap(conn, "2026-10-01")["state"] == "expired"


def test_due_gaps_skip_future_retries_and_round_robin_shares_the_budget(tmp_path):
    conn = _conn(tmp_path)
    for day in ("2026-10-20", "2026-10-21", "2026-10-22"):
        _observe(conn, day, symbol="USDJPY")
    _observe(conn, "2026-10-25", symbol="EURUSD")
    conn.execute("UPDATE history_sync_gaps SET next_retry_at=? WHERE day_utc='2026-10-21'",
                 ((NOW + timedelta(hours=1)).isoformat(),))
    conn.commit()
    rows = hs.due_gaps(conn, source=SRC, interval=IV, now=NOW)
    assert [r["day_utc"] for r in rows] == ["2026-10-20", "2026-10-22", "2026-10-25"]
    order = hs.order_round_robin(rows, ["USDJPY", "EURUSD"], None)
    assert [(r["symbol"], r["day_utc"]) for r in order] == [
        ("USDJPY", "2026-10-20"), ("EURUSD", "2026-10-25"), ("USDJPY", "2026-10-22")]
    order = hs.order_round_robin(rows, ["USDJPY", "EURUSD"], "USDJPY")
    assert order[0]["symbol"] == "EURUSD"
    _txn(conn, lambda c: hs.set_rr_cursor(c, SRC, IV, "EURUSD", now=NOW))
    assert hs.get_rr_cursor(conn, SRC, IV) == "EURUSD"
    assert hs.get_state(conn, SRC, IV, "USDJPY") is None    # 制御 key は系列に混ざらない


# ---- 保持 -----------------------------------------------------------------

def test_gc_drops_old_series_detail_but_keeps_run_identity_and_referenced_runs(tmp_path):
    conn = _conn(tmp_path)
    old = NOW - timedelta(days=120)
    recent = NOW - timedelta(days=10)

    def make(run_id, at, status):
        def work(c):
            hs.insert_run(c, run_id=run_id, owner="o", trigger="manual", source=SRC,
                          interval=IV, calendar_version="v", lookback_days=7, epoch="none",
                          t=at, b=at, e=at, started_at=at)
            hs.finish_run(c, run_id, status=status, rc=0, finished_at=at)
            hs.insert_series(c, run_id=run_id, op_seq=1, operation="recent", symbol="X",
                             range_kind="recent", range_start=at, range_end=at, day_utc=None,
                             started_at=at)
        _txn(conn, work)

    make("old_free", old, "ok")
    make("old_ref", old, "ok")
    make("new", recent, "ok")
    _txn(conn, lambda c: hs.insert_run(
        c, run_id="old_running", owner="o", trigger="manual", source=SRC, interval=IV,
        calendar_version="v", lookback_days=7, epoch="none", t=old, b=old, e=old,
        started_at=old))
    _observe(conn, "2026-10-20")
    conn.execute("UPDATE history_sync_gaps SET first_run_id='old_ref', last_run_id='old_ref'")
    conn.commit()
    result = _txn(conn, lambda c: hs.collect_garbage(c, now=NOW))
    runs = {r["id"]: r for r in conn.execute("SELECT * FROM history_sync_runs")}
    assert set(runs) == {"old_free", "old_ref", "new", "old_running"}      # run の行は消えない
    series = {r["run_id"] for r in conn.execute("SELECT run_id FROM history_sync_series")}
    assert series == {"old_ref", "new"}                   # 参照されない古い詳細だけ消える
    assert runs["old_free"]["tombstone"] == 1 and runs["new"]["tombstone"] == 0
    assert runs["old_running"]["tombstone"] == 0          # 未完 run は触らない
    assert result["series_deleted"] == 1


def test_gc_never_deletes_conflicts_and_keeps_a_conflict_referenced_run(tmp_path):
    conn = _conn(tmp_path)
    old = NOW - timedelta(days=200)

    def work(c):
        hs.insert_run(c, run_id="cr", owner="o", trigger="manual", source=SRC, interval=IV,
                      calendar_version="v", lookback_days=7, epoch="none", t=old, b=old,
                      e=old, started_at=old)
        hs.finish_run(c, "cr", status="ok", rc=0, finished_at=old)
        hs.insert_series(c, run_id="cr", op_seq=1, operation="recent", symbol="X",
                         range_kind="recent", range_start=old, range_end=old, day_utc=None,
                         started_at=old)
    _txn(conn, work)
    conn.execute(
        "INSERT INTO history_conflicts (source, interval, symbol, bar_time, history_epoch, "
        "existing_json, incoming_json, existing_digest, incoming_digest, origin, last_origin, "
        "state, first_seen_at, last_seen_at, first_run_id, last_run_id) VALUES "
        "('mt5','15m','X','t','none','{}','{}','a','b','sync','sync','kept','x','x','cr','cr')")
    conn.commit()
    _txn(conn, lambda c: hs.collect_garbage(c, now=NOW))
    assert conn.execute("SELECT COUNT(*) FROM history_conflicts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM history_sync_series WHERE run_id='cr'"
                        ).fetchone()[0] == 1


def test_interrupted_recovery_marks_only_runs_whose_owner_is_gone(tmp_path):
    conn = _conn(tmp_path)

    def work(c):
        for rid, owner in (("dead", "p1"), ("live", "p2")):
            hs.insert_run(c, run_id=rid, owner=owner, trigger="manual", source=SRC,
                          interval=IV, calendar_version="v", lookback_days=7, epoch="none",
                          t=NOW, b=NOW, e=NOW, started_at=NOW)
            hs.insert_series(c, run_id=rid, op_seq=1, operation="recent", symbol="X",
                             range_kind="recent", range_start=NOW, range_end=NOW,
                             day_utc=None, started_at=NOW)
    _txn(conn, work)
    changed = _txn(conn, lambda c: hs.mark_interrupted(
        c, owner_alive=lambda owner: owner == "p2", now=NOW))
    assert changed == 1
    status = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM history_sync_runs")}
    assert status == {"dead": "interrupted", "live": "running"}
    series = {r["run_id"]: r["status"] for r in conn.execute(
        "SELECT run_id, status FROM history_sync_series")}
    assert series == {"dead": "failed", "live": "running"}


def test_a_day_whose_end_equals_b_is_expired_and_one_ending_after_b_is_not(tmp_path):
    conn = _conn(tmp_path)
    _observe(conn, "2026-10-10")
    boundary = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc)          # 10-10 の終端ちょうど
    assert _txn(conn, lambda c: hs.expire_gaps(c, source=SRC, interval=IV,
                                               boundary=boundary, now=NOW)) == 1
    conn2 = _conn(tmp_path, "b.db")
    _observe(conn2, "2026-10-10")
    just_before = boundary - timedelta(minutes=15)
    assert _txn(conn2, lambda c: hs.expire_gaps(c, source=SRC, interval=IV,
                                                boundary=just_before, now=NOW)) == 0


# ---- 再開点 ---------------------------------------------------------------

def test_a_resume_point_is_saved_replaced_and_cleared_by_operation_key(tmp_path):
    conn = _conn(tmp_path)
    a, e0 = NOW - timedelta(days=2), NOW
    for prefix_end in (a + timedelta(hours=3), a + timedelta(hours=9)):
        _txn(conn, lambda c, p=prefix_end: hs.save_resume(
            c, source=SRC, interval=IV, symbol="USDJPY", op_key="recent", range_start=a,
            range_end=e0, tail_start=a - timedelta(hours=2), prefix_end=p, now=NOW))
    row = hs.get_resume(conn, SRC, IV, "USDJPY", "recent")
    assert row["prefix_end"] == (a + timedelta(hours=9)).isoformat()
    assert conn.execute("SELECT COUNT(*) FROM history_sync_resume").fetchone()[0] == 1
    assert hs.get_resume(conn, SRC, IV, "USDJPY", "gap:2026-11-04:primary_exact") is None
    _txn(conn, lambda c: hs.clear_resume(c, SRC, IV, "USDJPY", "recent"))
    assert hs.get_resume(conn, SRC, IV, "USDJPY", "recent") is None
