import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.store import cron_cursor, mission_decision_bars, missions
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 9, 24, 13, 0, 41, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 24, 12, 45, tzinfo=timezone.utc)
T0 = T1 - timedelta(minutes=15)


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def test_upsert_only_moves_forward_and_load_all_round_trips(tmp_path):
    conn = _conn(tmp_path)
    cron_cursor.upsert(conn, "USDJPY", "15m", T1, now=NOW)
    cron_cursor.upsert(conn, "USDJPY", "15m", T0, now=NOW)     # 後退は書かない
    cron_cursor.upsert(conn, "EURUSD", "15m", T0, now=NOW)
    assert cron_cursor.load_all(conn) == {
        ("USDJPY", "15m"): T1, ("EURUSD", "15m"): T0}


def test_upsert_failure_leaves_no_open_transaction(tmp_path):
    conn = _conn(tmp_path)
    conn.executescript(
        "CREATE TRIGGER fail_cc BEFORE INSERT ON cron_cursor "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END;")
    with pytest.raises(sqlite3.DatabaseError, match="injected"):
        cron_cursor.upsert(conn, "USDJPY", "15m", T1, now=NOW)
    assert not conn.in_transaction
    assert cron_cursor.load_all(conn) == {}


def test_init_db_adds_b2_tables_to_existing_db(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("DROP TABLE mission_decision_bars")
    conn.execute("DROP TABLE cron_cursor")
    conn.commit()
    init_db(conn)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"cron_cursor", "mission_decision_bars"} <= names


def test_mission_decision_bars_round_trip_and_cascade(tmp_path):
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW, trigger="cron")
    bars = {("USDJPY", "15m"): T1, ("EURUSD", "15m"): T0}
    mission_decision_bars.insert_many(conn, mid, bars)
    assert conn.in_transaction          # insert_many は commit しない
    conn.commit()
    assert mission_decision_bars.for_mission(conn, mid) == bars
    conn.execute("DELETE FROM missions WHERE id=?", (mid,))
    conn.commit()
    assert mission_decision_bars.for_mission(conn, mid) == {}


def test_upsert_refreshes_updated_at_on_conflict(tmp_path):
    conn = _conn(tmp_path)
    cron_cursor.upsert(conn, "USDJPY", "15m", T0, now=NOW)
    later = NOW + timedelta(minutes=15)
    cron_cursor.upsert(conn, "USDJPY", "15m", T1, now=later)
    row = conn.execute("SELECT bar_time, updated_at FROM cron_cursor").fetchone()
    assert (row["bar_time"], row["updated_at"]) == (
        T1.isoformat(), later.isoformat())


def test_upsert_rejects_naive_datetime(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="naive"):
        cron_cursor.upsert(conn, "USDJPY", "15m", T1.replace(tzinfo=None), now=NOW)
    with pytest.raises(ValueError, match="naive"):
        cron_cursor.upsert(conn, "USDJPY", "15m", T1, now=NOW.replace(tzinfo=None))
    assert cron_cursor.load_all(conn) == {}


def test_mission_decision_bars_reject_naive_datetime(tmp_path):
    """`cron_cursor` の `_iso()` と同じ規律: `insert_many` は naive
    datetime を `ValueError` で拒否し、`for_mission` も (何らかの経路で
    naive 文字列が保存されていた場合に備え) 読み出し時に同様に拒否する。"""
    conn = _conn(tmp_path)
    mid = missions.start(conn, "trade", "local", "m", NOW, trigger="cron")
    with pytest.raises(ValueError, match="naive"):
        mission_decision_bars.insert_many(
            conn, mid, {("USDJPY", "15m"): T1.replace(tzinfo=None)})
    conn.rollback()
    assert mission_decision_bars.for_mission(conn, mid) == {}

    conn.execute(
        "INSERT INTO mission_decision_bars (mission_id, pair, interval, "
        "bar_time) VALUES (?,?,?,?)",
        (mid, "USDJPY", "15m", T1.replace(tzinfo=None).isoformat()))
    conn.commit()
    with pytest.raises(ValueError, match="naive"):
        mission_decision_bars.for_mission(conn, mid)


def test_mission_decision_bars_are_scoped_to_mission_and_unique_per_pair(tmp_path):
    conn = _conn(tmp_path)
    first = missions.start(conn, "trade", "local", "m", NOW, trigger="cron")
    second = missions.start(conn, "trade", "local", "m", NOW, trigger="cron")
    mission_decision_bars.insert_many(conn, first, {("USDJPY", "15m"): T0})
    mission_decision_bars.insert_many(conn, second, {("EURUSD", "15m"): T1})
    conn.commit()
    assert mission_decision_bars.for_mission(conn, first) == {("USDJPY", "15m"): T0}
    assert mission_decision_bars.for_mission(conn, second) == {("EURUSD", "15m"): T1}
    with pytest.raises(sqlite3.IntegrityError):
        mission_decision_bars.insert_many(conn, first, {("USDJPY", "15m"): T1})
    conn.rollback()
