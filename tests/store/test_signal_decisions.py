"""signal の採否 decision と、decision・shadow・consume・mission 終端を束ねる
確定 transaction。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from agentic_fx.store import signal_decisions as sd
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.signal_shadow import ShadowSpread

NOW = datetime(2026, 10, 30, 13, 0, 7, tzinfo=timezone.utc)
SPREAD = ShadowSpread(pip_size=0.01, spread_pips=1.2, source="quote")
INTENT = {"pair": "USDJPY", "action": "open", "direction": "long"}


def _conn(tmp_path):
    conn = connect(tmp_path / "d.db")
    init_db(conn)
    return conn


def _mission(conn, status="running"):
    mid = conn.execute(
        "INSERT INTO missions (loop,runner,model,status,started_at,trigger) "
        "VALUES ('trade','local','m',?,?,'signal')",
        (status, NOW.isoformat())).lastrowid
    conn.commit()
    return mid


def _claimed(conn, mid, *, content_hash="h"):
    sid = conn.execute(
        "INSERT INTO signals (plugin,content_hash,pair,timeframe,bar_ts,kind,"
        "payload_json,status,claimed_by_mission_id,claimed_at,created_at,"
        "contract_version) VALUES ('p',?,'USDJPY','15m',"
        "'2026-10-30T12:45:00+00:00','strategy','{}','claimed',?,?,?,"
        "'executable_v1')",
        (content_hash, mid, NOW.isoformat(), NOW.isoformat())).lastrowid
    conn.commit()
    return sid


def _world(tmp_path):
    conn = _conn(tmp_path)
    mid = _mission(conn)
    return conn, mid, _claimed(conn, mid)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _state(conn, mid, sid):
    return (conn.execute("SELECT status FROM missions WHERE id=?",
                         (mid,)).fetchone()[0],
            conn.execute("SELECT status FROM signals WHERE id=?",
                         (sid,)).fetchone()[0])


def _reject(conn, mid, sid, *, actor="system", code="econ_window"):
    return sd.commit_reject(conn, mission_id=mid, signal_id=sid, actor=actor,
                            reject_code=code, spread=SPREAD,
                            output={"decision": "reject"}, transcript=[],
                            now=NOW)


def _accept(conn, mid, sid):
    return sd.commit_accept(conn, mission_id=mid, signal_id=sid,
                            intent_payload=INTENT, spread=SPREAD,
                            output={"decision": "accept"}, transcript=[],
                            now=NOW)


def _assert_untouched(conn, mid, sid):
    assert not conn.in_transaction
    assert _count(conn, "signal_decisions") == 0
    assert _count(conn, "signal_shadow") == 0
    assert _count(conn, "trade_intents") == 0
    assert _state(conn, mid, sid) == ("running", "claimed")


# --- 確定 transaction の正常系 -----------------------------------------------

def test_system_reject_writes_decision_shadow_consume_and_system_filtered(tmp_path):
    conn, mid, sid = _world(tmp_path)
    result = _reject(conn, mid, sid)
    assert result.trade_intent_id is None
    assert result.mission_status == "system_filtered"
    dec = dict(conn.execute("SELECT * FROM signal_decisions").fetchone())
    assert (dec["id"], dec["mission_id"], dec["signal_id"], dec["decision"],
            dec["actor"], dec["reject_code"], dec["trade_intent_id"],
            dec["decided_at"]) == (
        result.decision_id, mid, sid, "reject", "system", "econ_window", None,
        "2026-10-30T13:00:07+00:00")
    sh = dict(conn.execute("SELECT * FROM signal_shadow").fetchone())
    assert (sh["id"], sh["decision_id"], sh["signal_id"], sh["decision"],
            sh["actor"], sh["reject_code"], sh["state"], sh["pip_size"],
            sh["spread_pips"], sh["spread_source"], sh["rule_version"]) == (
        result.shadow_id, result.decision_id, sid, "reject", "system",
        "econ_window", "pending_entry", 0.01, 1.2, "quote", 1)
    assert _state(conn, mid, sid) == ("system_filtered", "consumed")
    assert _count(conn, "trade_intents") == 0


@pytest.mark.parametrize("code", ["news_shock", "opposite_position"])
def test_llm_reject_completes_the_mission(tmp_path, code):
    conn, mid, sid = _world(tmp_path)
    result = _reject(conn, mid, sid, actor="llm", code=code)
    assert result.mission_status == "completed"
    assert _state(conn, mid, sid) == ("completed", "consumed")
    assert tuple(conn.execute(
        "SELECT actor, reject_code FROM signal_shadow").fetchone()) == ("llm", code)
    out = conn.execute("SELECT output_json FROM missions WHERE id=?",
                       (mid,)).fetchone()[0]
    assert json.loads(out) == {"decision": "reject"}


def test_accept_writes_intent_decision_shadow_consume_and_completed(tmp_path):
    conn, mid, sid = _world(tmp_path)
    result = _accept(conn, mid, sid)
    intent = dict(conn.execute("SELECT * FROM trade_intents").fetchone())
    assert intent["id"] == result.trade_intent_id
    assert (intent["mission_id"], intent["action"], intent["gate_result"]) == (
        mid, "open", None)
    assert json.loads(intent["payload_json"]) == INTENT
    assert tuple(conn.execute(
        "SELECT trade_intent_id, decision, actor, reject_code "
        "FROM signal_decisions").fetchone()) == (
        result.trade_intent_id, "accept", "llm", None)
    assert tuple(conn.execute(
        "SELECT decision, actor, reject_code, state FROM signal_shadow"
    ).fetchone()) == ("accept", "llm", None, "pending_entry")
    assert _state(conn, mid, sid) == ("completed", "consumed")


def test_in_tx_variants_leave_the_commit_to_the_caller(tmp_path):
    conn, mid, sid = _world(tmp_path)
    sd.finalize_accept_in_tx(conn, mission_id=mid, signal_id=sid,
                             intent_payload=INTENT, spread=SPREAD, output=None,
                             transcript=[], now=NOW)
    assert conn.in_transaction
    conn.rollback()
    _assert_untouched(conn, mid, sid)
    sd.finalize_reject_in_tx(conn, mission_id=mid, signal_id=sid, actor="llm",
                             reject_code="news_shock", spread=SPREAD,
                             output=None, transcript=[], now=NOW)
    assert conn.in_transaction
    conn.rollback()
    _assert_untouched(conn, mid, sid)


# --- 失敗注入: どの書込みが失敗しても全体が戻る ---------------------------------

_WRITE_POINTS = {
    "intent": "BEFORE INSERT ON trade_intents",
    "decision": "BEFORE INSERT ON signal_decisions",
    "shadow": "BEFORE INSERT ON signal_shadow",
    "consume": "BEFORE UPDATE ON signals",
    "mission": "BEFORE UPDATE ON missions",
}


def _fail_at(conn, point):
    conn.execute(f"CREATE TEMP TRIGGER fail_{point} {_WRITE_POINTS[point]} "
                 "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    conn.commit()


@pytest.mark.parametrize("point", list(_WRITE_POINTS))
def test_accept_rolls_back_everything_when_any_write_fails(tmp_path, point):
    conn, mid, sid = _world(tmp_path)
    _fail_at(conn, point)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        _accept(conn, mid, sid)
    _assert_untouched(conn, mid, sid)
    conn.execute(f"DROP TRIGGER fail_{point}")
    conn.commit()
    result = _accept(conn, mid, sid)
    assert _count(conn, "signal_decisions") == 1
    assert result.mission_status == "completed"


@pytest.mark.parametrize("point", ["decision", "shadow", "consume", "mission"])
@pytest.mark.parametrize("actor, code", [("system", "econ_window"),
                                         ("llm", "news_shock")])
def test_reject_rolls_back_everything_when_any_write_fails(
        tmp_path, point, actor, code):
    conn, mid, sid = _world(tmp_path)
    _fail_at(conn, point)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        _reject(conn, mid, sid, actor=actor, code=code)
    _assert_untouched(conn, mid, sid)


def test_a_mission_that_is_no_longer_running_rolls_the_decision_back(tmp_path):
    conn, mid, sid = _world(tmp_path)
    conn.execute("UPDATE missions SET status='failed' WHERE id=?", (mid,))
    conn.commit()
    with pytest.raises(sd.DecisionTxError):
        _reject(conn, mid, sid)
    assert _count(conn, "signal_decisions") == 0
    assert _count(conn, "signal_shadow") == 0
    assert _state(conn, mid, sid) == ("failed", "claimed")
    with pytest.raises(sd.DecisionTxError):
        _accept(conn, mid, sid)
    assert _count(conn, "trade_intents") == 0
    assert _state(conn, mid, sid) == ("failed", "claimed")


def test_a_signal_claimed_by_another_mission_is_not_decided(tmp_path):
    conn = _conn(tmp_path)
    mid, other = _mission(conn), _mission(conn)
    sid = _claimed(conn, other)
    with pytest.raises(ValueError, match="not claimed by mission"):
        _reject(conn, mid, sid)
    _assert_untouched(conn, mid, sid)
    assert conn.execute("SELECT claimed_by_mission_id FROM signals").fetchone()[0] == other


def test_a_bad_spread_or_shape_never_reaches_the_database(tmp_path):
    conn, mid, sid = _world(tmp_path)
    with pytest.raises(sd.DecisionShapeError):
        _reject(conn, mid, sid, actor="system", code="news_shock")
    with pytest.raises(sd.DecisionShapeError):
        _reject(conn, mid, sid, actor="llm", code="econ_window")
    _assert_untouched(conn, mid, sid)


def test_a_retry_never_writes_a_second_decision_for_the_signal(tmp_path):
    conn, mid, sid = _world(tmp_path)
    _reject(conn, mid, sid)
    retry_mid = _mission(conn)
    conn.execute("UPDATE signals SET status='claimed', claimed_by_mission_id=? "
                 "WHERE id=?", (retry_mid, sid))
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _reject(conn, retry_mid, sid, actor="llm", code="news_shock")
    with pytest.raises(sqlite3.IntegrityError):
        _accept(conn, retry_mid, sid)
    assert _count(conn, "signal_decisions") == 1
    assert _count(conn, "signal_shadow") == 1
    assert _count(conn, "trade_intents") == 0
    assert _state(conn, retry_mid, sid) == ("running", "claimed")


def test_commit_refuses_to_run_over_someone_elses_open_transaction(tmp_path):
    conn, mid, sid = _world(tmp_path)
    conn.execute("UPDATE missions SET model='pending-write' WHERE id=?", (mid,))
    assert conn.in_transaction
    with pytest.raises(RuntimeError, match="open transaction"):
        _reject(conn, mid, sid)
    assert conn.in_transaction
    conn.rollback()
    _assert_untouched(conn, mid, sid)


# --- decision 行の形 -------------------------------------------------------

CANONICAL = [
    ("accept", "llm", None, True),
    ("reject", "llm", "news_shock", False),
    ("reject", "llm", "opposite_position", False),
    ("reject", "system", "econ_window", False),
]
NON_CANONICAL = [
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
]


@pytest.mark.parametrize("decision, actor, code, with_intent", CANONICAL)
def test_validate_shape_accepts_the_three_forms(decision, actor, code,
                                                with_intent):
    sd.validate_shape(decision=decision, actor=actor, reject_code=code,
                      trade_intent_id=1 if with_intent else None)


@pytest.mark.parametrize("decision, actor, code, with_intent", NON_CANONICAL)
def test_validate_shape_rejects_every_other_form(decision, actor, code,
                                                 with_intent):
    with pytest.raises(sd.DecisionShapeError):
        sd.validate_shape(decision=decision, actor=actor, reject_code=code,
                          trade_intent_id=1 if with_intent else None)


def _intent(conn, mid, action="open"):
    iid = conn.execute(
        "INSERT INTO trade_intents (mission_id,payload_json,action,created_at) "
        "VALUES (?,'{}',?,?)", (mid, action, NOW.isoformat())).lastrowid
    conn.commit()
    return iid


def _record_accept(conn, mid, sid, iid):
    return sd.record_in_tx(conn, mission_id=mid, signal_id=sid,
                           decision="accept", actor="llm", reject_code=None,
                           trade_intent_id=iid, now=NOW)


def test_accept_points_only_at_an_open_intent_of_the_same_mission(tmp_path):
    conn, mid, sid = _world(tmp_path)
    other = _mission(conn)
    with pytest.raises(sd.DecisionShapeError, match="another mission"):
        _record_accept(conn, mid, sid, _intent(conn, other))
    with pytest.raises(sd.DecisionShapeError, match="does not exist"):
        _record_accept(conn, mid, sid, 9999)
    with pytest.raises(sd.DecisionShapeError, match="not an open intent"):
        _record_accept(conn, mid, sid, _intent(conn, mid, action="hold"))
    assert _count(conn, "signal_decisions") == 0
    iid = _intent(conn, mid)
    _record_accept(conn, mid, sid, iid)
    conn.commit()
    sid2 = _claimed(conn, mid, content_hash="h2")
    with pytest.raises(sd.DecisionShapeError, match="already bound"):
        _record_accept(conn, mid, sid2, iid)


def test_a_reject_never_carries_an_intent(tmp_path):
    conn, mid, sid = _world(tmp_path)
    iid = _intent(conn, mid)
    with pytest.raises(sd.DecisionShapeError):
        sd.record_in_tx(conn, mission_id=mid, signal_id=sid, decision="reject",
                        actor="llm", reject_code="news_shock",
                        trade_intent_id=iid, now=NOW)
    assert _count(conn, "signal_decisions") == 0


def test_get_for_signal(tmp_path):
    conn, mid, sid = _world(tmp_path)
    assert sd.get_for_signal(conn, sid) is None
    result = _reject(conn, mid, sid)
    assert sd.get_for_signal(conn, sid)["id"] == result.decision_id
