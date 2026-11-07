"""shadow の作成・状態遷移の CAS・t0・spread snapshot・候補と集計。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.store import signal_decisions as sd
from agentic_fx.store import signal_shadow as ss
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.signal_shadow import ShadowOutcome, ShadowSpread

NOW = datetime(2026, 10, 30, 13, 0, tzinfo=timezone.utc)
SPREAD = ShadowSpread(pip_size=0.01, spread_pips=1.0, source="assumed")
FILL_AT = NOW + timedelta(minutes=1)
EXIT_AT = NOW + timedelta(hours=2)


def _conn(tmp_path):
    conn = connect(tmp_path / "s.db")
    init_db(conn)
    return conn


def _decided(conn, *, n=0, accept=False, now=NOW, plugin="p"):
    mid = conn.execute(
        "INSERT INTO missions (loop,runner,model,status,started_at) "
        "VALUES ('trade','local','m','running',?)", (now.isoformat(),)).lastrowid
    sid = conn.execute(
        "INSERT INTO signals (plugin,content_hash,pair,timeframe,bar_ts,kind,"
        "payload_json,status,claimed_by_mission_id,claimed_at,created_at,"
        "contract_version) VALUES (?,?,'USDJPY','15m',"
        "'2026-10-30T12:45:00+00:00','strategy','{}','claimed',?,?,?,"
        "'executable_v1')",
        (plugin, f"h{n}", mid, now.isoformat(), now.isoformat())).lastrowid
    conn.commit()
    if accept:
        return sd.commit_accept(conn, mission_id=mid, signal_id=sid,
                                intent_payload={}, spread=SPREAD, output=None,
                                transcript=[], now=now)
    return sd.commit_reject(conn, mission_id=mid, signal_id=sid, actor="system",
                            reject_code="econ_window", spread=SPREAD,
                            output=None, transcript=[], now=now)


def _shadow(conn, shadow_id):
    return ss.get(conn, shadow_id)


# --- t0 / spread / pnl ----------------------------------------------------

def test_observation_start_keeps_an_exact_minute_and_rounds_up_otherwise():
    exact = datetime(2026, 10, 30, 13, 5, tzinfo=timezone.utc)
    assert ss.observation_start(exact) == exact
    assert ss.observation_start(exact + timedelta(microseconds=1)) == (
        exact + timedelta(minutes=1))
    assert ss.observation_start(exact - timedelta(microseconds=1)) == exact
    jst = timezone(timedelta(hours=9))
    assert ss.observation_start(
        datetime(2026, 10, 30, 22, 5, 30, tzinfo=jst)) == datetime(
        2026, 10, 30, 13, 6, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="naive"):
        ss.observation_start(datetime(2026, 10, 30, 13, 5))


@pytest.mark.parametrize("bid, ask, source, pips", [
    (150.00, 150.012, "quote", 1.2),
    (150.0, 150.0, "assumed", 0.9),
    (None, 150.0, "assumed", 0.9),
    (150.0, None, "assumed", 0.9),
    (float("nan"), 150.0, "assumed", 0.9),
    (150.0, float("inf"), "assumed", 0.9),
    (150.1, 150.0, "assumed", 0.9),
    (0.0, 0.5, "assumed", 0.9),
    (-1.0, 0.5, "assumed", 0.9),
])
def test_spread_from_quote_uses_quote_only_when_ask_above_bid_above_zero(
        bid, ask, source, pips):
    spread = ss.spread_from_quote(bid, ask, pip_size=0.01,
                                  assumed_spread_pips=0.9)
    assert spread.source == source
    assert math.isclose(spread.spread_pips, pips)


@pytest.mark.parametrize("kwargs", [
    {"pip_size": 0, "spread_pips": 1.0, "source": "quote"},
    {"pip_size": float("nan"), "spread_pips": 1.0, "source": "quote"},
    {"pip_size": 0.01, "spread_pips": -0.1, "source": "quote"},
    {"pip_size": 0.01, "spread_pips": float("inf"), "source": "quote"},
    {"pip_size": 0.01, "spread_pips": 1.0, "source": "mid"},
])
def test_shadow_spread_rejects_invalid_snapshots(kwargs):
    with pytest.raises(ValueError):
        ShadowSpread(**kwargs)


def test_pnl_pips_by_direction():
    assert math.isclose(ss.pnl_pips("long", entry=150.0, exit_price=150.5,
                                    pip_size=0.01), 50.0)
    assert math.isclose(ss.pnl_pips("short", entry=150.0, exit_price=150.5,
                                    pip_size=0.01), -50.0)
    with pytest.raises(ValueError):
        ss.pnl_pips("flat", entry=1, exit_price=1, pip_size=0.01)


# --- 作成 ------------------------------------------------------------------

def test_start_copies_the_snapshot_from_the_decision(tmp_path):
    conn = _conn(tmp_path)
    accepted = _decided(conn, n=1, accept=True)
    row = _shadow(conn, accepted.shadow_id)
    assert (row["decision"], row["actor"], row["reject_code"]) == (
        "accept", "llm", None)
    assert (row["created_at"], row["updated_at"]) == (NOW.isoformat(),) * 2
    with pytest.raises(LookupError):
        ss.start_in_tx(conn, decision_id=9999, spread=SPREAD, now=NOW)
    conn.rollback()


# --- 状態遷移 ----------------------------------------------------------------

def _at(conn, shadow_id, state):
    """shadow を指定 state の正規形に書き換える (遷移元の準備)。"""
    cols = {
        "pending_entry": {},
        "filled": {"entry_price": 150.0, "entry_at": FILL_AT.isoformat()},
        "resolved": {"result_code": "sl", "entry_price": 150.0,
                     "entry_at": FILL_AT.isoformat(), "exit_price": 149.9,
                     "pnl_pips": -10.0, "resolved_at": EXIT_AT.isoformat()},
        "expired": {"result_code": "no_fill"},
    }[state]
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE signal_shadow SET state=?{', ' if sets else ''}{sets} "
                 "WHERE id=?", (state, *cols.values(), shadow_id))
    conn.commit()
    return _shadow(conn, shadow_id)


_OUTCOMES = {
    "filled": ShadowOutcome.filled(entry_price=150.0, entry_at=FILL_AT),
    "sl+entry": ShadowOutcome.resolved("sl", exit_price=149.9, pnl_pips=-10.0,
                                       resolved_at=EXIT_AT, entry_price=150.0,
                                       entry_at=FILL_AT),
    "tp+entry": ShadowOutcome.resolved("tp", exit_price=150.2, pnl_pips=20.0,
                                       resolved_at=EXIT_AT, entry_price=150.0,
                                       entry_at=FILL_AT),
    "day_close+entry": ShadowOutcome.resolved(
        "day_close", exit_price=150.1, pnl_pips=10.0, resolved_at=EXIT_AT,
        entry_price=150.0, entry_at=FILL_AT),
    "sl": ShadowOutcome.resolved("sl", exit_price=149.9, pnl_pips=-10.0,
                                 resolved_at=EXIT_AT),
    "tp": ShadowOutcome.resolved("tp", exit_price=150.2, pnl_pips=20.0,
                                 resolved_at=EXIT_AT),
    "day_close": ShadowOutcome.resolved("day_close", exit_price=150.1,
                                        pnl_pips=10.0, resolved_at=EXIT_AT),
    "insufficient": ShadowOutcome.data_insufficient(),
    "insufficient+entry": ShadowOutcome.data_insufficient(
        entry_price=150.0, entry_at=FILL_AT),
    "no_fill": ShadowOutcome.no_fill(),
}

# §3.3 の 4 正規形と §3.4 から決まる許可遷移 (この表の外はすべて禁止)。
ALLOWED = {
    ("pending_entry", "filled"),
    ("pending_entry", "sl+entry"), ("pending_entry", "tp+entry"),
    ("pending_entry", "day_close+entry"),
    ("pending_entry", "insufficient"), ("pending_entry", "insufficient+entry"),
    ("pending_entry", "no_fill"),
    ("filled", "sl"), ("filled", "tp"), ("filled", "day_close"),
    ("filled", "insufficient"),
}
ALL_PAIRS = [(f, o) for f in ss.STATES for o in _OUTCOMES]


@pytest.mark.parametrize("from_state, outcome", sorted(ALLOWED))
def test_allowed_transitions_write_once(tmp_path, from_state, outcome):
    conn = _conn(tmp_path)
    shadow_id = _decided(conn).shadow_id
    before = _at(conn, shadow_id, from_state)
    later = NOW + timedelta(days=2)
    assert ss.transition(conn, shadow_id, expected_state=from_state,
                         expected_updated_at=before["updated_at"],
                         outcome=_OUTCOMES[outcome], now=later)
    after = _shadow(conn, shadow_id)
    target = _OUTCOMES[outcome]
    assert (after["state"], after["result_code"]) == (
        target.state, target.result_code)
    assert after["updated_at"] == later.isoformat()
    if from_state == "filled":
        assert (after["entry_price"], after["entry_at"]) == (
            before["entry_price"], before["entry_at"])
    elif target.entry_price is not None:
        assert (after["entry_price"], after["entry_at"]) == (
            150.0, FILL_AT.isoformat())
    else:
        assert (after["entry_price"], after["entry_at"]) == (None, None)
    if target.exit_price is not None:
        assert (after["exit_price"], after["pnl_pips"], after["resolved_at"]) == (
            target.exit_price, target.pnl_pips, EXIT_AT.isoformat())
    # 1 回だけ: 同じ CAS をもう一度出しても書かない。
    again = ss.transition(conn, shadow_id, expected_state=from_state,
                          expected_updated_at=before["updated_at"],
                          outcome=_OUTCOMES[outcome], now=later)
    assert again is False
    assert _shadow(conn, shadow_id) == after


@pytest.mark.parametrize("from_state, outcome",
                         [p for p in ALL_PAIRS if p not in ALLOWED])
def test_forbidden_transitions_raise_before_any_sql(tmp_path, from_state,
                                                    outcome):
    conn = _conn(tmp_path)
    shadow_id = _decided(conn).shadow_id
    before = _at(conn, shadow_id, from_state)
    with pytest.raises(ss.ShadowTransitionError):
        ss.transition(conn, shadow_id, expected_state=from_state,
                      expected_updated_at=before["updated_at"],
                      outcome=_OUTCOMES[outcome], now=NOW + timedelta(days=2))
    assert not conn.in_transaction
    assert _shadow(conn, shadow_id) == before


def test_the_cas_needs_both_the_state_and_updated_at(tmp_path):
    conn = _conn(tmp_path)
    shadow_id = _decided(conn).shadow_id
    row = _shadow(conn, shadow_id)
    assert not ss.transition(conn, shadow_id, expected_state="pending_entry",
                             expected_updated_at="2026-01-01T00:00:00+00:00",
                             outcome=_OUTCOMES["no_fill"], now=NOW)
    _at(conn, shadow_id, "filled")
    assert not ss.transition(conn, shadow_id, expected_state="pending_entry",
                             expected_updated_at=row["updated_at"],
                             outcome=_OUTCOMES["no_fill"], now=NOW)
    assert _shadow(conn, shadow_id)["state"] == "filled"
    assert not conn.in_transaction


def test_two_workers_on_one_shadow_succeed_once(tmp_path):
    conn = _conn(tmp_path)
    shadow_id = _decided(conn).shadow_id
    other = connect(tmp_path / "s.db")
    seen = _shadow(conn, shadow_id)["updated_at"]
    results = [
        ss.transition(c, shadow_id, expected_state="pending_entry",
                      expected_updated_at=seen, outcome=_OUTCOMES[o],
                      now=NOW + timedelta(days=2))
        for c, o in ((conn, "no_fill"), (other, "insufficient"))]
    assert results == [True, False]
    assert _shadow(conn, shadow_id)["result_code"] == "no_fill"


def test_filled_keeps_its_entry_and_numbers_must_be_finite(tmp_path):
    conn = _conn(tmp_path)
    shadow_id = _decided(conn).shadow_id
    filled = _at(conn, shadow_id, "filled")
    with pytest.raises(ss.ShadowTransitionError, match="keeps its stored entry"):
        ss.transition_in_tx(conn, shadow_id, expected_state="filled",
                            expected_updated_at=filled["updated_at"],
                            outcome=_OUTCOMES["sl+entry"], now=NOW)
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ss.ShadowTransitionError, match="finite"):
            ss.transition_in_tx(
                conn, shadow_id, expected_state="filled",
                expected_updated_at=filled["updated_at"],
                outcome=ShadowOutcome.resolved("tp", exit_price=bad,
                                               pnl_pips=1.0,
                                               resolved_at=EXIT_AT),
                now=NOW)
    with pytest.raises(ss.ShadowTransitionError, match="no exit values"):
        ss.transition_in_tx(
            conn, shadow_id, expected_state="filled",
            expected_updated_at=filled["updated_at"],
            outcome=ShadowOutcome(ss.STATE_RESOLVED,
                                  ss.RESULT_DATA_INSUFFICIENT,
                                  exit_price=150.0), now=NOW)
    assert not conn.in_transaction
    assert _shadow(conn, shadow_id) == filled


def test_transition_in_tx_does_not_commit(tmp_path):
    conn = _conn(tmp_path)
    shadow_id = _decided(conn).shadow_id
    row = _shadow(conn, shadow_id)
    assert ss.transition_in_tx(conn, shadow_id, expected_state="pending_entry",
                               expected_updated_at=row["updated_at"],
                               outcome=_OUTCOMES["no_fill"], now=NOW)
    conn.rollback()
    assert _shadow(conn, shadow_id) == row


# --- 候補と集計 --------------------------------------------------------------

def test_unresolved_candidates_skip_terminal_complete_and_young_rows(tmp_path):
    conn = _conn(tmp_path)
    old = NOW - timedelta(days=3)
    a = _decided(conn, n=1, now=old).shadow_id
    b = _decided(conn, n=2, now=old + timedelta(minutes=1)).shadow_id
    resolved = _decided(conn, n=3, now=old).shadow_id
    with_complete = _decided(conn, n=4, now=old).shadow_id
    young = _decided(conn, n=5, now=NOW).shadow_id
    _at(conn, b, "filled")
    _at(conn, resolved, "expired")
    conn.execute(
        "INSERT INTO ohlcv_slice_sets (shadow_id,pair,range_start,range_end,"
        "expected_rows,state,created_at,completed_at,rule_version) VALUES "
        "(?,'USDJPY',?,?,1,'complete',?,?,1)",
        (with_complete, old.isoformat(),
         (old + timedelta(minutes=1)).isoformat(), NOW.isoformat(),
         NOW.isoformat()))
    conn.commit()
    got = ss.unresolved_candidates(conn, decided_before=NOW - timedelta(days=1),
                                   limit=10)
    assert [r["id"] for r in got] == [a, b]
    assert got[0]["pair"] == "USDJPY" and got[0]["set_id"] is None
    assert young not in [r["id"] for r in got]
    assert len(ss.unresolved_candidates(
        conn, decided_before=NOW - timedelta(days=1), limit=1)) == 1


def test_period_summary_counts_by_strategy_and_outcome(tmp_path):
    conn = _conn(tmp_path)
    ids = [_decided(conn, n=i, plugin="p" if i < 3 else "q").shadow_id
           for i in range(4)]
    outside = _decided(conn, n=9, now=NOW + timedelta(days=1)).shadow_id
    for shadow_id, pnl in ((ids[0], 20.0), (ids[1], -10.0)):
        row = _at(conn, shadow_id, "filled")
        ss.transition(conn, shadow_id, expected_state="filled",
                      expected_updated_at=row["updated_at"],
                      outcome=ShadowOutcome.resolved(
                          "tp" if pnl > 0 else "sl", exit_price=150.0,
                          pnl_pips=pnl, resolved_at=EXIT_AT), now=EXIT_AT)
    del outside
    rows = ss.period_summary(conn, since=NOW, until=NOW + timedelta(hours=1))
    flat = {(r["plugin"], r["state"], r["result_code"]):
            (r["n"], r["numeric_n"], r["pnl_pips_sum"], r["win_n"])
            for r in rows}
    assert flat == {
        ("p", "pending_entry", None): (1, 0, 0.0, 0),
        ("p", "resolved", "sl"): (1, 1, -10.0, 0),
        ("p", "resolved", "tp"): (1, 1, 20.0, 1),
        ("q", "pending_entry", None): (1, 0, 0.0, 0),
    }
