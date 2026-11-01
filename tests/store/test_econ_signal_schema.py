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
            f"PRAGMA foreign_key_list({child})") if fk["from"] == "signal_id"}
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
    assert after == before
    for key, sid in ids.items():
        row = _row(conn, sid)
        assert row["contract_version"] is None
        if key.startswith("ab_"):
            assert (row["abandon_reason"], row["abandoned_at"]) == (
                "legacy_unknown", None), key
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
        "abandoned_at": None, "contract_version": None}


@pytest.mark.parametrize("reason", ["freshness_expired", "requeued_expired",
                                    "source_inactive"])
def test_abandon_claimed_records_the_given_reason_and_utc_time(tmp_path, reason):
    conn = _store_conn(tmp_path)
    sid, _ = _claimed(conn, content_hash="x")
    jst = NOW.astimezone(timezone(timedelta(hours=9)))

    signals.abandon_claimed(conn, sid, now=jst, reason=reason)

    assert _row(conn, sid) == {
        "status": "abandoned", "requeue_count": 0, "abandon_reason": reason,
        "abandoned_at": NOW_UTC, "contract_version": None}


def test_abandon_claimed_rejects_naive_time_without_writing(tmp_path):
    conn = _store_conn(tmp_path)
    sid, _ = _claimed(conn, content_hash="x")
    with pytest.raises(ValueError, match="naive"):
        signals.abandon_claimed(conn, sid, now=NOW.replace(tzinfo=None),
                                reason="source_inactive")
    assert _row(conn, sid)["status"] == "claimed"


def test_freshness_abandon_reason_follows_the_requeue_history():
    assert signals.freshness_abandon_reason({"requeue_count": 0}) == \
        "freshness_expired"
    assert signals.freshness_abandon_reason({"requeue_count": 1}) == \
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


# --- trade loop の呼出側 3 箇所 ---------------------------------------------

_OPEN = {"action": "open", "pair": "USDJPY", "direction": "long",
         "entry_type": "market", "horizon": "day", "stop_loss": 147.0,
         "take_profit": 149.0, "reasoning": "x"}


def _loop_world(tmp_path, results):
    from tests.loops.test_trade_loop import NOW as LOOP_NOW, _loop
    conn, loop, runner, _ = _loop(tmp_path, results)
    sid = signals.add(
        conn, plugin="strategy", content_hash="a" * 64, pair="USDJPY",
        timeframe="1h", bar_ts=LOOP_NOW.isoformat(), kind="strategy",
        payload={"direction": "long"}, now=LOOP_NOW)
    return conn, loop, runner, sid, LOOP_NOW


def _jump(loop, when):
    clock = FixedClock(when)
    loop.clock = clock
    loop.executor.clock = clock


def test_trade_loop_source_inactive_abandons_with_source_inactive(
        tmp_path, monkeypatch):
    conn, loop, _, sid, loop_now = _loop_world(
        tmp_path, [MissionResult("completed", _OPEN, [])])
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
            return MissionResult("completed", _OPEN, [])

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

    msg = loop._requeue_signal(claimed, abandon_if_stale=True)

    assert msg is not None and "鮮度切れ" in msg
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


def test_signal_data_migrations_are_empty_until_the_payload_rule_lands():
    assert db_module._signal_data_migrations() == ()


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


def test_v3_rebuild_leaves_legacy_payload_rows_unmigrated(tmp_path):
    conn = _v5(tmp_path)
    rows, ids = _load_legacy_rows(conn)
    columns = ",".join(V2_COLUMNS)
    before = {r["id"]: tuple(r) for r in conn.execute(
        f"SELECT {columns} FROM signals")}

    init_db(conn)

    after = {r["id"]: tuple(r) for r in conn.execute(
        f"SELECT {columns} FROM signals")}
    assert after == before
    for row in rows:
        stored = _row(conn, ids[row["key"]])
        assert stored["status"] == row["status"]
        assert stored["contract_version"] is None
        assert stored["abandon_reason"] is None


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
