"""回避帯・signal 採否・shadow・反応統計の schema と、signals v3 rebuild、
既存 abandoned 経路の理由と時刻。"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import db as db_module
from agentic_fx.store import missions, signals
from agentic_fx.store.db import TABLE_NAMES, connect, init_db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
V5_SCHEMA = FIXTURES / "db_schema_v5.sql"
LEGACY_PAYLOADS = FIXTURES / "legacy_signal_payloads.json"

NOW = datetime(2026, 10, 30, 13, 0, tzinfo=timezone.utc)
NOW_UTC = NOW.isoformat()

NEW_TABLES = (
    "signal_decisions", "signal_shadow", "ohlcv_slice_sets", "ohlcv_slices",
    "ohlcv_slice_generations", "econ_reaction_keys", "econ_reaction_values")
CHILDREN_OF_SIGNALS = (
    "plugin_deploy_op_signals", "signal_decisions", "signal_shadow")
V2_COLUMNS = (
    "id", "plugin", "content_hash", "pair", "timeframe", "bar_ts", "kind",
    "payload_json", "status", "claimed_by_mission_id", "claimed_at",
    "requeue_count", "created_at")


# --- helpers ---------------------------------------------------------------

def _fresh(tmp_path, name="fresh.db"):
    conn = connect(tmp_path / name)
    init_db(conn)
    return conn


def _v5(tmp_path, name="v5.db"):
    """回避帯の schema 追加前に init_db が作っていた DB (fixture の DDL)。"""
    conn = connect(tmp_path / name)
    conn.executescript(V5_SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def _mission(conn, status="completed"):
    cur = conn.execute(
        "INSERT INTO missions (loop,runner,model,status,started_at) "
        "VALUES ('trade','local','m',?,?)", (status, NOW_UTC))
    conn.commit()
    return cur.lastrowid


def _raw_signal(conn, *, content_hash, status="pending", kind="strategy",
                requeue_count=0, claimed_by=None, claimed_at=None,
                bar_ts="2026-10-30T12:00:00+00:00", timeframe="15m"):
    cur = conn.execute(
        "INSERT INTO signals (plugin,content_hash,pair,timeframe,bar_ts,kind,"
        "payload_json,status,claimed_by_mission_id,claimed_at,requeue_count,"
        "created_at) VALUES ('p',?,'USDJPY',?,?,?,'{}',?,?,?,?,?)",
        (content_hash, timeframe, bar_ts, kind, status, claimed_by, claimed_at,
         requeue_count, NOW_UTC))
    conn.commit()
    if kind == "strategy" and "contract_version" in {
            r[1] for r in conn.execute("PRAGMA table_info(signals)")}:
        conn.execute("UPDATE signals SET contract_version='executable_v1' WHERE id=?",
                     (cur.lastrowid,))
        conn.commit()
    return cur.lastrowid


def _deploy_op(conn, name="p"):
    cur = conn.execute(
        "INSERT INTO plugin_deploy_ops (op_kind,name,from_kind,to_kind,"
        "from_target,subject_content_hash,subject_artifact_hash,revoke,force,"
        "phase,notify_attempted,actor,created_at,updated_at) VALUES "
        "('disable_symlink',?,'symlink','absent','t','h','a',0,0,'done',0,"
        "'human_cli',?,?)", (name, NOW_UTC, NOW_UTC))
    conn.commit()
    return cur.lastrowid


def _link(conn, op_id, signal_id, status_at_link="pending"):
    conn.execute(
        "INSERT INTO plugin_deploy_op_signals (op_id,signal_id,status_at_link) "
        "VALUES (?,?,?)", (op_id, signal_id, status_at_link))
    conn.commit()


def _row(conn, sid):
    return dict(conn.execute(
        "SELECT status, requeue_count, abandon_reason, abandoned_at, "
        "contract_version FROM signals WHERE id=?", (sid,)).fetchone())


def _body(sql):
    return " ".join(sql[sql.index("("):].split())


def _shape(conn, table):
    info = [tuple(r) for r in conn.execute(f"PRAGMA table_info({table})")]
    fks = sorted(tuple(r)[2:] for r in conn.execute(
        f"PRAGMA foreign_key_list({table})"))
    indexes = []
    for idx in conn.execute(f"PRAGMA index_list({table})"):
        cols = tuple(r["name"] for r in conn.execute(
            f"PRAGMA index_info('{idx['name']}')"))
        indexes.append((idx["unique"], idx["origin"], idx["partial"], cols))
    return info, fks, sorted(indexes)


def _table_sql(conn, table):
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()[0]


def _schema_dump(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")]


def _all_rows(conn, table):
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


# --- fresh / existing DB の同形 --------------------------------------------

def test_new_tables_are_registered_and_created(tmp_path):
    conn = _fresh(tmp_path)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(NEW_TABLES) <= names
    assert set(NEW_TABLES) <= TABLE_NAMES


def test_v5_fixture_is_the_pre_change_schema():
    """fixture は rebuild 前の形を持つ (fixture を作り直して前提が消える変異の検出)。"""
    probe = sqlite3.connect(":memory:")
    probe.executescript(V5_SCHEMA.read_text(encoding="utf-8"))
    names = {r[0] for r in probe.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not set(NEW_TABLES) & names
    cols = {r[1] for r in probe.execute("PRAGMA table_info(signals)")}
    assert not {"contract_version", "abandon_reason", "abandoned_at"} & cols
    assert "superseded_by" not in {
        r[1] for r in probe.execute("PRAGMA table_info(econ_events)")}
    assert "'econ_window'" not in probe.execute(
        "SELECT sql FROM sqlite_master WHERE name='trade_intents'").fetchone()[0]


def test_fresh_and_existing_db_have_the_same_schema(tmp_path):
    fresh = _fresh(tmp_path)
    existing = _v5(tmp_path)
    init_db(existing)
    for table in sorted(TABLE_NAMES):
        assert _shape(existing, table) == _shape(fresh, table), table
    for table in (*NEW_TABLES, "signals", "trade_intents"):
        assert _body(_table_sql(existing, table)) == _body(
            _table_sql(fresh, table)), table
    fresh_indexes = {r[0]: r[1] for r in fresh.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index'")}
    existing_indexes = {r[0]: r[1] for r in existing.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index'")}
    assert existing_indexes == fresh_indexes


def test_new_tables_create_only_primary_and_unique_indexes(tmp_path):
    conn = _fresh(tmp_path)
    for table in NEW_TABLES:
        for idx in conn.execute(f"PRAGMA index_list({table})"):
            assert idx["origin"] in ("pk", "u"), (table, idx["name"])


def test_signals_v3_columns_and_econ_events_superseded_by(tmp_path):
    conn = _fresh(tmp_path)
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(signals)")}
    for name in ("contract_version", "abandon_reason", "abandoned_at"):
        assert cols[name]["notnull"] == 0 and cols[name]["dflt_value"] is None
    fk = [tuple(r)[2:5] for r in conn.execute(
        "PRAGMA foreign_key_list(econ_events)")]
    assert fk == [("econ_events", "superseded_by", "id")]


@pytest.mark.parametrize("builder", ["fresh", "v5"])
def test_second_init_changes_neither_rows_nor_schema(tmp_path, builder):
    conn = _fresh(tmp_path) if builder == "fresh" else _v5(tmp_path)
    mid = _mission(conn)
    if builder == "v5":
        _raw_signal(conn, content_hash="a", status="abandoned")
    else:
        _raw_signal(conn, content_hash="a")
        signals.expire_stale(conn, now=NOW + timedelta(hours=1),
                             freshness_bars=2)
    _raw_signal(conn, content_hash="b", status="claimed", claimed_by=mid,
                claimed_at=NOW_UTC)
    conn.execute(
        "INSERT INTO trade_intents (mission_id,payload_json,action,gate_result,"
        "reject_reason,reject_category,created_at) VALUES "
        "(?,'{}','open','rejected','r','signal',?)", (mid, NOW_UTC))
    conn.commit()
    init_db(conn)
    before = (_schema_dump(conn), _all_rows(conn, "signals"),
              _all_rows(conn, "trade_intents"),
              _all_rows(conn, "data_migrations"))
    backups = sorted(p.name for p in tmp_path.iterdir())

    init_db(conn)

    after = (_schema_dump(conn), _all_rows(conn, "signals"),
             _all_rows(conn, "trade_intents"),
             _all_rows(conn, "data_migrations"))
    assert after == before
    assert sorted(p.name for p in tmp_path.iterdir()) == backups


def test_fresh_init_takes_no_signals_backup(tmp_path):
    _fresh(tmp_path)
    assert not (tmp_path / "fresh.db.bak-signals-v3").exists()
    assert not (tmp_path / "fresh.db.bak-trade-intents-observability").exists()


def test_v5_init_backs_up_before_the_signals_rebuild(tmp_path):
    conn = _v5(tmp_path)
    _raw_signal(conn, content_hash="x")
    init_db(conn)
    backup = sqlite3.connect(tmp_path / "v5.db.bak-signals-v3")
    try:
        cols = {r[1] for r in backup.execute("PRAGMA table_info(signals)")}
        assert "abandon_reason" not in cols
        assert backup.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
    finally:
        backup.close()


# --- rebuild: 子表の参照先 -------------------------------------------------

def _assert_children_reference_signals(conn):
    for child in CHILDREN_OF_SIGNALS:
        targets = {fk["table"] for fk in conn.execute(
            f"PRAGMA foreign_key_list({child})")
            if fk["from"] == "signal_id" and fk["to"] == "id"}
        assert targets == {"signals"}, child
    for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"):
        assert "signals_v1" not in sql and "signals_new" not in sql, name
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _decision_and_shadow(conn, *, mission_id, signal_id):
    cur = conn.execute(
        "INSERT INTO signal_decisions (mission_id,signal_id,trade_intent_id,"
        "decision,actor,reject_code,decided_at) VALUES "
        "(?,?,NULL,'reject','system','econ_window',?)",
        (mission_id, signal_id, NOW_UTC))
    decision_id = cur.lastrowid
    conn.execute(
        "INSERT INTO signal_shadow (decision_id,signal_id,decision,actor,"
        "reject_code,state,pip_size,spread_pips,spread_source,rule_version,"
        "created_at,updated_at) VALUES "
        "(?,?,'reject','system','econ_window','pending_entry',0.01,1.0,"
        "'assumed',1,?,?)", (decision_id, signal_id, NOW_UTC, NOW_UTC))
    conn.commit()
    return decision_id


def test_rebuild_keeps_all_three_child_tables_on_signals(tmp_path):
    conn = _v5(tmp_path)
    mid = _mission(conn)
    sid = _raw_signal(conn, content_hash="linked")
    _link(conn, _deploy_op(conn), sid)

    init_db(conn)

    _assert_children_reference_signals(conn)
    other = _raw_signal(conn, content_hash="decided", status="consumed")
    _decision_and_shadow(conn, mission_id=mid, signal_id=other)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO plugin_deploy_op_signals (op_id,signal_id,"
            "status_at_link) VALUES (1, 9999, 'pending')")
    with pytest.raises(sqlite3.IntegrityError):
        _decision_and_shadow(conn, mission_id=mid, signal_id=9999)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def _legacy_pre_fk_signals_ddl():
    return (
        "CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "plugin TEXT NOT NULL, content_hash TEXT NOT NULL,"
        "pair TEXT NOT NULL, timeframe TEXT NOT NULL, bar_ts TEXT NOT NULL,"
        "kind TEXT NOT NULL, payload_json TEXT NOT NULL,"
        "status TEXT NOT NULL DEFAULT 'pending'"
        "  CHECK(status IN ('pending','claimed','consumed','abandoned')),"
        "claimed_by_mission_id INTEGER, claimed_at TEXT,"
        "requeue_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,"
        "UNIQUE(plugin, content_hash, pair, timeframe, bar_ts))")


def test_pre_fk_signals_db_keeps_child_tables_on_signals(tmp_path):
    """FK 追加前の signals を持つ DB でも、v2 化の RENAME が子表の参照先を
    書き換えない (子行がある状態で確かめる)。"""
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_pre_fk_signals_ddl())
    conn.commit()
    conn.executescript(db_module._SCHEMA)
    sid = _raw_signal(conn, content_hash="legacy", status="abandoned")
    _link(conn, _deploy_op(conn), sid)

    init_db(conn)

    _assert_children_reference_signals(conn)
    assert _row(conn, sid)["abandon_reason"] == "legacy_unknown"


# --- 既存 abandoned の移行 (legacy_unknown) --------------------------------

def test_existing_abandoned_rows_all_become_legacy_unknown(tmp_path):
    conn = _v5(tmp_path)
    mid = _mission(conn)
    ids = {
        "ab_strategy_0": _raw_signal(conn, content_hash="1", status="abandoned"),
        "ab_strategy_2": _raw_signal(conn, content_hash="2", status="abandoned",
                                     requeue_count=2,
                                     claimed_at="2026-10-30T12:20:00+00:00"),
        "ab_signal_1": _raw_signal(conn, content_hash="3", status="abandoned",
                                   kind="signal", requeue_count=1),
        "pending": _raw_signal(conn, content_hash="4"),
        "claimed": _raw_signal(conn, content_hash="5", status="claimed",
                               claimed_by=mid, claimed_at=NOW_UTC),
        "consumed": _raw_signal(conn, content_hash="6", status="consumed",
                                claimed_by=mid, claimed_at=NOW_UTC),
    }
    columns = ",".join(V2_COLUMNS)
    before = {r["id"]: tuple(r) for r in conn.execute(
        f"SELECT {columns} FROM signals")}

    init_db(conn)

    after = {r["id"]: tuple(r) for r in conn.execute(
        f"SELECT {columns} FROM signals")}
    assert {sid: after[sid] for key, sid in ids.items()
            if key.startswith("ab_") or key == "consumed"} == {
        sid: before[sid] for key, sid in ids.items()
        if key.startswith("ab_") or key == "consumed"}
    for key, sid in ids.items():
        row = _row(conn, sid)
        assert row["contract_version"] is None
        if key.startswith("ab_"):
            assert (row["abandon_reason"], row["abandoned_at"]) == (
                "legacy_unknown", None), key
        elif key in {"pending", "claimed"}:
            assert (row["status"], row["abandon_reason"]) == (
                "abandoned", "normalization_failed")
            assert row["abandoned_at"] is not None
        else:
            assert (row["abandon_reason"], row["abandoned_at"]) == (
                None, None), key
    snapshot = _all_rows(conn, "signals")
    init_db(conn)
    assert _all_rows(conn, "signals") == snapshot


def test_rebuild_keeps_the_autoincrement_high_water_mark(tmp_path):
    conn = _v5(tmp_path)
    _raw_signal(conn, content_hash="keep")
    gone = _raw_signal(conn, content_hash="gone")
    conn.execute("DELETE FROM signals WHERE id=?", (gone,))
    conn.commit()

    init_db(conn)

    new_id = _raw_signal(conn, content_hash="next")
    assert new_id == gone + 1


def test_rebuild_with_an_empty_signals_table_keeps_the_high_water_mark(tmp_path):
    conn = _v5(tmp_path)
    gone = _raw_signal(conn, content_hash="gone")
    conn.execute("DELETE FROM signals")
    conn.commit()

    init_db(conn)

    assert _raw_signal(conn, content_hash="next") == gone + 1


def test_rebuild_carries_existing_v3_values_when_only_the_check_differs(tmp_path):
    """列は揃っているが CHECK 本文が古い表の rebuild は、書かれた理由と時刻を
    消さない。"""
    conn = _fresh(tmp_path)
    sid = _raw_signal(conn, content_hash="keep")
    signals.expire_stale(conn, now=NOW + timedelta(hours=3), freshness_bars=2)
    kept = _row(conn, sid)
    old_sql = _table_sql(conn, "signals")
    stale_sql = old_sql.replace("'ineligible_cleanup',", "", 1)
    assert stale_sql != old_sql
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE signals RENAME TO signals_old")
    conn.execute(stale_sql)
    conn.execute("INSERT INTO signals SELECT * FROM signals_old")
    conn.execute("DROP TABLE signals_old")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")

    init_db(conn)

    assert "'ineligible_cleanup'" in _table_sql(conn, "signals")
    assert _row(conn, sid) == kept
    assert kept["abandon_reason"] == "freshness_expired"


def test_leftover_signals_new_is_dropped_without_rebuilding(tmp_path):
    conn = _fresh(tmp_path)
    sid = _raw_signal(conn, content_hash="x")
    signals.expire_stale(conn, now=NOW + timedelta(hours=3), freshness_bars=2)
    kept = _row(conn, sid)
    conn.execute("CREATE TABLE signals_new (x INTEGER)")
    conn.commit()

    init_db(conn)

    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='signals_new'"
                        ).fetchone() is None
    assert _row(conn, sid) == kept
    assert not (tmp_path / "fresh.db.bak-signals-v3").exists()


# --- trade_intents の econ_window --------------------------------------------

def test_trade_intents_accepts_econ_window_after_rebuild_and_keeps_rows(tmp_path):
    conn = _v5(tmp_path)
    mid = _mission(conn)
    conn.execute(
        "INSERT INTO trade_intents (id,mission_id,payload_json,action,gate_result,"
        "reject_reason,reject_category,created_at) VALUES "
        "(5,?,'{}','open','rejected','r','signal',?)", (mid, NOW_UTC))
    conn.execute(
        "INSERT INTO orders (id,intent_id,pair,direction,entry_type,horizon,"
        "status,created_at,updated_at) VALUES "
        "(1,5,'USDJPY','long','market','day','open',?,?)", (NOW_UTC, NOW_UTC))
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO trade_intents (mission_id,payload_json,action,"
            "gate_result,reject_reason,reject_category,created_at) VALUES "
            "(?,'{}','open','rejected','r','econ_window',?)", (mid, NOW_UTC))
    conn.rollback()

    init_db(conn)

    row = conn.execute(
        "SELECT action, reject_category FROM trade_intents WHERE id=5").fetchone()
    assert tuple(row) == ("open", "signal")
    conn.execute(
        "INSERT INTO trade_intents (mission_id,payload_json,action,gate_result,"
        "reject_reason,reject_category,created_at) VALUES "
        "(?,'{}','open','rejected','r','econ_window',?)", (mid, NOW_UTC))
    assert [fk["table"] for fk in conn.execute(
        "PRAGMA foreign_key_list(orders)") if fk["from"] == "intent_id"] == [
            "trade_intents"]
    for bad in (("rejected", None), ("accepted", "econ_window"),
                ("rejected", "econ")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO trade_intents (mission_id,payload_json,action,"
                "gate_result,reject_reason,reject_category,created_at) VALUES "
                "(?,'{}','open',?,'r',?,?)", (mid, bad[0], bad[1], NOW_UTC))


# --- signals v3 の CHECK ---------------------------------------------------

def _set(conn, sid, **cols):
    assignments = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE signals SET {assignments} WHERE id=?",
                 (*cols.values(), sid))


@pytest.mark.parametrize("cols", [
    {"status": "abandoned", "abandon_reason": None, "abandoned_at": NOW_UTC},
    {"status": "abandoned", "abandon_reason": None, "abandoned_at": None},
    {"status": "abandoned", "abandon_reason": "freshness_expired",
     "abandoned_at": None},
    {"status": "abandoned", "abandon_reason": "deploy_op", "abandoned_at": None},
    {"status": "abandoned", "abandon_reason": "unknown_reason",
     "abandoned_at": NOW_UTC},
    {"status": "pending", "abandon_reason": "freshness_expired"},
    {"status": "pending", "abandoned_at": NOW_UTC},
    {"status": "claimed", "abandon_reason": "legacy_unknown"},
    {"status": "consumed", "abandoned_at": NOW_UTC},
    {"contract_version": "executable_v2"},
    {"contract_version": ""},
])
def test_signals_check_rejects_inconsistent_rows(tmp_path, cols):
    conn = _fresh(tmp_path)
    sid = _raw_signal(conn, content_hash="x")
    with pytest.raises(sqlite3.IntegrityError):
        _set(conn, sid, **cols)


@pytest.mark.parametrize("cols", [
    {"status": "abandoned", "abandon_reason": "legacy_unknown",
     "abandoned_at": None},
    {"status": "abandoned", "abandon_reason": "legacy_unknown",
     "abandoned_at": NOW_UTC},
    *({"status": "abandoned", "abandon_reason": reason, "abandoned_at": NOW_UTC}
      for reason in db_module.SIGNAL_ABANDON_REASONS),
    {"contract_version": "executable_v1"},
    {"contract_version": None},
])
def test_signals_check_accepts_the_canonical_rows(tmp_path, cols):
    conn = _fresh(tmp_path)
    sid = _raw_signal(conn, content_hash="x")
    _set(conn, sid, **cols)


def test_abandon_reason_vocabulary_is_fixed():
    assert db_module.SIGNAL_ABANDON_REASONS == (
        "freshness_expired", "requeued_expired", "retry_exhausted",
        "source_inactive", "normalization_failed", "deploy_op",
        "deploy_linked", "ineligible_cleanup", "legacy_unknown")


# --- 既存 abandoned 経路 6 本: 理由と時刻 ----------------------------------

def _store_conn(tmp_path):
    return _fresh(tmp_path, "store.db")


def _claimed(conn, *, content_hash, requeue_count=0, mission_id=None):
    mid = mission_id if mission_id is not None else _mission(conn, "running")
    return _raw_signal(conn, content_hash=content_hash, status="claimed",
                       requeue_count=requeue_count, claimed_by=mid,
                       claimed_at="2026-10-30T12:30:00+00:00"), mid


def test_expire_stale_writes_the_freshness_reason_by_requeue_history(tmp_path):
    conn = _store_conn(tmp_path)
    fresh_once = _raw_signal(conn, content_hash="0")
    requeued = _raw_signal(conn, content_hash="1", requeue_count=1)
    unknown_tf = _raw_signal(conn, content_hash="2", timeframe="7m")
    alive = _raw_signal(conn, content_hash="3",
                        bar_ts="2026-10-30T15:00:00+00:00")
    later = NOW + timedelta(hours=1)

    result = signals.expire_stale(conn, now=later, freshness_bars=2)

    assert result.total == 3
    assert _row(conn, fresh_once)["abandon_reason"] == "freshness_expired"
    assert _row(conn, requeued)["abandon_reason"] == "requeued_expired"
    assert _row(conn, unknown_tf)["abandon_reason"] == "freshness_expired"
    for sid in (fresh_once, requeued, unknown_tf):
        assert _row(conn, sid)["abandoned_at"] == later.isoformat()
    assert _row(conn, alive) == {
        "status": "pending", "requeue_count": 0, "abandon_reason": None,
        "abandoned_at": None, "contract_version": "executable_v1"}


@pytest.mark.parametrize("reason", ["freshness_expired", "requeued_expired",
                                    "source_inactive"])
def test_abandon_claimed_records_the_given_reason_and_utc_time(tmp_path, reason):
    conn = _store_conn(tmp_path)
    sid, _ = _claimed(conn, content_hash="x")
    jst = NOW.astimezone(timezone(timedelta(hours=9)))

    signals.abandon_claimed(conn, sid, now=jst, reason=reason)

    assert _row(conn, sid) == {
        "status": "abandoned", "requeue_count": 0, "abandon_reason": reason,
        "abandoned_at": NOW_UTC, "contract_version": "executable_v1"}


def test_abandon_claimed_rejects_naive_time_without_writing(tmp_path):
    conn = _store_conn(tmp_path)
    sid, _ = _claimed(conn, content_hash="x")
    with pytest.raises(ValueError, match="naive"):
        signals.abandon_claimed(conn, sid, now=NOW.replace(tzinfo=None),
                                reason="source_inactive")
    assert _row(conn, sid)["status"] == "claimed"


def test_freshness_abandon_reason_follows_the_requeue_history():
    base = {"kind": "strategy", "contract_version": "executable_v1"}
    assert signals.freshness_abandon_reason({**base, "requeue_count": 0}) == \
        "freshness_expired"
    assert signals.freshness_abandon_reason({**base, "requeue_count": 1}) == \
        "requeued_expired"


@pytest.mark.parametrize("requeue_count, linked, expected", [
    (0, False, {"status": "pending", "requeue_count": 1,
                "abandon_reason": None, "abandoned_at": None}),
    (2, False, {"status": "abandoned", "requeue_count": 2,
                "abandon_reason": "retry_exhausted", "abandoned_at": NOW_UTC}),
    (0, True, {"status": "abandoned", "requeue_count": 0,
               "abandon_reason": "deploy_linked", "abandoned_at": NOW_UTC}),
    (2, True, {"status": "abandoned", "requeue_count": 2,
               "abandon_reason": "deploy_linked", "abandoned_at": NOW_UTC}),
])
@pytest.mark.parametrize("path", ["requeue", "reclaim_expired",
                                  "recover_interrupted"])
def test_requeue_family_writes_retry_or_deploy_reason(
        tmp_path, path, requeue_count, linked, expected):
    conn = _store_conn(tmp_path)
    sid, _ = _claimed(conn, content_hash="x", requeue_count=requeue_count)
    if linked:
        _link(conn, _deploy_op(conn), sid, "claimed")

    if path == "requeue":
        signals.requeue(conn, sid, now=NOW, max_requeue=2)
    elif path == "reclaim_expired":
        assert signals.reclaim_expired(
            conn, now=NOW, lease_min=15, max_requeue=2) == [expected["status"]]
    else:
        missions.recover_interrupted(conn, now=NOW, max_requeue=2)

    row = _row(conn, sid)
    row.pop("contract_version")
    assert row == expected


def test_deploy_op_pending_abandon_writes_deploy_op_inside_the_caller_tx(tmp_path):
    conn = _store_conn(tmp_path)
    by_identity = _raw_signal(conn, content_hash="h")
    by_name = conn.execute(
        "INSERT INTO signals (plugin,content_hash,pair,timeframe,bar_ts,kind,"
        "payload_json,created_at) VALUES ('other','z','USDJPY','15m',?,"
        "'strategy','{}',?)", ("2026-10-30T12:00:00+00:00", NOW_UTC)).lastrowid
    conn.commit()
    claimed, _ = _claimed(conn, content_hash="c")
    op_id = _deploy_op(conn)

    signals.abandon_pending_for_identities(
        conn, [("p", "h"), ("p", "c")], op_id=op_id, now=NOW, commit=False)
    signals.abandon_pending_for_name(conn, "other", op_id=op_id, now=NOW,
                                     commit=False)
    conn.commit()

    for sid in (by_identity, by_name):
        row = _row(conn, sid)
        assert (row["status"], row["abandon_reason"], row["abandoned_at"]) == (
            "abandoned", "deploy_op", NOW_UTC)
    assert _row(conn, claimed)["status"] == "claimed"
    links = {r[0]: r[1] for r in conn.execute(
        "SELECT signal_id, status_at_link FROM plugin_deploy_op_signals")}
    assert links == {by_identity: "pending", by_name: "pending",
                     claimed: "claimed"}


def test_every_new_abandoned_row_has_reason_and_time(tmp_path):
    conn = _store_conn(tmp_path)
    _raw_signal(conn, content_hash="stale")
    signals.expire_stale(conn, now=NOW + timedelta(hours=1), freshness_bars=2)
    sid, _ = _claimed(conn, content_hash="over", requeue_count=2)
    signals.requeue(conn, sid, now=NOW, max_requeue=2)
    missing = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE status='abandoned' AND "
        "(abandon_reason IS NULL OR abandoned_at IS NULL)").fetchone()[0]
    assert missing == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM signals WHERE status='abandoned'").fetchone()[0] == 2


# --- 専用採否 loop の claim 終端 ---------------------------------------------

_ACCEPT = {"decision": "accept", "reject_code": None}


def _loop_world(tmp_path, results):
    from tests.loops.test_trade_loop import NOW as LOOP_NOW, _loop
    conn, loop, runner, _ = _loop(tmp_path, results)
    sid = signals.add(
        conn, plugin="strategy", content_hash="a" * 64, pair="USDJPY",
        timeframe="1h", bar_ts=(LOOP_NOW - timedelta(hours=1)).isoformat(),
        kind="strategy", payload={"action": "open", "pair": "USDJPY",
        "direction": "long", "entry_type": "market", "horizon": "day",
        "stop_loss": 147.0, "take_profit": 150.0},
        contract_version="executable_v1",
        now=LOOP_NOW)
    from types import SimpleNamespace
    loop.econ.window_view.return_value.in_window = False
    loop.executor.gather_open_snapshot = lambda *args, **kwargs: SimpleNamespace(
        captured_at=LOOP_NOW)
    loop.executor.open_from_snapshot = lambda *args, **kwargs: {
        "result": "submitted", "order_id": None, "reasons": []}
    return conn, loop, runner, sid, LOOP_NOW


def _jump(loop, when):
    clock = FixedClock(when)
    loop.clock = clock
    loop.executor.clock = clock


def test_trade_loop_source_inactive_abandons_with_source_inactive(
        tmp_path, monkeypatch):
    conn, loop, _, sid, loop_now = _loop_world(
        tmp_path, [MissionResult("completed", _ACCEPT, [])])
    loop._plugins_root = tmp_path / "plugins"
    monkeypatch.setattr(
        "agentic_fx.loops.trade_loop.signal_source_active", lambda *_a: False)

    loop.run_once("signal")

    row = _row(conn, sid)
    assert (row["status"], row["abandon_reason"], row["abandoned_at"]) == (
        "abandoned", "source_inactive", loop_now.isoformat())


@pytest.mark.parametrize("requeue_count, reason", [
    (0, "freshness_expired"), (1, "requeued_expired")])
def test_trade_loop_commit_core_stale_abandons_with_freshness_reason(
        tmp_path, requeue_count, reason):
    conn, loop, _, sid, loop_now = _loop_world(tmp_path, [])
    conn.execute("UPDATE signals SET requeue_count=? WHERE id=?",
                 (requeue_count, sid))
    conn.commit()
    late = loop_now + timedelta(hours=3)

    class AdvancingRunner:
        def run(self, _mission):
            _jump(loop, late)
            return MissionResult("completed", _ACCEPT, [])

    loop.runner = AdvancingRunner()
    loop.run_once("signal")

    row = _row(conn, sid)
    assert (row["status"], row["abandon_reason"], row["abandoned_at"]) == (
        "abandoned", reason, late.isoformat())


@pytest.mark.parametrize("requeue_count, reason", [
    (0, "freshness_expired"), (1, "requeued_expired")])
def test_trade_loop_data_state_cleanup_abandons_stale_claim_with_freshness_reason(
        tmp_path, requeue_count, reason):
    """新規リスク停止中に鮮度が切れた claim の後始末 (`_requeue_signal` の
    abandon_if_stale 分岐)。"""
    conn, loop, _, sid, loop_now = _loop_world(tmp_path, [])
    conn.execute("UPDATE signals SET requeue_count=? WHERE id=?",
                 (requeue_count, sid))
    conn.commit()
    mid = missions.start(conn, "trade", "local", "m", loop_now)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=loop_now,
                                   freshness_bars=None)
    late = loop_now + timedelta(hours=5)
    _jump(loop, late)

    msg, terminal = loop._requeue_signal(claimed, abandon_if_stale=True)

    assert msg is not None and "鮮度切れ" in msg
    assert terminal == "freshness_abandoned"
    row = _row(conn, sid)
    assert (row["status"], row["requeue_count"], row["abandon_reason"],
            row["abandoned_at"]) == ("abandoned", requeue_count, reason,
                                     late.isoformat())


# --- data_migrations の冪等 guard と呼出し順 --------------------------------

def test_data_migration_helper_applies_once_and_records_the_name(tmp_path):
    conn = _fresh(tmp_path)
    calls = []

    def migrate(c):
        calls.append(c.in_transaction)
        c.execute("INSERT INTO alert_state (key,value,updated_at) "
                  "VALUES (?,'v',?)", (f"k{len(calls)}", NOW_UTC))

    assert db_module._apply_data_migration_once(conn, "probe_v1", migrate)
    assert not db_module._apply_data_migration_once(conn, "probe_v1", migrate)
    assert calls == [True]
    assert conn.execute("SELECT name FROM data_migrations WHERE name='probe_v1'"
                        ).fetchone() is not None
    assert conn.execute("SELECT COUNT(*) FROM alert_state").fetchone()[0] == 1
    assert db_module._apply_data_migration_once(conn, "probe_v2", migrate)
    assert calls == [True, True]
    assert conn.execute("SELECT COUNT(*) FROM alert_state").fetchone()[0] == 2


def test_data_migration_helper_rolls_back_change_and_record_on_failure(tmp_path):
    conn = _fresh(tmp_path)

    def broken(c):
        c.execute("INSERT INTO alert_state (key,value,updated_at) "
                  "VALUES ('k','v',?)", (NOW_UTC,))
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        db_module._apply_data_migration_once(conn, "probe_v1", broken)
    assert conn.execute("SELECT COUNT(*) FROM alert_state").fetchone()[0] == 0
    assert conn.execute("SELECT 1 FROM data_migrations WHERE name='probe_v1'"
                        ).fetchone() is None
    assert not conn.in_transaction


def test_signal_data_migrations_run_after_the_v3_rebuild(tmp_path, monkeypatch):
    conn = _v5(tmp_path)
    _raw_signal(conn, content_hash="x", status="abandoned")
    seen = []

    def probe(c):
        cols = {r[1] for r in c.execute("PRAGMA table_info(signals)")}
        seen.append(("abandon_reason" in cols, c.execute(
            "SELECT abandon_reason FROM signals").fetchone()[0]))

    monkeypatch.setattr(db_module, "_signal_data_migrations",
                        lambda: (("probe_after_v3", probe),))
    init_db(conn)
    init_db(conn)

    assert seen == [(True, "legacy_unknown")]


def test_signal_data_migrations_register_the_payload_normalizer():
    names = [name for name, _ in db_module._signal_data_migrations()]
    assert names == ["normalize_strategy_payloads_v1"]


# --- 新表の CHECK -----------------------------------------------------------

def _decision(conn, *, mission_id, signal_id, decision, actor, reject_code,
              intent_id):
    return conn.execute(
        "INSERT INTO signal_decisions (mission_id,signal_id,trade_intent_id,"
        "decision,actor,reject_code,decided_at) VALUES (?,?,?,?,?,?,?)",
        (mission_id, signal_id, intent_id, decision, actor, reject_code,
         NOW_UTC)).lastrowid


@pytest.fixture
def decided(tmp_path):
    conn = _fresh(tmp_path)
    mid = _mission(conn)
    sid = _raw_signal(conn, content_hash="d", status="consumed")
    iid = conn.execute(
        "INSERT INTO trade_intents (mission_id,payload_json,action,created_at) "
        "VALUES (?,'{}','open',?)", (mid, NOW_UTC)).lastrowid
    conn.commit()
    return conn, mid, sid, iid


@pytest.mark.parametrize("decision, actor, code, with_intent", [
    ("accept", "llm", None, True),
    ("reject", "llm", "news_shock", False),
    ("reject", "llm", "opposite_position", False),
    ("reject", "system", "econ_window", False),
])
def test_signal_decisions_accept_the_three_canonical_forms(
        decided, decision, actor, code, with_intent):
    conn, mid, sid, iid = decided
    _decision(conn, mission_id=mid, signal_id=sid, decision=decision,
              actor=actor, reject_code=code,
              intent_id=iid if with_intent else None)


@pytest.mark.parametrize("decision, actor, code, with_intent", [
    ("accept", "llm", None, False),
    ("accept", "llm", "news_shock", True),
    ("accept", "system", None, True),
    ("reject", "llm", None, False),
    ("reject", "llm", "econ_window", False),
    ("reject", "llm", "news_shock", True),
    ("reject", "system", "news_shock", False),
    ("reject", "system", None, False),
    ("reject", "system", "econ_window", True),
    ("hold", "llm", None, False),
    ("reject", "human", "econ_window", False),
])
def test_signal_decisions_reject_other_forms(
        decided, decision, actor, code, with_intent):
    conn, mid, sid, iid = decided
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, mission_id=mid, signal_id=sid, decision=decision,
                  actor=actor, reject_code=code,
                  intent_id=iid if with_intent else None)


def test_signal_decisions_are_unique_per_signal_and_need_real_parents(decided):
    conn, mid, sid, _ = decided
    _decision(conn, mission_id=mid, signal_id=sid, decision="reject",
              actor="system", reject_code="econ_window", intent_id=None)
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, mission_id=mid, signal_id=sid, decision="reject",
                  actor="llm", reject_code="news_shock", intent_id=None)
    other = _raw_signal(conn, content_hash="e", status="consumed")
    for kwargs in ({"mission_id": 9999, "signal_id": other, "intent_id": None},
                   {"mission_id": mid, "signal_id": 9999, "intent_id": None}):
        with pytest.raises(sqlite3.IntegrityError):
            _decision(conn, decision="reject", actor="system",
                      reject_code="econ_window", **kwargs)
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, mission_id=mid, signal_id=other, decision="accept",
                  actor="llm", reject_code=None, intent_id=9999)


_SHADOW_BASE = {
    "decision": "reject", "actor": "system", "reject_code": "econ_window",
    "state": "pending_entry", "entry_price": None, "entry_at": None,
    "result_code": None, "exit_price": None, "pnl_pips": None,
    "resolved_at": None, "pip_size": 0.01, "spread_pips": 1.0,
    "spread_source": "assumed", "rule_version": 1,
}
_FILL = {"entry_price": 150.0, "entry_at": NOW_UTC}
_EXIT = {"exit_price": 150.5, "pnl_pips": 50.0, "resolved_at": NOW_UTC}


def _shadow(conn, mid, **overrides):
    sid = _raw_signal(conn, content_hash=f"s{conn.total_changes}",
                      status="consumed")
    did = _decision(conn, mission_id=mid, signal_id=sid, decision="reject",
                    actor="system", reject_code="econ_window", intent_id=None)
    values = {**_SHADOW_BASE, **overrides}
    cols = ",".join(values)
    marks = ",".join("?" * len(values))
    conn.execute(
        f"INSERT INTO signal_shadow (decision_id,signal_id,{cols},created_at,"
        f"updated_at) VALUES (?,?,{marks},?,?)",
        (did, sid, *values.values(), NOW_UTC, NOW_UTC))


@pytest.mark.parametrize("overrides", [
    {},
    {"state": "filled", **_FILL},
    {"state": "resolved", "result_code": "sl", **_FILL, **_EXIT},
    {"state": "resolved", "result_code": "tp", **_FILL, **_EXIT},
    {"state": "resolved", "result_code": "day_close", **_FILL, **_EXIT},
    {"state": "resolved", "result_code": "data_insufficient"},
    {"state": "resolved", "result_code": "data_insufficient", **_FILL},
    {"state": "expired", "result_code": "no_fill"},
    {"decision": "accept", "actor": "llm", "reject_code": None},
    {"decision": "reject", "actor": "llm", "reject_code": "opposite_position",
     "spread_source": "quote"},
])
def test_signal_shadow_accepts_the_four_canonical_states(decided, overrides):
    conn, mid, _, _ = decided
    _shadow(conn, mid, **overrides)


@pytest.mark.parametrize("overrides", [
    {"state": "pending_entry", **_FILL},
    {"state": "pending_entry", "result_code": "sl"},
    {"state": "filled"},
    {"state": "filled", "entry_price": 150.0},
    {"state": "filled", "entry_at": NOW_UTC},
    {"state": "filled", **_FILL, "exit_price": 150.5},
    {"state": "filled", **_FILL, "result_code": "tp"},
    {"state": "resolved", "result_code": "sl", **_FILL},
    {"state": "resolved", "result_code": "sl", **_EXIT},
    {"state": "resolved", "result_code": "tp", **_FILL, **_EXIT,
     "pnl_pips": None},
    {"state": "resolved", "result_code": None, **_FILL, **_EXIT},
    {"state": "resolved", "result_code": "no_fill"},
    {"state": "resolved", "result_code": "data_insufficient", **_FILL, **_EXIT},
    {"state": "resolved", "result_code": "data_insufficient",
     "entry_price": 150.0},
    *({"state": "resolved", "result_code": "data_insufficient", col: value}
      for col, value in _EXIT.items()),
    *({"state": "expired", "result_code": "no_fill", col: value}
      for col, value in _EXIT.items()),
    *({"state": "pending_entry", col: value} for col, value in _EXIT.items()),
    *({"state": "filled", **_FILL, col: value} for col, value in _EXIT.items()),
    {"state": "expired", "result_code": "no_fill", **_FILL},
    {"state": "expired", "result_code": "data_insufficient"},
    {"state": "expired"},
    {"state": "open"},
    {"spread_source": "mid"},
    {"spread_pips": None},
    {"pip_size": None},
    {"decision": "accept", "actor": "system", "reject_code": None},
    {"decision": "accept", "actor": "llm", "reject_code": "news_shock"},
    {"decision": "reject", "actor": "llm", "reject_code": None},
    {"decision": "reject", "actor": "system", "reject_code": "news_shock"},
])
def test_signal_shadow_rejects_other_shapes(decided, overrides):
    conn, mid, _, _ = decided
    with pytest.raises(sqlite3.IntegrityError):
        _shadow(conn, mid, **overrides)


def test_signal_shadow_is_unique_per_decision_and_signal(decided):
    conn, mid, sid, _ = decided
    did = _decision(conn, mission_id=mid, signal_id=sid, decision="reject",
                    actor="system", reject_code="econ_window", intent_id=None)
    values = {**_SHADOW_BASE}
    cols = ",".join(values)
    marks = ",".join("?" * len(values))
    sql = (f"INSERT INTO signal_shadow (decision_id,signal_id,{cols},"
           f"created_at,updated_at) VALUES (?,?,{marks},?,?)")
    conn.execute(sql, (did, sid, *values.values(), NOW_UTC, NOW_UTC))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (did, sid, *values.values(), NOW_UTC, NOW_UTC))


@pytest.fixture
def slice_world(tmp_path):
    conn = _fresh(tmp_path)
    eid = conn.execute(
        "INSERT INTO econ_events (ts,country,name,importance) VALUES "
        "('2026-10-30T12:30:00+00:00','USD','CPI',3)").lastrowid
    gen = conn.execute(
        "INSERT INTO ohlcv_slice_generations (started_at) VALUES (?)",
        (NOW_UTC,)).lastrowid
    conn.commit()
    return conn, eid, gen


_SET_BASE = {
    "shadow_id": None, "pair": "USDJPY",
    "range_start": "2026-10-30T12:15:00+00:00",
    "range_end": "2026-10-30T16:30:00+00:00", "expected_rows": 255,
    "state": "awaiting", "generation": None, "spread_pips": None,
    "spread_source": None, "completed_at": None, "purged_at": None,
    "rule_version": 1,
}


def _slice_set(conn, eid, **overrides):
    values = {"event_id": eid, **_SET_BASE, **overrides}
    cols = ",".join(values)
    marks = ",".join("?" * len(values))
    return conn.execute(
        f"INSERT INTO ohlcv_slice_sets ({cols},created_at) VALUES ({marks},?)",
        (*values.values(), NOW_UTC)).lastrowid


@pytest.mark.parametrize("overrides", [
    {},
    {"spread_pips": 1.2, "spread_source": "quote"},
    {"spread_pips": 1.0, "spread_source": "assumed"},
    {"state": "staging", "generation": "GEN"},
    {"state": "complete", "completed_at": NOW_UTC},
    {"state": "complete", "completed_at": NOW_UTC, "purged_at": NOW_UTC},
    {"state": "unrecoverable", "completed_at": NOW_UTC, "purged_at": NOW_UTC},
    {"state": "cancelled", "completed_at": NOW_UTC},
])
def test_slice_sets_accept_the_state_shapes(slice_world, overrides):
    conn, eid, gen = slice_world
    overrides = {k: (gen if v == "GEN" else v) for k, v in overrides.items()}
    _slice_set(conn, eid, **overrides)


@pytest.mark.parametrize("overrides", [
    {"event_id": None},
    {"state": "staging"},
    {"state": "awaiting", "generation": "GEN"},
    {"state": "complete", "generation": "GEN", "completed_at": NOW_UTC},
    {"state": "complete"},
    {"state": "awaiting", "completed_at": NOW_UTC},
    {"state": "cancelled", "completed_at": NOW_UTC, "purged_at": NOW_UTC},
    {"state": "awaiting", "purged_at": NOW_UTC},
    {"spread_pips": 1.0},
    {"spread_source": "quote"},
    {"spread_pips": 1.0, "spread_source": "mid"},
    {"state": "done"},
    {"generation": 9999, "state": "staging"},
])
def test_slice_sets_reject_other_shapes(slice_world, overrides):
    conn, eid, gen = slice_world
    overrides = {k: (gen if v == "GEN" else v) for k, v in overrides.items()}
    with pytest.raises(sqlite3.IntegrityError):
        _slice_set(conn, eid, **overrides)


def test_slice_sets_are_unique_per_event_pair_and_per_shadow(slice_world, decided):
    conn, eid, _ = slice_world
    _slice_set(conn, eid)
    _slice_set(conn, eid, pair="EURUSD")
    with pytest.raises(sqlite3.IntegrityError):
        _slice_set(conn, eid)
    mid = _mission(conn)
    _shadow(conn, mid)
    shadow_id = conn.execute("SELECT MAX(id) FROM signal_shadow").fetchone()[0]
    _slice_set(conn, None, shadow_id=shadow_id)
    with pytest.raises(sqlite3.IntegrityError):
        _slice_set(conn, None, shadow_id=shadow_id)
    with pytest.raises(sqlite3.IntegrityError):
        _slice_set(conn, None, shadow_id=shadow_id, pair="EURUSD")
    with pytest.raises(sqlite3.IntegrityError):
        _slice_set(conn, None, shadow_id=shadow_id + 1000)
    with pytest.raises(sqlite3.IntegrityError):
        _slice_set(conn, eid, shadow_id=shadow_id, pair="GBPUSD")


def test_shadow_slice_sets_do_not_hold_a_spread(slice_world):
    conn, _, _ = slice_world
    mid = _mission(conn)
    _shadow(conn, mid)
    shadow_id = conn.execute("SELECT MAX(id) FROM signal_shadow").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        _slice_set(conn, None, shadow_id=shadow_id, spread_pips=1.0,
                   spread_source="quote")


def test_slice_generation_ids_are_not_reused(tmp_path):
    conn = _fresh(tmp_path)
    first = conn.execute("INSERT INTO ohlcv_slice_generations (started_at) "
                         "VALUES (?)", (NOW_UTC,)).lastrowid
    conn.execute("DELETE FROM ohlcv_slice_generations")
    second = conn.execute("INSERT INTO ohlcv_slice_generations (started_at) "
                          "VALUES (?)", (NOW_UTC,)).lastrowid
    assert second == first + 1


def _bar(conn, set_id, *, bar_time="2026-10-30T12:15:00+00:00",
         quality="observed", ohlcv=(150.0, 150.2, 149.9, 150.1, 12.0)):
    conn.execute(
        "INSERT INTO ohlcv_slices (set_id,bar_time,open,high,low,close,volume,"
        "source,quality) VALUES (?,?,?,?,?,?,?,'mt5',?)",
        (set_id, bar_time, *ohlcv, quality))


@pytest.mark.parametrize("quality, ohlcv", [
    ("observed", (150.0, 150.2, 149.9, 150.1, 12.0)),
    ("observed", (150.0, 150.0, 150.0, 150.0, 0.0)),
    ("observed", (149.9, 150.2, 149.9, 150.2, 1.0)),
    ("no_tick_flat", (150.1, 150.1, 150.1, 150.1, 0.0)),
    ("datafeed_outage_gap", (None, None, None, None, None)),
])
def test_slices_accept_valid_bars(slice_world, quality, ohlcv):
    conn, eid, _ = slice_world
    _bar(conn, _slice_set(conn, eid), quality=quality, ohlcv=ohlcv)


@pytest.mark.parametrize("quality, ohlcv", [
    ("observed", (math.nan, 150.2, 149.9, 150.1, 1.0)),
    ("observed", (150.0, math.inf, 149.9, 150.1, 1.0)),
    ("observed", (150.0, 150.2, -math.inf, 150.1, 1.0)),
    ("observed", (150.0, 150.2, 149.9, 150.1, math.inf)),
    ("observed", (150.0, 150.2, 149.9, 150.1, math.nan)),
    ("observed", (0.0, 0.0, 0.0, 0.0, 0.0)),
    ("observed", (-1.0, -0.5, -1.5, -1.0, 0.0)),
    ("observed", (150.0, 149.9, 150.2, 150.1, 1.0)),
    ("observed", (150.3, 150.2, 149.9, 150.1, 1.0)),
    ("observed", (149.8, 150.2, 149.9, 150.1, 1.0)),
    ("observed", (150.0, 150.2, 149.9, 150.3, 1.0)),
    ("observed", (150.0, 150.2, 149.9, 149.8, 1.0)),
    ("observed", (150.0, 150.2, 149.9, 150.1, -1.0)),
    ("observed", (None, 150.2, 149.9, 150.1, 1.0)),
    ("no_tick_flat", (150.0, 150.2, 149.9, 150.1, 0.0)),
    ("no_tick_flat", (150.1, 150.1, 150.1, 150.1, 3.0)),
    ("no_tick_flat", (math.nan, math.nan, math.nan, math.nan, 0.0)),
    ("datafeed_outage_gap", (150.0, 150.2, 149.9, 150.1, 1.0)),
    ("datafeed_outage_gap", (None, None, None, None, 0.0)),
    ("missing", (150.0, 150.2, 149.9, 150.1, 1.0)),
])
def test_slices_reject_non_finite_non_positive_or_inverted_bars(
        slice_world, quality, ohlcv):
    conn, eid, _ = slice_world
    set_id = _slice_set(conn, eid)
    with pytest.raises(sqlite3.IntegrityError):
        _bar(conn, set_id, quality=quality, ohlcv=ohlcv)


def test_slices_are_unique_per_set_minute_and_need_a_set(slice_world):
    conn, eid, _ = slice_world
    set_id = _slice_set(conn, eid)
    _bar(conn, set_id)
    with pytest.raises(sqlite3.IntegrityError):
        _bar(conn, set_id)
    _bar(conn, set_id, bar_time="2026-10-30T12:16:00+00:00")
    with pytest.raises(sqlite3.IntegrityError):
        _bar(conn, 9999)


def test_reaction_keys_are_unique_per_event_currency(slice_world):
    conn, eid, _ = slice_world
    set_id = _slice_set(conn, eid)
    sql = ("INSERT INTO econ_reaction_keys (event_id,currency,set_id,decided_at)"
           " VALUES (?,?,?,?)")
    conn.execute(sql, (eid, "USD", set_id, NOW_UTC))
    conn.execute(sql, (eid, "JPY", set_id, NOW_UTC))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (eid, "USD", set_id, NOW_UTC))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (eid, "EUR", 9999, NOW_UTC))


_RESOLVED = (12.0, -3.0, 3.0, 2.0)


@pytest.mark.parametrize("window, status, values, ok", [
    (15, "resolved", _RESOLVED, True),
    (60, "resolved", (12.0, 3.0, 3.0, -1.0), True),
    (240, "unresolvable", (None, None, None, None), True),
    (30, "resolved", _RESOLVED, False),
    (15, "resolved", (12.0, None, 3.0, 2.0), False),
    (15, "resolved", (12.0, -3.0, 3.0, None), False),
    (15, "unresolvable", _RESOLVED, False),
    (15, "unresolvable", (None, None, None, 2.0), False),
    (15, "pending", (None, None, None, None), False),
])
def test_reaction_values_hold_numbers_only_when_resolved(
        slice_world, window, status, values, ok):
    conn, eid, _ = slice_world
    set_id = _slice_set(conn, eid)
    sql = ("INSERT INTO econ_reaction_values (set_id,window_min,status,"
           "range_pips,close_change_pips,abs_close_change_pips,"
           "net_abs_close_change_pips,rule_version,computed_at) "
           "VALUES (?,?,?,?,?,?,?,1,?)")
    if ok:
        conn.execute(sql, (set_id, window, status, *values, NOW_UTC))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, (set_id, window, status, *values, NOW_UTC))
    else:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, (set_id, window, status, *values, NOW_UTC))


def test_econ_events_superseded_by_must_point_to_an_event(slice_world):
    conn, eid, _ = slice_world
    newer = conn.execute(
        "INSERT INTO econ_events (ts,country,name,importance) VALUES "
        "('2026-10-30T13:30:00+00:00','USD','CPI',3)").lastrowid
    conn.execute("UPDATE econ_events SET superseded_by=? WHERE id=?",
                 (newer, eid))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE econ_events SET superseded_by=9999 WHERE id=?",
                     (newer,))


# --- 旧 payload 未移行 fixture -----------------------------------------------

def _load_legacy_rows(conn):
    rows = json.loads(LEGACY_PAYLOADS.read_text(encoding="utf-8"))["rows"]
    mid = _mission(conn, "running")
    ids = {}
    for row in rows:
        claimed_by = mid if row["status"] == "claimed" else None
        cur = conn.execute(
            "INSERT INTO signals (plugin,content_hash,pair,timeframe,bar_ts,"
            "kind,payload_json,status,claimed_by_mission_id,claimed_at,"
            "requeue_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["plugin"], row["content_hash"], row["pair"], row["timeframe"],
             row["bar_ts"], row["kind"],
             json.dumps(row["payload"], ensure_ascii=False), row["status"],
             claimed_by, row["claimed_at"], row["requeue_count"],
             row["created_at"]))
        ids[row["key"]] = cur.lastrowid
    conn.commit()
    return rows, ids


def test_legacy_payload_fixture_covers_the_migration_inputs():
    rows = json.loads(LEGACY_PAYLOADS.read_text(encoding="utf-8"))["rows"]
    shapes = {(r["kind"], r["status"], r["expected_after_normalization"])
              for r in rows}
    assert shapes == {
        ("strategy", "pending", "normalize"),
        ("strategy", "pending", "abandon"),
        ("strategy", "claimed", "normalize"),
        ("signal", "pending", "unchanged"),
    }
    missing = next(r for r in rows
                   if r["expected_after_normalization"] == "abandon")
    assert "stop_loss" not in missing["payload"]
    limit = next(r for r in rows if r["status"] == "claimed")
    assert limit["payload"]["entry_type"] == "limit"
    assert limit["payload"]["limit_price"] is not None


def test_data_migration_normalizes_legacy_payload_rows_once(tmp_path):
    conn = _v5(tmp_path)
    rows, ids = _load_legacy_rows(conn)
    init_db(conn)
    expected = {
        "pending_normalizable": ("pending", "executable_v1", None),
        "pending_missing_stop_loss": ("abandoned", None, "normalization_failed"),
        "claimed_limit_normalizable": ("claimed", "executable_v1", None),
        "pending_reference_signal": ("pending", None, None),
    }
    for row in rows:
        stored = _row(conn, ids[row["key"]])
        assert (stored["status"], stored["contract_version"],
                stored["abandon_reason"]) == expected[row["key"]]
    assert _row(conn, ids["pending_missing_stop_loss"])["abandoned_at"] is not None

    normal = conn.execute("SELECT payload_json FROM signals WHERE id=?", (
        ids["pending_normalizable"],)).fetchone()[0]
    assert normal == (
        '{"action": "open", "pair": "USDJPY", "direction": "long", '
        '"entry_type": "market", "horizon": "day", "stop_loss": 151.2, '
        '"confidence": 0.5, "reasoning": "breakout", "take_profit": 152.4, '
        '"live_source": "mt5", "source": "mt5", "interval": "15m", '
        '"bar_time": "2026-10-30T12:00:00+00:00", "bars_origin": "mt5:15m"}')
    before = {r["id"]: tuple(r) for r in conn.execute("SELECT * FROM signals")}
    init_db(conn)
    assert {r["id"]: tuple(r) for r in conn.execute("SELECT * FROM signals")} == before


# --- rebuild guard は構造で比べる -----------------------------------------

import hashlib  # noqa: E402

V5_FIXTURE_SHA256 = (
    "e08f8af0cf2862f030b52d442356f4b9a23474b3feab5d44983683760b50b7a0")


def test_v5_fixture_hash_is_pinned_and_changes_only_with_a_deliberate_update():
    """fixture は回避帯追加前の schema dump そのもの。内容を変えるときは、
    その意図をこの hash の更新で示す。"""
    digest = hashlib.sha256(V5_SCHEMA.read_bytes()).hexdigest()
    assert digest == V5_FIXTURE_SHA256


def _guard_conn(tmp_path, template, table):
    conn = sqlite3.connect(tmp_path / f"guard-{table}.db")
    conn.execute(template.format(name=table, ine=""))
    conn.commit()
    return conn


def _with_comment_and_spacing(ddl):
    return ddl.replace(
        "CREATE TABLE", "-- 先頭コメント\nCREATE TABLE", 1).replace(
        "  id INTEGER", "  /* 列の説明 */  id   INTEGER", 1).replace(
        ",\n", ", -- 行末コメント\n", 3)


GUARD_TARGETS = (
    ("signals", "_SIGNALS_V3_DDL"),
    ("trade_intents", "_TRADE_INTENTS_DDL"),
)


@pytest.mark.parametrize("table,const", GUARD_TARGETS)
def test_guard_ignores_comment_and_whitespace_only_ddl_changes(
        tmp_path, table, const):
    template = getattr(db_module, const)
    conn = _guard_conn(tmp_path, template, table)
    reworded = _with_comment_and_spacing(template)
    assert reworded != template
    assert db_module._table_definition_matches(conn, table, template)
    assert db_module._table_definition_matches(conn, table, reworded)


def _semantic_variants(const, template):
    if const == "_SIGNALS_V3_DDL":
        return {
            "column": template.replace(
                "requeue_count INTEGER NOT NULL DEFAULT 0",
                "requeue_count INTEGER NOT NULL DEFAULT 1", 1),
            "check": template.replace("'ineligible_cleanup',", "", 1),
            "fk": template.replace(
                "REFERENCES missions(id)", "REFERENCES missions(id) "
                "ON DELETE CASCADE", 1),
        }
    return {
        "column": template.replace(
            "payload_json TEXT NOT NULL", "payload_json TEXT", 1),
        "check": template.replace("'econ_window'", "'other_window'"),
        "fk": template.replace(
            "REFERENCES missions(id)", "REFERENCES missions(id) "
            "ON DELETE CASCADE", 1),
    }


@pytest.mark.parametrize("kind", ("column", "check", "fk"))
@pytest.mark.parametrize("table,const", GUARD_TARGETS)
def test_guard_detects_a_meaning_change_in_column_check_or_fk(
        tmp_path, table, const, kind):
    template = getattr(db_module, const)
    conn = _guard_conn(tmp_path, template, table)
    changed = _semantic_variants(const, template)[kind]
    assert changed != template
    assert not db_module._table_definition_matches(conn, table, changed)


def test_comment_only_ddl_change_does_not_rebuild_signals(tmp_path, monkeypatch):
    conn = _fresh(tmp_path)
    sid = _raw_signal(conn, content_hash="keep")
    monkeypatch.setattr(
        db_module, "_SIGNALS_V3_DDL",
        _with_comment_and_spacing(db_module._SIGNALS_V3_DDL))

    init_db(conn)

    assert not (tmp_path / "fresh.db.bak-signals-v3").exists()
    assert _row(conn, sid)["status"] == "pending"


def test_comment_only_ddl_change_does_not_rebuild_trade_intents(
        tmp_path, monkeypatch):
    conn = _fresh(tmp_path)
    monkeypatch.setattr(
        db_module, "_TRADE_INTENTS_DDL",
        _with_comment_and_spacing(db_module._TRADE_INTENTS_DDL))

    init_db(conn)

    assert not list(tmp_path.glob("fresh.db.bak-trade-intents*"))


def test_check_change_in_the_template_rebuilds_signals(tmp_path, monkeypatch):
    conn = _fresh(tmp_path)
    sid = _raw_signal(conn, content_hash="keep")
    monkeypatch.setattr(
        db_module, "_SIGNALS_V3_DDL",
        db_module._SIGNALS_V3_DDL.replace("'ineligible_cleanup',", "", 1))

    init_db(conn)

    assert (tmp_path / "fresh.db.bak-signals-v3").exists()
    assert "'ineligible_cleanup'" not in _table_sql(conn, "signals")
    assert _row(conn, sid)["status"] == "pending"


def test_check_change_in_the_template_rebuilds_trade_intents(
        tmp_path, monkeypatch):
    conn = _fresh(tmp_path)
    monkeypatch.setattr(
        db_module, "_TRADE_INTENTS_DDL",
        db_module._TRADE_INTENTS_DDL.replace("'econ_window'", "'other_window'"))

    init_db(conn)

    assert list(tmp_path.glob("fresh.db.bak-trade-intents*"))
    assert "other_window" in _table_sql(conn, "trade_intents")


# --- legacy_alter_table の復元 / v3 rebuild の途中失敗 ----------------------

@pytest.mark.parametrize("initial", (1, 0))
def test_signals_fk_migration_restores_legacy_alter_table(tmp_path, initial):
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_pre_fk_signals_ddl())
    conn.commit()
    conn.executescript(db_module._SCHEMA)
    _raw_signal(conn, content_hash="legacy")
    conn.execute(f"PRAGMA legacy_alter_table={initial}")

    db_module._migrate_signals_fk(conn)

    assert conn.execute("PRAGMA legacy_alter_table").fetchone()[0] == initial
    _assert_children_reference_signals(conn)


class _FailingConn:
    """`fail_on` で始まる SQL を実行しようとした時に例外を投げる接続の薄い皮。"""

    def __init__(self, conn, fail_on):
        self._conn = conn
        self._fail_on = fail_on

    def execute(self, sql, *args):
        if sql.strip().startswith(self._fail_on):
            raise RuntimeError("injected failure")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _rollback_world(tmp_path):
    conn = _v5(tmp_path)
    mid = _mission(conn)
    keep = _raw_signal(conn, content_hash="keep")
    gone = _raw_signal(conn, content_hash="gone")
    conn.execute("DELETE FROM signals WHERE id=?", (gone,))
    conn.commit()
    conn.executescript(db_module._SCHEMA)
    _link(conn, _deploy_op(conn), keep)
    return conn, mid, keep, gone


def _state(conn):
    seq = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='signals'").fetchone()
    return (_schema_dump(conn), _all_rows(conn, "signals"),
            seq[0] if seq else None,
            {c: _shape(conn, c) for c in CHILDREN_OF_SIGNALS
             if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?",
                             (c,)).fetchone()})


@pytest.mark.parametrize("fail_on", (
    "DROP TABLE signals",
    "ALTER TABLE signals_new RENAME",
), ids=("after_copy_before_drop", "before_rename"))
def test_signals_v3_failure_midway_rolls_back_table_children_and_sequence(
        tmp_path, fail_on):
    conn, _mid, _keep, gone = _rollback_world(tmp_path)
    before = _state(conn)
    assert before[2] == gone

    with pytest.raises(RuntimeError, match="injected failure"):
        db_module._migrate_signals_v3(_FailingConn(conn, fail_on))

    assert _state(conn) == before
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not conn.in_transaction
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='signals_new'"
                        ).fetchone() is None
    _assert_children_reference_signals(conn)


# --- v3 rebuild の FK 検査範囲 ----------------------------------------------

def _orders_row(conn, intent_id):
    conn.execute(
        "INSERT INTO orders (intent_id,pair,direction,entry_type,horizon,"
        "status,created_at,updated_at) VALUES (?,'USDJPY','long','market',"
        "'day','filled',?,?)", (intent_id, NOW_UTC, NOW_UTC))
    conn.commit()


def test_v3_rebuild_ignores_fk_violations_in_unrelated_tables(
        tmp_path, caplog):
    conn, _mid, keep, _gone = _rollback_world(tmp_path)
    conn.execute("PRAGMA foreign_keys=OFF")
    _orders_row(conn, 9999)
    conn.execute("PRAGMA foreign_keys=ON")

    with caplog.at_level("WARNING", logger="agentic_fx.store.db"):
        db_module._migrate_signals_v3(conn)

    assert "abandon_reason" in {r[1] for r in conn.execute(
        "PRAGMA table_info(signals)")}
    assert _row(conn, keep)["status"] == "pending"
    assert any("orders" in r.getMessage() and "FK" in r.getMessage()
               for r in caplog.records)
    for child in CHILDREN_OF_SIGNALS:
        assert "signals" in {fk["table"] for fk in conn.execute(
            f"PRAGMA foreign_key_list({child})")}, child


def test_v3_rebuild_still_fails_on_violations_in_the_signals_child_tables(
        tmp_path):
    conn, _mid, _keep, gone = _rollback_world(tmp_path)
    conn.execute("PRAGMA foreign_keys=OFF")
    _link(conn, _deploy_op(conn, "other"), gone)
    conn.execute("PRAGMA foreign_keys=ON")
    before = _state(conn)

    with pytest.raises(RuntimeError, match="FK 違反"):
        db_module._migrate_signals_v3(conn)

    assert _state(conn) == before


# --- CHECK 式の正規化 -------------------------------------------------------

_GUARD_TEMPLATE = (
    "CREATE TABLE {ine}{name} (a TEXT CHECK(a IN ('x y', 'z')), "
    "b INTEGER, CHECK (b > 0 AND a IS NOT NULL))")


def _synthetic_matches(tmp_path, variant):
    conn = sqlite3.connect(":memory:")
    conn.execute(_GUARD_TEMPLATE.format(name="t", ine=""))
    try:
        return db_module._table_definition_matches(conn, "t", variant)
    finally:
        conn.close()


def test_guard_treats_whitespace_inside_a_string_literal_as_meaning(tmp_path):
    changed = _GUARD_TEMPLATE.replace("'x y'", "'x  y'")
    assert changed != _GUARD_TEMPLATE
    assert not _synthetic_matches(tmp_path, changed)
    assert not _synthetic_matches(tmp_path, _GUARD_TEMPLATE.replace(
        "'x y'", "'xy'"))
    assert not _synthetic_matches(tmp_path, _GUARD_TEMPLATE.replace(
        "'z'", "'Z'"))


@pytest.mark.parametrize("variant", [
    "CREATE TABLE {ine}{name} (a TEXT check(a in ('x y', 'z')), "
    "b INTEGER, check (b>0 and a is not null))",
    "CREATE TABLE {ine}{name} (a TEXT, b INTEGER, "
    "CHECK(  b > 0\n AND a IS NOT NULL), CHECK (a IN ('x y','z')))",
    "CREATE TABLE {ine}{name} (a TEXT, b INTEGER CHECK (b>0 AND a IS NOT NULL),"
    " check(a In ('x y' , 'z')))",
])
def test_guard_ignores_keyword_case_and_check_placement(tmp_path, variant):
    assert _synthetic_matches(tmp_path, variant)


def test_normalized_check_clauses_keep_literals_verbatim():
    clauses = db_module._normalized_check_clauses(
        "CREATE TABLE t (a TEXT CHECK(a IN ('a ''b'' (c)', \"Q q\")))")
    assert clauses == ["(A IN('a ''b'' (c)',\"Q q\"))"]


# --- signal_shadow と signal_decisions の結び -------------------------------

def test_signal_shadow_must_point_at_its_own_decision_signal(decided):
    conn, mid, sid, _ = decided
    other = _raw_signal(conn, content_hash="other", status="consumed")
    did = _decision(conn, mission_id=mid, signal_id=sid, decision="reject",
                    actor="system", reject_code="econ_window", intent_id=None)
    _decision(conn, mission_id=mid, signal_id=other, decision="reject",
              actor="system", reject_code="econ_window", intent_id=None)
    values = {**_SHADOW_BASE}
    cols = ",".join(values)
    marks = ",".join("?" * len(values))
    sql = (f"INSERT INTO signal_shadow (decision_id,signal_id,{cols},"
           f"created_at,updated_at) VALUES (?,?,{marks},?,?)")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(sql, (did, other, *values.values(), NOW_UTC, NOW_UTC))
    conn.execute(sql, (did, sid, *values.values(), NOW_UTC, NOW_UTC))


def test_signal_decisions_expose_a_unique_id_signal_pair(tmp_path):
    conn = _fresh(tmp_path)
    uniques = [tuple(c["name"] for c in conn.execute(
        f"PRAGMA index_info('{i['name']}')"))
        for i in conn.execute("PRAGMA index_list(signal_decisions)")
        if i["unique"]]
    assert ("id", "signal_id") in uniques
    fks = [(r["table"], r["from"], r["to"]) for r in conn.execute(
        "PRAGMA foreign_key_list(signal_shadow)")
        if r["table"] == "signal_decisions"]
    assert sorted(fks) == [("signal_decisions", "decision_id", "id"),
                           ("signal_decisions", "signal_id", "signal_id")]


# --- abandon_claimed の理由検証と naive 拒否 --------------------------------

@pytest.mark.parametrize("reason", db_module.SIGNAL_ABANDON_REASONS)
def test_abandon_claimed_accepts_every_vocabulary_reason(tmp_path, reason):
    conn = _store_conn(tmp_path)
    sid, _ = _claimed(conn, content_hash="x")
    signals.abandon_claimed(conn, sid, now=NOW, reason=reason)
    assert _row(conn, sid)["abandon_reason"] == reason


@pytest.mark.parametrize("reason", ["normalisation_failed", "", "Source_Inactive"])
def test_abandon_claimed_rejects_unknown_reason_before_writing(tmp_path, reason):
    conn = _store_conn(tmp_path)
    sid, _ = _claimed(conn, content_hash="x")
    with pytest.raises(ValueError, match="abandon reason"):
        signals.abandon_claimed(conn, sid, now=NOW, reason=reason)
    assert _row(conn, sid)["status"] == "claimed"


def test_requeue_rejects_naive_time_without_writing(tmp_path):
    conn = _store_conn(tmp_path)
    sid, _ = _claimed(conn, content_hash="x")
    with pytest.raises(ValueError, match="naive"):
        signals.requeue(conn, sid, now=NOW.replace(tzinfo=None), max_requeue=2)
    assert _row(conn, sid)["status"] == "claimed"


def test_every_source_caller_of_requeue_and_abandon_passes_now():
    import ast
    src = Path(db_module.__file__).resolve().parents[1]
    found = []
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "signals"
                    and node.func.attr in ("requeue", "abandon_claimed")):
                found.append(path.name)
                kw = {k.arg: k.value for k in node.keywords}
                assert "now" in kw, (path.name, node.lineno)
                assert not (isinstance(kw["now"], ast.Call)
                            and ast.unparse(kw["now"]).startswith(
                                "datetime.utcnow")), (path.name, node.lineno)
    assert found.count("trade_loop.py") == 4


@pytest.mark.parametrize("abandon_if_stale", [False, True])
def test_trade_loop_requeue_signal_uses_the_aware_clock(
        tmp_path, abandon_if_stale):
    conn, loop, _, sid, loop_now = _loop_world(tmp_path, [])
    mid = missions.start(conn, "trade", "local", "m", loop_now)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=loop_now,
                                   freshness_bars=None)
    if abandon_if_stale:
        _jump(loop, loop_now + timedelta(hours=5))

    msg, terminal = loop._requeue_signal(
        claimed, abandon_if_stale=abandon_if_stale)

    assert (msg is not None) == abandon_if_stale
    assert (terminal == "freshness_abandoned") == abandon_if_stale
    row = _row(conn, sid)
    assert row["status"] == ("abandoned" if abandon_if_stale else "pending")
    assert row["requeue_count"] == (0 if abandon_if_stale else 1)


# --- requeue / abandon の理由規則は 1 か所 ----------------------------------

@pytest.mark.parametrize("requeue_count, linked, expected", [
    (0, False, ("pending", None)),
    (1, False, ("pending", None)),
    (2, False, ("abandoned", "retry_exhausted")),
    (3, False, ("abandoned", "retry_exhausted")),
    (0, True, ("abandoned", "deploy_linked")),
    (2, True, ("abandoned", "deploy_linked")),
])
def test_requeue_outcome_is_the_rule_the_sql_paths_follow(
        tmp_path, requeue_count, linked, expected):
    assert signals.requeue_outcome(
        requeue_count=requeue_count, max_requeue=2,
        deploy_linked=linked) == expected
    conn = _store_conn(tmp_path)
    sid, _ = _claimed(conn, content_hash="x", requeue_count=requeue_count)
    if linked:
        _link(conn, _deploy_op(conn), sid, "claimed")
    signals.requeue(conn, sid, now=NOW, max_requeue=2)
    row = _row(conn, sid)
    assert (row["status"], row["abandon_reason"]) == expected


def test_missions_holds_no_copy_of_the_requeue_reason_rule():
    import ast
    path = Path(missions.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys = {id(n.slice) for n in ast.walk(tree)
            if isinstance(n, ast.Subscript)}
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in keys}
    assert not {"deploy_linked", "retry_exhausted"} & literals
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                if isinstance(n, ast.ImportFrom) and n.module and \
                        n.module.endswith("store.signals"):
                    raise AssertionError(f"function-local import in {fn.name}")


# --- ダミー data migration の機構 -------------------------------------------

def test_registered_data_migration_runs_once_across_inits(tmp_path, monkeypatch):
    calls = []

    def probe(c):
        calls.append(c.in_transaction)

    monkeypatch.setattr(db_module, "_signal_data_migrations",
                        lambda: (("probe_once", probe),))
    conn = _fresh(tmp_path)
    init_db(conn)
    init_db(conn)

    assert calls == [True]
    assert conn.execute("SELECT COUNT(*) FROM data_migrations "
                        "WHERE name='probe_once'").fetchone()[0] == 1
    assert not conn.in_transaction


# --- slice set の値域 CHECK -------------------------------------------------

@pytest.mark.parametrize("overrides", [
    {"expected_rows": -1},
    {"range_end": "2026-10-30T12:15:00+00:00"},
    {"range_start": "2026-10-30T16:30:00+00:00",
     "range_end": "2026-10-30T12:15:00+00:00"},
])
def test_slice_sets_reject_negative_rows_and_empty_or_inverted_ranges(
        slice_world, overrides):
    conn, eid, _ = slice_world
    with pytest.raises(sqlite3.IntegrityError):
        _slice_set(conn, eid, **overrides)


def test_slice_sets_accept_zero_expected_rows(slice_world):
    conn, eid, _ = slice_world
    _slice_set(conn, eid, expected_rows=0)


# --- abandoned_at の期間 query は datetime() で正規化する ---------------------

JST = timezone(timedelta(hours=9))


def _abandoned_in_jst(conn, content_hash, created_jst, abandoned_utc):
    sid = _raw_signal(conn, content_hash=content_hash, status="claimed",
                      claimed_by=_mission(conn, "running"),
                      claimed_at=created_jst.isoformat())
    conn.execute("UPDATE signals SET created_at=? WHERE id=?",
                 (created_jst.isoformat(), sid))
    signals.abandon_claimed(conn, sid, now=abandoned_utc,
                            reason="source_inactive")
    return sid


def test_abandon_lag_compares_across_offsets(tmp_path):
    conn = _store_conn(tmp_path)
    created = datetime(2026, 10, 30, 22, 0, tzinfo=JST)  # 13:00Z
    sid = _abandoned_in_jst(conn, "a", created, NOW + timedelta(minutes=30))
    # 生の文字列比較では created_at ('...T22:00:00+09:00') の方が後に見える
    assert _row(conn, sid)["abandoned_at"] < created.isoformat()
    assert signals.abandon_lag_seconds(conn, sid) == 1800
    pending = _raw_signal(conn, content_hash="p")
    assert signals.abandon_lag_seconds(conn, pending) is None


def test_abandoned_counts_group_a_period_regardless_of_clock_offset(tmp_path):
    conn = _store_conn(tmp_path)
    created = datetime(2026, 10, 30, 22, 0, tzinfo=JST)
    _abandoned_in_jst(conn, "in1", created, NOW + timedelta(minutes=30))
    _abandoned_in_jst(conn, "in2", created, NOW + timedelta(minutes=45))
    _abandoned_in_jst(conn, "out", created, NOW + timedelta(hours=2))
    since = datetime(2026, 10, 30, 22, 0, tzinfo=JST)   # 13:00Z
    until = datetime(2026, 10, 30, 23, 0, tzinfo=JST)   # 14:00Z

    assert signals.abandoned_counts(conn, since=since, until=until) == {
        "source_inactive": 2}
    with pytest.raises(ValueError, match="naive"):
        signals.abandoned_counts(conn, since=since.replace(tzinfo=None),
                                 until=until)


# --- guard は UNIQUE index だけを比べる / 規則表は 1 か所 ---------------------

@pytest.mark.parametrize("table,const", GUARD_TARGETS)
def test_guard_ignores_a_performance_only_index(tmp_path, table, const):
    template = getattr(db_module, const)
    conn = _guard_conn(tmp_path, template, table)
    first_col = conn.execute(f"PRAGMA table_info('{table}')").fetchall()[1][1]
    conn.execute(f"CREATE INDEX perf_only_idx ON {table}({first_col})")
    assert db_module._table_definition_matches(conn, table, template)


def test_guard_detects_a_unique_constraint_change(tmp_path):
    base = "CREATE TABLE {ine}{name} (a INTEGER, b INTEGER, UNIQUE(a))"
    conn = sqlite3.connect(tmp_path / "u.db")
    conn.execute(base.format(name="t", ine=""))
    assert db_module._table_definition_matches(conn, "t", base)
    assert not db_module._table_definition_matches(
        conn, "t", base.replace("UNIQUE(a)", "UNIQUE(a, b)"))
    assert not db_module._table_definition_matches(
        conn, "t", base.replace(", UNIQUE(a)", ""))


def test_requeue_rule_table_drives_sql_and_python_identically():
    swapped = tuple(reversed(signals._REQUEUE_RULES))
    sql = signals._requeue_set_sql(swapped)
    assert sql != signals._REQUEUE_SET
    reason = sql.split("abandon_reason = ")[1]
    assert reason.index("retry_exhausted") < reason.index("deploy_linked")
    assert signals.requeue_outcome(
        requeue_count=5, max_requeue=2, deploy_linked=True,
        rules=swapped) == ("abandoned", "retry_exhausted")
    assert signals.requeue_outcome(
        requeue_count=5, max_requeue=2, deploy_linked=True) == (
        "abandoned", "deploy_linked")


def test_requeue_precedence_literals_exist_only_in_the_rule_table():
    import ast
    tree = ast.parse(Path(signals.__file__).read_text(encoding="utf-8"))
    hits = [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and ("requeue_count >= " in n.value
                 or "WHEN requeue_count >=" in n.value)]
    assert len(hits) == 1


def test_migration_abandon_of_a_claimed_unnormalizable_row_clears_the_claim(tmp_path):
    conn = _v5(tmp_path)
    mid = _mission(conn, "running")
    sid = _raw_signal(conn, content_hash="bad", status="claimed", claimed_by=mid,
                      claimed_at="2026-10-30T12:46:00+00:00")
    init_db(conn)
    row = conn.execute(
        "SELECT status, abandon_reason, abandoned_at, claimed_by_mission_id, "
        "claimed_at FROM signals WHERE id=?", (sid,)).fetchone()
    assert (row[0], row[1]) == ("abandoned", "normalization_failed")
    assert row[2] is not None and row[3] is None and row[4] is None


def test_data_migration_limit_row_payload_bytes_include_limit_fields(tmp_path):
    conn = _v5(tmp_path)
    rows, ids = _load_legacy_rows(conn)
    init_db(conn)
    limit = conn.execute("SELECT payload_json FROM signals WHERE id=?", (
        ids["claimed_limit_normalizable"],)).fetchone()[0]
    assert limit == (
        '{"action": "open", "pair": "USDJPY", "direction": "long", '
        '"entry_type": "limit", "horizon": "day", "stop_loss": 150.6, '
        '"confidence": 0.5, "reasoning": "pullback", "limit_price": 151.05, '
        '"expires_in": "4h", "live_source": "mt5", "source": "mt5", '
        '"interval": "15m", "bar_time": "2026-10-30T12:30:00+00:00", '
        '"bars_origin": "mt5:15m"}')


# --- migration は live と同じ公開検証を通す ---------------------------------

_VALID_LEGACY = {
    "action": "open", "rationale": "r", "direction": "long",
    "entry_type": "market", "limit_price": None, "stop_loss": 151.2,
    "take_profit": None, "live_source": "mt5", "source": "mt5",
    "interval": "15m", "bar_time": "2026-10-30T12:00:00+00:00",
    "bars_origin": "mt5:15m",
}


@pytest.mark.parametrize("override", [
    {"action": "close"},
    {"direction": "sideways"},
    {"direction": None},
    {"stop_loss": None},
    {"entry_type": "limit", "limit_price": None},
    {"entry_type": "stop"},
    {"stop_loss": -1.0},
    {"stop_loss": "abc"},
    {"take_profit": -3.0},
    {"entry_type": "limit", "limit_price": "x"},
])
def test_migration_abandons_payloads_the_live_validation_rejects(tmp_path, override):
    conn = _v5(tmp_path)
    mid = _mission(conn, "running")
    sid = _raw_signal(conn, content_hash="bad", status="claimed", claimed_by=mid,
                      claimed_at="2026-10-30T12:46:00+00:00")
    conn.execute("UPDATE signals SET payload_json=? WHERE id=?",
                 (json.dumps({**_VALID_LEGACY, **override}), sid))
    conn.commit()
    init_db(conn)
    row = conn.execute(
        "SELECT status, abandon_reason, abandoned_at, claimed_by_mission_id, "
        "claimed_at, contract_version FROM signals WHERE id=?", (sid,)).fetchone()
    assert (row[0], row[1]) == ("abandoned", "normalization_failed")
    assert row[2] is not None and row[3] is None and row[4] is None
    assert row[5] is None


def test_normalize_strategy_result_rejects_unvalidated_shapes():
    from agentic_fx.plugin.strategy_adapter import normalize_strategy_result
    base = {"action": "open", "rationale": "r", "direction": "long",
            "entry_type": "market", "limit_price": None, "stop_loss": 1.0,
            "take_profit": None}
    assert normalize_strategy_result(base, "USDJPY")["action"] == "open"
    for override in ({"action": "close"}, {"direction": None},
                     {"stop_loss": None}, {"stop_loss": -1.0},
                     {"entry_type": "limit", "limit_price": None}):
        with pytest.raises(ValueError):
            normalize_strategy_result({**base, **override}, "USDJPY")


def _abandoned_row(conn, *, plugin, reason, kind="strategy", contract="executable_v1",
                   at="2026-10-30T12:00:00+00:00", h):
    conn.execute(
        "INSERT INTO signals (plugin,content_hash,pair,timeframe,bar_ts,kind,"
        "payload_json,status,abandon_reason,abandoned_at,contract_version,"
        "created_at) VALUES (?,?,'USDJPY','15m',?,?,'{}','abandoned',?,?,?,?)",
        (plugin, h, at, kind, reason, at, contract, NOW_UTC))
    conn.commit()


def test_one_classification_branch_drives_all_abandoned_aggregates(tmp_path):
    conn = connect(tmp_path / "mixed.db")
    init_db(conn)
    _abandoned_row(conn, plugin="a", reason="ineligible_cleanup", kind="signal",
                   contract=None, h="1")
    _abandoned_row(conn, plugin="a", reason="normalization_failed",
                   contract=None, h="2")
    _abandoned_row(conn, plugin="a", reason="freshness_expired", h="3")
    _abandoned_row(conn, plugin="b", reason="normalization_failed",
                   contract=None, h="4")
    _abandoned_row(conn, plugin="b", reason="legacy_unknown", contract=None,
                   h="5")
    since, until = NOW - timedelta(days=2), NOW + timedelta(days=1)
    assert signals.abandoned_counts(conn, since=since, until=until) == {
        "normalization_failed": 2, "freshness_expired": 1}
    assert signals.abandoned_counts_by_strategy(conn, since=since, until=until) == {
        "a": {"freshness_expired": 1, "normalization_failed": 1},
        "b": {"normalization_failed": 1}}
    report = signals.human_signal_aggregates(conn, since=since, until=until)
    assert report["cleanup"] == 1
    assert report["legacy_unknown_total"] == 1
    assert report["abandoned"] == {"normalization_failed": 2,
                                   "freshness_expired": 1}


# --- T1b 是正: add / consume / 理由規則 / 集計 / 移行 ----------------------

def test_add_never_stamps_a_contract_version_on_its_own(tmp_path):
    conn = _fresh(tmp_path)
    sid = signals.add(
        conn, plugin="p", content_hash="h", pair="USDJPY", timeframe="1h",
        bar_ts=NOW_UTC, kind="strategy", payload={"x": 1}, now=NOW)
    assert _row(conn, sid)["contract_version"] is None
    explicit = signals.add(
        conn, plugin="p", content_hash="h2", pair="USDJPY", timeframe="1h",
        bar_ts=NOW_UTC, kind="strategy", payload={"x": 1}, now=NOW,
        contract_version="executable_v1")
    assert _row(conn, explicit)["contract_version"] == "executable_v1"


def _claimed_for_consume(conn):
    sid, mid = _claimed(conn, content_hash="c")
    return sid, mid


def test_consume_wrapper_keeps_other_uncommitted_work_when_the_cas_misses(tmp_path):
    conn = _store_conn(tmp_path)
    sid, mid = _claimed_for_consume(conn)
    conn.execute("INSERT INTO alert_state (key,value,updated_at) "
                 "VALUES ('k','v',?)", (NOW_UTC,))
    with pytest.raises(ValueError, match="not claimed"):
        signals.consume(conn, sid, mission_id=mid + 99, now=NOW)
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM alert_state").fetchone()[0] == 1


def test_consume_in_tx_leaves_the_commit_to_the_outer_transaction(tmp_path):
    conn = _store_conn(tmp_path)
    sid, mid = _claimed_for_consume(conn)
    conn.execute("BEGIN")
    conn.execute("INSERT INTO alert_state (key,value,updated_at) "
                 "VALUES ('k','v',?)", (NOW_UTC,))
    with pytest.raises(ValueError, match="not claimed"):
        signals.consume_in_tx(conn, sid, mission_id=mid + 99, now=NOW)
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM alert_state").fetchone()[0] == 0


_FRESHNESS_CASES = [
    # (kind, contract_version, requeue_count, expected)
    ("strategy", "executable_v1", 0, "freshness_expired"),
    ("strategy", "executable_v1", 2, "requeued_expired"),
    ("signal", None, 0, "ineligible_cleanup"),
    ("signal", None, 2, "ineligible_cleanup"),
    ("strategy", None, 1, "ineligible_cleanup"),
    ("weird", "executable_v1", 0, "ineligible_cleanup"),
]


@pytest.mark.parametrize("kind, version, requeues, expected", _FRESHNESS_CASES)
def test_both_freshness_paths_write_the_same_reason(
        tmp_path, kind, version, requeues, expected):
    conn = _store_conn(tmp_path)
    pending = _raw_signal(conn, content_hash="p", kind=kind,
                          requeue_count=requeues)
    claimed, _ = _claimed(conn, content_hash="c", requeue_count=requeues)
    for sid in (pending, claimed):
        conn.execute("UPDATE signals SET kind=?, contract_version=? WHERE id=?",
                     (kind, version, sid))
    conn.commit()
    row = dict(conn.execute("SELECT * FROM signals WHERE id=?", (claimed,)
                            ).fetchone())
    assert signals.freshness_abandon_reason(row) == expected

    signals.expire_stale(conn, now=NOW + timedelta(hours=3), freshness_bars=1)
    assert _row(conn, pending)["abandon_reason"] == expected


def test_no_pending_row_is_left_outside_claim_and_expiry(tmp_path):
    conn = _store_conn(tmp_path)
    odd = _raw_signal(conn, content_hash="odd", kind="weird")
    conn.execute("UPDATE signals SET contract_version='executable_v1' "
                 "WHERE id=?", (odd,))
    conn.commit()
    assert not signals.pending_exists(conn, now=NOW, freshness_bars=None)
    result = signals.expire_stale(conn, now=NOW + timedelta(hours=3),
                                  freshness_bars=1)
    assert result.total == 1
    assert _row(conn, odd)["abandon_reason"] == "ineligible_cleanup"
    assert conn.execute("SELECT COUNT(*) FROM signals WHERE status='pending'"
                        ).fetchone()[0] == 0


def test_aggregates_qualify_columns_and_keep_legacy_out_of_the_period(tmp_path):
    conn = connect(tmp_path / "agg.db")
    init_db(conn)
    _abandoned_row(conn, plugin="a", reason="freshness_expired", h="1")
    _abandoned_row(conn, plugin="a", reason="retry_exhausted", h="2")
    _abandoned_row(conn, plugin="b", reason="freshness_expired", h="3")
    _abandoned_row(conn, plugin="b", reason="legacy_unknown", contract=None,
                   h="4")
    since, until = NOW + timedelta(days=5), NOW + timedelta(days=6)
    statements = []
    conn.set_trace_callback(statements.append)
    report = signals.human_signal_aggregates(conn, since=since, until=until)
    conn.set_trace_callback(None)
    assert report["abandoned"] == {} and report["abandoned_by_strategy"] == {}
    assert "legacy_unknown" not in report
    assert report["legacy_unknown_total"] == 1

    since, until = NOW - timedelta(days=2), NOW + timedelta(days=1)
    statements.clear()
    conn.set_trace_callback(statements.append)
    report = signals.human_signal_aggregates(conn, since=since, until=until)
    conn.set_trace_callback(None)
    assert report["abandoned"] == {"freshness_expired": 2, "retry_exhausted": 1}
    assert sum(1 for s in statements if "GROUP BY plugin, abandon_reason" in s) == 1
    assert not any("GROUP BY abandon_reason" in s for s in statements)
    joined = [s for s in statements if "FROM signal_decisions JOIN signals" in s]
    assert joined and all("signals.status" in s and "signals.kind" in s
                          and "signals.contract_version" in s for s in joined)
    assert not any(" status=" in s.replace("signals.status", "")
                   for s in joined)


def _legacy_pending(conn, h, payload_json):
    cur = conn.execute(
        "INSERT INTO signals (plugin,content_hash,pair,timeframe,bar_ts,kind,"
        "payload_json,created_at) VALUES ('p',?,'USDJPY','15m',?,'strategy',?,?)",
        (h, NOW_UTC, payload_json, NOW_UTC))
    conn.commit()
    return cur.lastrowid


@pytest.mark.parametrize("payload_json", ['"text"', "[1, 2]", "null", "5"])
def test_non_object_legacy_payloads_are_abandoned_as_normalization_failed(
        tmp_path, payload_json):
    conn = _fresh(tmp_path)
    sid = _legacy_pending(conn, "h", payload_json)
    signals.normalize_legacy_strategy_payloads(conn, now=NOW)
    assert _row(conn, sid)["abandon_reason"] == "normalization_failed"


def test_an_unexpected_exception_abandons_only_that_row(tmp_path, monkeypatch):
    from agentic_fx.plugin import sandbox
    conn = _fresh(tmp_path)
    good = json.loads(LEGACY_PAYLOADS.read_text(encoding="utf-8"))["rows"]
    normalizable = next(r for r in good
                        if r["expected_after_normalization"] == "normalize")
    first = _legacy_pending(conn, "boom", json.dumps(normalizable["payload"]))
    second = _legacy_pending(conn, "ok", json.dumps(normalizable["payload"]))
    real = sandbox.validate_strategy_result
    calls = []

    def flaky(result):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("unexpected")
        return real(result)

    monkeypatch.setattr(sandbox, "validate_strategy_result", flaky)
    signals.normalize_legacy_strategy_payloads(conn, now=NOW)
    assert _row(conn, first)["abandon_reason"] == "normalization_failed"
    assert _row(conn, second)["contract_version"] == "executable_v1"


def test_the_status_constant_is_bound_not_interpolated():
    import inspect
    src = inspect.getsource(missions.signals_rate_ok)
    assert "{STATUS_SYSTEM_FILTERED}" not in src


def test_the_replay_adapter_keeps_one_name_for_the_intent_mapping():
    from agentic_fx.plugin import strategy_adapter
    assert not hasattr(strategy_adapter, "_strategy_result_to_intent")


# --- store の writer 契約と DDL の CHECK の一致 --------------------------------

@pytest.mark.parametrize("decision", ["accept", "reject", "hold"])
@pytest.mark.parametrize("actor", ["llm", "system", "human"])
@pytest.mark.parametrize("code", [None, "news_shock", "opposite_position",
                                  "econ_window", "other"])
@pytest.mark.parametrize("with_intent", [False, True])
def test_decision_shape_check_agrees_with_the_ddl(
        decided, decision, actor, code, with_intent):
    from agentic_fx.store import signal_decisions
    conn, mid, sid, iid = decided
    intent = iid if with_intent else None
    try:
        signal_decisions.validate_shape(decision=decision, actor=actor,
                                        reject_code=code,
                                        trade_intent_id=intent)
        python_ok = True
    except signal_decisions.DecisionShapeError:
        python_ok = False
    try:
        _decision(conn, mission_id=mid, signal_id=sid, decision=decision,
                  actor=actor, reject_code=code, intent_id=intent)
        db_ok = True
    except sqlite3.IntegrityError:
        db_ok = False
    conn.rollback()
    assert python_ok == db_ok


@pytest.mark.parametrize("quality", ["observed", "no_tick_flat",
                                     "datafeed_outage_gap"])
@pytest.mark.parametrize("o, h, l, c", [
    (2.0, 3.0, 1.0, 2.0), (2.0, 2.0, 2.0, 2.0), (3.0, 2.0, 1.0, 2.0),
    (2.0, 1.0, 3.0, 2.0), (2.0, 3.0, 1.0, 3.5), (0.0, 3.0, 0.0, 2.0),
    (-1.0, 3.0, -1.0, 2.0), (math.nan, 3.0, 1.0, 2.0), (2.0, math.inf, 1.0, 2.0),
    (None, None, None, None), (2.0, None, 1.0, 2.0)])
@pytest.mark.parametrize("volume", [None, 0.0, 5.0, -1.0, math.inf])
def test_slice_row_check_agrees_with_the_ddl(slice_world, quality, o, h, l, c,
                                             volume):
    from agentic_fx.store import ohlcv_slices
    conn, eid, _ = slice_world
    set_id = _slice_set(conn, eid)
    row = ohlcv_slices.SliceRow(
        datetime(2026, 10, 30, 12, 15, tzinfo=timezone.utc), o, h, l, c,
        volume, "mt5-live", quality)
    python_ok = ohlcv_slices.slice_row_problem(row) is None
    try:
        ohlcv_slices._insert_rows(conn, set_id, [row])
        db_ok = True
    except sqlite3.IntegrityError:
        db_ok = False
    conn.rollback()
    assert python_ok == db_ok
