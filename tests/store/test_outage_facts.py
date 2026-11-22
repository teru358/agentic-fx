"""停止の facts (observe 済み水位・後着足・接触参照・健全の記録) の保存と、gap の表の移行。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from agentic_fx.core.contracts import Bar
from agentic_fx.store import outage_facts
from agentic_fx.store.db import TABLE_NAMES, connect, init_db
from agentic_fx.store.ohlcv import upsert_cache_bars

T0 = datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 18, 22, 5, tzinfo=timezone.utc)
SRC = "mt5-live"

_OLD_GAP_DDL = """
CREATE TABLE datafeed_outage_gap (
  pair TEXT NOT NULL,
  interval TEXT NOT NULL,
  epoch INTEGER NOT NULL,
  gap_start TEXT NOT NULL,
  replay_through TEXT,
  PRIMARY KEY (pair, interval, epoch)
)
"""


def _db(tmp_path, name="f.db"):
    conn = connect(tmp_path / name)
    init_db(conn)
    return conn


def _gap_shape(conn):
    return [(r["name"], r["type"].upper(), r["notnull"], r["dflt_value"], r["pk"])
            for r in conn.execute("PRAGMA table_info(datafeed_outage_gap)")]


def _old_gap_db(tmp_path):
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(str(path))
    legacy.execute(_OLD_GAP_DDL)
    legacy.execute("INSERT INTO datafeed_outage_gap VALUES "
                   "('USDJPY','1m',3,'2026-11-13T12:31:00+00:00',NULL)")
    legacy.execute("INSERT INTO datafeed_outage_gap VALUES "
                   "('USDJPY','1h',3,'2026-11-13T12:00:00+00:00','2026-11-13T13:00:00+00:00')")
    legacy.commit()
    legacy.close()
    return path


def test_new_tables_are_registered_and_exist_on_a_fresh_db(tmp_path):
    conn = _db(tmp_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("datafeed_outage_key_facts", "datafeed_late_bars", "datafeed_outage_contacts"):
        assert table in TABLE_NAMES and table in names


def test_fresh_gap_table_allows_an_explicit_null_gap_start_and_has_source(tmp_path):
    conn = _db(tmp_path)
    cols = {c[0]: c for c in _gap_shape(conn)}
    assert cols["gap_start"][2] == 0
    assert "source" in cols and cols["source"][2] == 0
    conn.execute("INSERT INTO datafeed_outage_gap (pair, interval, epoch, gap_start, "
                 "replay_through, source) VALUES ('USDJPY','1m',1,NULL,NULL,?)", (SRC,))
    conn.commit()
    assert conn.execute("SELECT gap_start FROM datafeed_outage_gap").fetchone()[0] is None


def test_old_gap_table_is_rebuilt_keeping_rows_and_the_second_init_is_a_noop(tmp_path):
    path = _old_gap_db(tmp_path)
    fresh = _db(tmp_path, "fresh.db")
    conn = connect(path)
    init_db(conn)
    rows = [tuple(r) for r in conn.execute(
        "SELECT pair, interval, epoch, gap_start, replay_through, source "
        "FROM datafeed_outage_gap ORDER BY interval")]
    assert rows == [
        ("USDJPY", "1h", 3, "2026-11-13T12:00:00+00:00", "2026-11-13T13:00:00+00:00", None),
        ("USDJPY", "1m", 3, "2026-11-13T12:31:00+00:00", None, None)]
    assert _gap_shape(conn) == _gap_shape(fresh)
    before = conn.execute("SELECT sql FROM sqlite_master WHERE name='datafeed_outage_gap'").fetchone()[0]
    init_db(conn)
    init_db(conn)
    assert conn.execute("SELECT sql FROM sqlite_master WHERE name='datafeed_outage_gap'"
                        ).fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM datafeed_outage_gap").fetchone()[0] == 2
    conn.execute("INSERT INTO datafeed_outage_gap (pair, interval, epoch, gap_start, source) "
                 "VALUES ('EURUSD','1m',4,NULL,?)", (SRC,))
    conn.commit()
    leftovers = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'datafeed_outage_gap_%'")]
    assert leftovers == []


def test_gap_rebuild_recovers_from_a_leftover_temporary_table(tmp_path):
    path = _old_gap_db(tmp_path)
    raw = sqlite3.connect(str(path))
    raw.execute("CREATE TABLE datafeed_outage_gap_new (junk INTEGER)")
    raw.commit()
    raw.close()
    conn = connect(path)
    init_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM datafeed_outage_gap").fetchone()[0] == 2
    assert not [r for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name='datafeed_outage_gap_new'")]


def test_old_state_table_gains_the_health_columns_without_touching_the_row(tmp_path):
    path = tmp_path / "s.db"
    legacy = sqlite3.connect(str(path))
    legacy.execute("""
        CREATE TABLE datafeed_outage_state (
          id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT NOT NULL,
          epoch INTEGER NOT NULL DEFAULT 0, confirmed INTEGER NOT NULL DEFAULT 0,
          entered_degraded_at TEXT, ready_streak INTEGER NOT NULL DEFAULT 0,
          pending_human_confirmation INTEGER NOT NULL DEFAULT 0,
          resume_requested_at TEXT, resume_acknowledge INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL)""")
    legacy.execute("INSERT INTO datafeed_outage_state (id, state, epoch, confirmed, updated_at) "
                   "VALUES (1, 'degraded', 66, 1, '2026-11-20T00:00:00+00:00')")
    legacy.commit()
    legacy.close()
    conn = connect(path)
    init_db(conn)
    init_db(conn)
    row = dict(conn.execute("SELECT * FROM datafeed_outage_state").fetchone())
    assert (row["state"], row["epoch"]) == ("degraded", 66)
    for col in ("auto_healthy", "auto_healthy_at", "manual_healthy", "manual_healthy_at"):
        assert row[col] is None
    assert row["migration_confirmation_pending"] == 0
    assert outage_facts.load_health(conn) == outage_facts.Health(None, None, None, None)


def test_key_facts_distinguish_an_absent_row_from_an_explicit_null_watermark(tmp_path):
    conn = _db(tmp_path)
    assert outage_facts.load_key_facts(conn, SRC) == {}
    outage_facts.save_key_facts(conn, "USDJPY", "1m", SRC, observed_through=None,
                                stall_start=None, last_attempt_ok=None, now=T0)
    conn.commit()
    facts = outage_facts.load_key_facts(conn, SRC)
    assert set(facts) == {("USDJPY", "1m")}
    assert facts[("USDJPY", "1m")] == outage_facts.KeyFacts(None, None, None, T0)


def test_key_facts_round_trip_overwrite_and_separate_sources(tmp_path):
    conn = _db(tmp_path)
    outage_facts.save_key_facts(conn, "USDJPY", "1m", SRC, observed_through=T0,
                                stall_start=T1, last_attempt_ok=True, now=T0)
    outage_facts.save_key_facts(conn, "USDJPY", "1m", "yfinance", observed_through=T1,
                                stall_start=None, last_attempt_ok=False, now=T0)
    outage_facts.save_key_facts(conn, "USDJPY", "1m", SRC, observed_through=T1,
                                stall_start=None, last_attempt_ok=False, now=T1)
    conn.commit()
    mt5 = outage_facts.load_key_facts(conn, SRC)[("USDJPY", "1m")]
    assert mt5 == outage_facts.KeyFacts(T1, None, False, T1)
    other = outage_facts.load_key_facts(conn, "yfinance")[("USDJPY", "1m")]
    assert other.observed_through == T1 and other.last_attempt_ok is False


def test_key_facts_rollback_with_the_surrounding_transaction(tmp_path):
    conn = _db(tmp_path)
    outage_facts.save_key_facts(conn, "USDJPY", "1m", SRC, observed_through=T0,
                                stall_start=None, last_attempt_ok=True, now=T0)
    conn.rollback()
    assert outage_facts.load_key_facts(conn, SRC) == {}


def test_health_round_trip(tmp_path):
    conn = _db(tmp_path)
    conn.execute("INSERT INTO datafeed_outage_state (id, state, updated_at) "
                 "VALUES (1, 'ready', '2026-11-18T00:00:00+00:00')")
    outage_facts.save_health(conn, auto_healthy=True, manual_healthy=False, now=T0)
    conn.commit()
    assert outage_facts.load_health(conn) == outage_facts.Health(True, T0, False, T0)


def test_late_bars_are_stored_once_and_keep_their_first_detection_time(tmp_path):
    conn = _db(tmp_path)
    late = Bar("USDJPY", "1m", T0, 150.0, 150.2, 149.9, 150.1, 12.0)
    outage_facts.add_late_bars(conn, [late], SRC, detected_at=T0)
    outage_facts.add_late_bars(conn, [Bar("USDJPY", "1m", T0, 1, 1, 1, 1, 1)], SRC, detected_at=T1)
    conn.commit()
    rows = outage_facts.list_late_bars(conn, SRC)
    assert [(b.symbol, b.interval, b.ts, b.open, b.high, b.low, b.close, b.volume, at)
            for b, at in rows] == [("USDJPY", "1m", T0, 150.0, 150.2, 149.9, 150.1, 12.0, T0)]
    assert outage_facts.list_late_bars(conn, "yfinance") == []
    outage_facts.delete_late_bars(conn, SRC, [("USDJPY", "1m", T0)])
    conn.commit()
    assert outage_facts.list_late_bars(conn, SRC) == []


def test_contact_references_are_listed_per_epoch_and_deduplicated(tmp_path):
    conn = _db(tmp_path)
    outage_facts.add_contact(conn, epoch=5, order_id=7, pair="USDJPY", bar_time=T0,
                             kind="sl", detected_at=T1)
    outage_facts.add_contact(conn, epoch=5, order_id=7, pair="USDJPY", bar_time=T0,
                             kind="both", detected_at=T1)
    outage_facts.add_contact(conn, epoch=5, order_id=8, pair="USDJPY", bar_time=T1,
                             kind="unknown", detected_at=T1)
    outage_facts.add_contact(conn, epoch=6, order_id=9, pair="USDJPY", bar_time=T1,
                             kind="limit", detected_at=T1)
    conn.commit()
    got = [(c.order_id, c.bar_time, c.kind) for c in outage_facts.list_contacts(conn, epoch=5)]
    assert got == [(7, T0, "sl"), (8, T1, "unknown")]
    with pytest.raises(sqlite3.IntegrityError):
        outage_facts.add_contact(conn, epoch=5, order_id=1, pair="USDJPY", bar_time=T0,
                                 kind="bogus", detected_at=T1)


def test_upsert_cache_bars_can_leave_the_commit_to_the_caller(tmp_path):
    conn = _db(tmp_path)
    bar = Bar("USDJPY", "1m", T0, 1, 1, 1, 1, 1)
    assert upsert_cache_bars(conn, [bar], source=SRC, commit=False) == 1
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 0
    assert upsert_cache_bars(conn, [bar], source=SRC) == 1
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 1
