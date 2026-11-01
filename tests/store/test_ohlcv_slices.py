"""slice set の台帳・staging・完備 CAS・世代回収・掃除・purge と容量算定。"""
from __future__ import annotations

import inspect
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agentic_fx.store import ohlcv_slices as os_
from agentic_fx.store import signal_decisions as sd
from agentic_fx.store import signal_shadow as ss
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv_slices import ShadowCas, SliceRow

NOW = datetime(2026, 10, 30, 13, 0, tzinfo=timezone.utc)
TS = datetime(2026, 10, 30, 12, 30, tzinfo=timezone.utc)
PAIRS = {"USDJPY": ("USD", "JPY"), "EURUSD": ("EUR", "USD")}
SPREAD = ss.ShadowSpread(pip_size=0.01, spread_pips=1.0, source="assumed")


def _conn(tmp_path, name="o.db"):
    conn = connect(tmp_path / name)
    init_db(conn)
    return conn


def _event(conn, *, ts=TS, country="USD", name="CPI", importance=3):
    eid = conn.execute(
        "INSERT INTO econ_events (ts,country,name,importance) VALUES (?,?,?,?)",
        (ts.isoformat(), country, name, importance)).lastrowid
    conn.commit()
    return eid


def _bars(start, n, *, price=150.0, source="mt5-live"):
    return [SliceRow(start + timedelta(minutes=i), price, price + 0.02,
                     price - 0.02, price + 0.01, 10.0, source, "observed")
            for i in range(n)]


def _event_set(conn, eid, pair="USDJPY"):
    set_id = os_.create_event_set_in_tx(conn, event_id=eid, pair=pair, now=NOW)
    conn.commit()
    return set_id


def _stage_all(conn, set_id, gen, rows, step=240):
    for i in range(0, len(rows), step):
        result = os_.stage_event_rows(conn, set_id=set_id, generation=gen,
                                      rows=rows[i:i + step], max_rows=step)
        assert result.committed, result
    return result


def _rows_of(conn, set_id):
    return conn.execute("SELECT COUNT(*) FROM ohlcv_slices WHERE set_id=?",
                        (set_id,)).fetchone()[0]


def _shadow(conn, n=0):
    mid = conn.execute(
        "INSERT INTO missions (loop,runner,model,status,started_at) "
        "VALUES ('trade','local','m','running',?)", (NOW.isoformat(),)).lastrowid
    sid = conn.execute(
        "INSERT INTO signals (plugin,content_hash,pair,timeframe,bar_ts,kind,"
        "payload_json,status,claimed_by_mission_id,claimed_at,created_at,"
        "contract_version) VALUES ('p',?,'USDJPY','15m',"
        "'2026-10-30T12:45:00+00:00','strategy','{}','claimed',?,?,?,"
        "'executable_v1')",
        (f"h{n}", mid, NOW.isoformat(), NOW.isoformat())).lastrowid
    conn.commit()
    return sd.commit_reject(conn, mission_id=mid, signal_id=sid, actor="system",
                            reject_code="econ_window", spread=SPREAD,
                            output=None, transcript=[], now=NOW).shadow_id


# --- 1m 行の事前検証 (DB の CHECK と同じ判定) --------------------------------

_GOOD = SliceRow(TS, 150.0, 150.2, 149.9, 150.1, 5.0, "mt5-live", "observed")
_BAD_OHLC = [
    {"open": float("nan")}, {"high": float("inf")}, {"low": 0.0},
    {"low": -1.0}, {"close": float("-inf")},
    {"high": 149.0},                      # high < low
    {"open": 150.3},                      # open > high
    {"close": 149.8},                     # close < low
    {"volume": -1.0}, {"volume": float("inf")},
    {"quality": "no_tick_flat"},          # flat なのに 4 値が違う
    {"quality": "no_tick_flat", "open": 150.0, "high": 150.0, "low": 150.0,
     "close": 150.0, "volume": 1.0},      # flat の volume は 0
    {"quality": "datafeed_outage_gap"},   # outage が値を持つ
]


@pytest.mark.parametrize("change", _BAD_OHLC)
def test_invalid_bars_are_refused_before_and_by_the_database(tmp_path, change):
    row = replace(_GOOD, **change)
    assert os_.slice_row_problem(row) is not None
    with pytest.raises(os_.InvalidSliceRow):
        os_.validate_slice_rows([row])
    conn = _conn(tmp_path)
    set_id = _event_set(conn, _event(conn))
    with pytest.raises(sqlite3.IntegrityError):
        os_._insert_rows(conn, set_id, [row])
    conn.rollback()


@pytest.mark.parametrize("row", [
    _GOOD,
    SliceRow(TS, 150.0, 150.0, 150.0, 150.0, 0.0, "mt5-live", "no_tick_flat"),
    SliceRow(TS, None, None, None, None, None, "mt5-live", "datafeed_outage_gap"),
])
def test_valid_bars_pass(row):
    assert os_.slice_row_problem(row) is None


@pytest.mark.parametrize("rows, match", [
    ([replace(_GOOD, bar_time=TS.replace(tzinfo=None))], "naive"),
    ([replace(_GOOD, bar_time=TS + timedelta(seconds=1))], "minute boundary"),
    ([_GOOD, _GOOD], "duplicate"),
    ([replace(_GOOD, quality="interpolated")], "unknown quality"),
    ([replace(_GOOD, source="")], "source"),
])
def test_row_shape_problems(rows, match):
    with pytest.raises(os_.InvalidSliceRow, match=match):
        os_.validate_slice_rows(rows)


# --- event set の台帳 --------------------------------------------------------

def test_event_set_targets_follow_importance_currency_all_and_supersede(tmp_path):
    conn = _conn(tmp_path)
    usd = _event(conn)
    jpy = _event(conn, country="JPY", name="BOJ")
    allc = _event(conn, country="All", name="G7")
    _event(conn, country="CAD", name="Jobs")
    low = _event(conn, name="Claims", importance=2)
    old = _event(conn, name="PCE", ts=TS + timedelta(hours=1))
    new = _event(conn, name="PCE", ts=TS + timedelta(hours=2))
    conn.execute("UPDATE econ_events SET superseded_by=? WHERE id=?", (new, old))
    conn.commit()
    got = os_.event_set_targets(conn, importance_min=3, pair_currencies=PAIRS)
    assert got == [(usd, "USDJPY"), (usd, "EURUSD"), (jpy, "USDJPY"),
                   (allc, "USDJPY"), (allc, "EURUSD"),
                   (new, "USDJPY"), (new, "EURUSD")]
    assert (low, "USDJPY") in os_.event_set_targets(
        conn, importance_min=2, pair_currencies=PAIRS)
    assert os_.event_set_targets(conn, importance_min=3, pair_currencies=PAIRS,
                                 event_ids=[jpy, old]) == [(jpy, "USDJPY")]
    assert os_.event_set_targets(conn, importance_min=3, pair_currencies=PAIRS,
                                 event_ids=[]) == []


def test_awaiting_creation_is_idempotent_and_covers_the_event_range(tmp_path):
    conn = _conn(tmp_path)
    eid = _event(conn)
    first = os_.sync_event_sets(
        conn, targets=[(eid, "USDJPY")], scope_event_ids=[eid], now=NOW)
    again = os_.sync_event_sets(
        conn, targets=[(eid, "USDJPY")], scope_event_ids=[eid], now=NOW)
    assert len(first.created) == 1 and again == os_.SyncResult((), (), (), ())
    row = os_.get_set(conn, first.created[0])
    assert (row["state"], row["range_start"], row["range_end"],
            row["expected_rows"], row["generation"], row["spread_pips"]) == (
        "awaiting", "2026-10-30T12:15:00+00:00", "2026-10-30T16:30:00+00:00",
        255, None, None)
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_slice_sets").fetchone()[0] == 1
    old = _event(conn, name="PCE")
    conn.execute("UPDATE econ_events SET superseded_by=? WHERE id=?", (eid, old))
    conn.commit()
    assert os_.create_event_set_in_tx(conn, event_id=old, pair="USDJPY",
                                      now=NOW) is None


def test_sync_in_tx_rolls_back_as_one_transaction(tmp_path):
    conn = _conn(tmp_path)
    eid = _event(conn)
    os_.sync_event_sets_in_tx(conn, targets=[(eid, "USDJPY")],
                              scope_event_ids=None, now=NOW)
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_slice_sets").fetchone()[0] == 0


def test_cancel_reactivate_and_sweep_follow_the_ledger_rules(tmp_path):
    conn = _conn(tmp_path)
    eid = _event(conn)
    gen = os_.new_generation(conn, now=NOW)
    set_id = os_.sync_event_sets(conn, targets=[(eid, "USDJPY")],
                                 scope_event_ids=None, now=NOW).created[0]
    assert os_.record_event_spread(conn, set_id, spread_pips=1.3,
                                   spread_source="quote")
    _stage_all(conn, set_id, gen, _bars(TS - timedelta(minutes=15), 30))
    # importance 低下などで対象外 → staging のまま cancelled、行は残る。
    out = os_.sync_event_sets(conn, targets=[], scope_event_ids=None, now=NOW)
    assert out.cancelled == (set_id,)
    row = os_.get_set(conn, set_id)
    assert (row["state"], row["generation"], row["completed_at"]) == (
        "cancelled", None, NOW.isoformat())
    assert _rows_of(conn, set_id) == 30
    assert os_.matured_event_sets(conn, now=NOW + timedelta(days=9),
                                  limit=10) == []
    assert os_.spread_candidates(conn, now=TS) == []
    # 行が残る間は再有効化を見送る。
    back = os_.sync_event_sets(conn, targets=[(eid, "USDJPY")],
                               scope_event_ids=None, now=NOW)
    assert back.deferred == (set_id,) and back.reactivated == ()
    assert os_.get_set(conn, set_id)["state"] == "cancelled"
    # 毎 cycle の掃除は状態を変えずに行だけ消す。
    assert os_.cancelled_sets_with_rows(conn, limit=10) == [set_id]
    step = os_.sweep_cancelled_step(conn, set_id=set_id, max_rows=20)
    assert (step.rows, step.done) == (20, False)
    step = os_.sweep_cancelled_step(conn, set_id=set_id, max_rows=20)
    assert (step.rows, step.done) == (10, True)
    assert os_.get_set(conn, set_id)["state"] == "cancelled"
    assert os_.cancelled_sets_with_rows(conn, limit=10) == []
    back = os_.sync_event_sets(conn, targets=[(eid, "USDJPY")],
                               scope_event_ids=None, now=NOW)
    assert back.reactivated == (set_id,) and back.created == ()
    row = os_.get_set(conn, set_id)
    assert (row["state"], row["completed_at"], row["generation"],
            row["purged_at"], row["spread_pips"], row["spread_source"]) == (
        "awaiting", None, None, None, 1.3, "quote")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_slice_sets").fetchone()[0] == 1


def test_a_superseded_event_set_stays_cancelled(tmp_path):
    conn = _conn(tmp_path)
    eid = _event(conn)
    set_id = os_.sync_event_sets(conn, targets=[(eid, "USDJPY")],
                                 scope_event_ids=None, now=NOW).created[0]
    new = _event(conn, ts=TS + timedelta(hours=1))
    conn.execute("UPDATE econ_events SET superseded_by=? WHERE id=?", (new, eid))
    conn.commit()
    targets = os_.event_set_targets(conn, importance_min=3,
                                    pair_currencies={"USDJPY": ("USD", "JPY")})
    out = os_.sync_event_sets(conn, targets=targets, scope_event_ids=None,
                              now=NOW)
    assert out.cancelled == (set_id,)
    assert not os_.reactivate_in_tx(conn, set_id)
    conn.rollback()
    out = os_.sync_event_sets(conn, targets=[(eid, "USDJPY")],
                              scope_event_ids=None, now=NOW)
    assert out.deferred == (set_id,)
    assert os_.get_set(conn, set_id)["state"] == "cancelled"


def test_startup_completion_creates_only_missing_sets(tmp_path):
    conn = _conn(tmp_path)
    usd = _event(conn)
    old = _event(conn, ts=TS - timedelta(days=30), name="NFP")
    low = _event(conn, name="Claims", importance=2)
    one = {"USDJPY": ("USD", "JPY")}
    first = os_.sync_event_sets(
        conn, targets=os_.event_set_targets(conn, importance_min=3,
                                            pair_currencies=one),
        scope_event_ids=None, now=NOW)
    assert len(first.created) == 2
    done = os_.get_set(conn, first.created[1])
    conn.execute("UPDATE ohlcv_slice_sets SET state='complete', completed_at=? "
                 "WHERE id=?", (NOW.isoformat(), done["id"]))
    conn.commit()
    # pair 追加と importance_min 引き下げの後の起動時補完。
    second = os_.sync_event_sets(
        conn, targets=os_.event_set_targets(conn, importance_min=2,
                                            pair_currencies=PAIRS),
        scope_event_ids=None, now=NOW)
    pairs_of = {(r["event_id"], r["pair"]) for r in conn.execute(
        "SELECT event_id, pair FROM ohlcv_slice_sets")}
    assert pairs_of == {(e, p) for e in (usd, old, low)
                        for p in ("USDJPY", "EURUSD")}
    assert len(second.created) == 4 and second.cancelled == ()
    # pair を外すと未完 set だけ cancelled、complete は変えない。
    third = os_.sync_event_sets(
        conn, targets=os_.event_set_targets(conn, importance_min=3,
                                            pair_currencies=one),
        scope_event_ids=None, now=NOW)
    states = {(r["event_id"], r["pair"]): r["state"] for r in conn.execute(
        "SELECT event_id, pair, state FROM ohlcv_slice_sets")}
    assert states[(done["event_id"], "USDJPY")] == "complete"
    assert states[(usd, "USDJPY")] == "awaiting"
    assert {k for k, v in states.items() if v == "cancelled"} == {
        (usd, "EURUSD"), (old, "EURUSD"), (low, "USDJPY"), (low, "EURUSD")}
    assert len(third.cancelled) == 4


def test_scope_limits_cancellation_to_the_events_of_the_pass(tmp_path):
    conn = _conn(tmp_path)
    a, b = _event(conn), _event(conn, name="PPI")
    os_.sync_event_sets(conn, targets=[(a, "USDJPY"), (b, "USDJPY")],
                        scope_event_ids=None, now=NOW)
    out = os_.sync_event_sets(conn, targets=[], scope_event_ids=[a], now=NOW)
    assert len(out.cancelled) == 1
    assert {r[0]: r[1] for r in conn.execute(
        "SELECT event_id, state FROM ohlcv_slice_sets")} == {
        a: "cancelled", b: "awaiting"}
    assert os_.sync_event_sets(conn, targets=[], scope_event_ids=[],
                               now=NOW).cancelled == ()


def test_spread_is_recorded_once_on_awaiting_sets(tmp_path):
    conn = _conn(tmp_path)
    eid = _event(conn)
    set_id = _event_set(conn, eid)
    assert os_.spread_candidates(conn, now=TS - timedelta(minutes=5, seconds=1)) == []
    got = os_.spread_candidates(conn, now=TS - timedelta(minutes=5))
    assert [r["set_id"] for r in got] == [set_id]
    assert [r["set_id"] for r in os_.spread_candidates(
        conn, now=TS + timedelta(hours=1))] == [set_id]
    assert os_.record_event_spread(conn, set_id, spread_pips=0.8,
                                   spread_source="assumed")
    assert not os_.record_event_spread(conn, set_id, spread_pips=1.1,
                                       spread_source="quote")
    assert (os_.get_set(conn, set_id)["spread_pips"],
            os_.get_set(conn, set_id)["spread_source"]) == (0.8, "assumed")
    assert os_.spread_candidates(conn, now=TS) == []
    with pytest.raises(ValueError):
        os_.record_event_spread(conn, set_id, spread_pips=1.0,
                                spread_source="mid")


def test_matured_event_sets_wait_for_four_hours_and_a_day(tmp_path):
    conn = _conn(tmp_path)
    set_id = _event_set(conn, _event(conn))
    edge = TS + timedelta(hours=4, days=1)
    assert os_.matured_event_sets(conn, now=edge - timedelta(seconds=1),
                                  limit=5) == []
    assert [r["id"] for r in os_.matured_event_sets(conn, now=edge,
                                                    limit=5)] == [set_id]


# --- staging と完備 ---------------------------------------------------------

def test_staging_moves_awaiting_to_the_current_generation_and_completes(tmp_path):
    conn = _conn(tmp_path)
    set_id = _event_set(conn, _event(conn))
    os_.record_event_spread(conn, set_id, spread_pips=1.1, spread_source="quote")
    gen = os_.new_generation(conn, now=NOW)
    rows = _bars(TS - timedelta(minutes=15), 255)
    first = os_.stage_event_rows(conn, set_id=set_id, generation=gen,
                                 rows=rows[:240])
    assert first.committed and first.rows == 240
    assert first.commit_seconds is not None and first.commit_seconds >= 0
    row = os_.get_set(conn, set_id)
    assert (row["state"], row["generation"], row["spread_pips"]) == (
        "staging", gen, 1.1)
    assert os_.complete_rows(conn, set_id) is None
    short = os_.complete_set(conn, set_id=set_id, generation=gen, now=NOW)
    assert short.status == "conflict"
    os_.stage_event_rows(conn, set_id=set_id, generation=gen, rows=rows[240:])
    assert os_.complete_set(conn, set_id=set_id, generation=gen + 1,
                            now=NOW).status == "conflict"
    done = os_.complete_set(conn, set_id=set_id, generation=gen, now=NOW)
    assert done.committed
    row = os_.get_set(conn, set_id)
    assert (row["state"], row["generation"], row["completed_at"]) == (
        "complete", None, NOW.isoformat())
    assert os_.complete_rows(conn, set_id) == rows
    again = os_.complete_set(conn, set_id=set_id, generation=gen, now=NOW)
    assert again.status == "conflict"


def test_staging_refuses_other_generations_states_and_ranges(tmp_path):
    conn = _conn(tmp_path)
    set_id = _event_set(conn, _event(conn))
    gen = os_.new_generation(conn, now=NOW)
    start = TS - timedelta(minutes=15)
    _stage_all(conn, set_id, gen, _bars(start, 10))
    other = os_.stage_event_rows(conn, set_id=set_id, generation=gen + 1,
                                 rows=_bars(start + timedelta(minutes=10), 5))
    assert other.status == "conflict" and _rows_of(conn, set_id) == 10
    with pytest.raises(os_.InvalidSliceRow, match="outside"):
        os_.stage_event_rows(conn, set_id=set_id, generation=gen,
                             rows=_bars(start - timedelta(minutes=1), 1))
    with pytest.raises(ValueError, match="batch limit"):
        os_.stage_event_rows(conn, set_id=set_id, generation=gen,
                             rows=_bars(start, 11), max_rows=10)
    with pytest.raises(ValueError, match="max_rows"):
        os_.stage_event_rows(conn, set_id=set_id, generation=gen,
                             rows=_bars(start, 1), max_rows=241)
    assert not conn.in_transaction and _rows_of(conn, set_id) == 10


def test_an_out_of_range_first_batch_leaves_the_set_awaiting(tmp_path):
    conn = _conn(tmp_path)
    set_id = _event_set(conn, _event(conn))
    gen = os_.new_generation(conn, now=NOW)
    with pytest.raises(os_.InvalidSliceRow):
        os_.stage_event_rows(conn, set_id=set_id, generation=gen,
                             rows=_bars(TS + timedelta(hours=4), 1))
    assert os_.get_set(conn, set_id)["state"] == "awaiting"


@pytest.mark.parametrize("change", [{"close": float("nan")}, {"high": 149.0},
                                    {"low": 0.0}])
def test_an_invalid_bar_in_a_batch_writes_nothing(tmp_path, change):
    conn = _conn(tmp_path)
    set_id = _event_set(conn, _event(conn))
    gen = os_.new_generation(conn, now=NOW)
    rows = _bars(TS - timedelta(minutes=15), 3)
    rows[1] = replace(rows[1], **change)
    calls = []
    with pytest.raises(os_.InvalidSliceRow):
        os_.stage_event_rows(conn, set_id=set_id, generation=gen, rows=rows,
                             begin=lambda c: calls.append(c) or True)
    assert calls == [] and not conn.in_transaction
    assert os_.get_set(conn, set_id)["state"] == "awaiting"
    assert _rows_of(conn, set_id) == 0


def test_begin_hook_can_yield_and_must_open_a_transaction(tmp_path):
    conn = _conn(tmp_path)
    set_id = _event_set(conn, _event(conn))
    gen = os_.new_generation(conn, now=NOW)
    rows = _bars(TS - timedelta(minutes=15), 3)
    calls = []

    def flag_up(c):
        calls.append(c)
        return False

    out = os_.stage_event_rows(conn, set_id=set_id, generation=gen, rows=rows,
                               begin=flag_up)
    assert out.status == "yielded" and calls == [conn]
    assert os_.get_set(conn, set_id)["state"] == "awaiting"
    with pytest.raises(RuntimeError, match="without opening"):
        os_.stage_event_rows(conn, set_id=set_id, generation=gen, rows=rows,
                             begin=lambda c: True)
    conn.execute("UPDATE missions SET model='x'")
    with pytest.raises(RuntimeError, match="open transaction"):
        os_.stage_event_rows(conn, set_id=set_id, generation=gen, rows=rows)
    conn.rollback()


def test_busy_rolls_back_and_raises_without_waiting(tmp_path):
    conn = _conn(tmp_path)
    set_id = _event_set(conn, _event(conn))
    gen = os_.new_generation(conn, now=NOW)
    worker = connect(tmp_path / "o.db")
    worker.execute("PRAGMA busy_timeout=0")
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            os_.stage_event_rows(worker, set_id=set_id, generation=gen,
                                 rows=_bars(TS - timedelta(minutes=15), 2))
        assert not worker.in_transaction
    finally:
        conn.rollback()
    assert os_.get_set(conn, set_id)["state"] == "awaiting"
    worker.close()


def test_readback_mismatch_discards_rows_and_keeps_the_spread(tmp_path):
    conn = _conn(tmp_path)
    set_id = _event_set(conn, _event(conn))
    os_.record_event_spread(conn, set_id, spread_pips=1.4, spread_source="quote")
    gen = os_.new_generation(conn, now=NOW)
    rows = _bars(TS - timedelta(minutes=15), 255)
    _stage_all(conn, set_id, gen, rows)
    conn.execute("UPDATE ohlcv_slices SET close=150.015 WHERE set_id=? AND "
                 "bar_time=?", (set_id, rows[7].bar_time.isoformat()))
    conn.commit()
    assert os_.staging_rows(conn, set_id=set_id, generation=gen) != rows
    out = os_.discard_staging(conn, set_id=set_id, generation=gen, max_rows=100)
    assert out.committed and out.done
    row = os_.get_set(conn, set_id)
    assert (row["state"], row["generation"], row["spread_pips"],
            row["spread_source"], row["completed_at"]) == (
        "awaiting", None, 1.4, "quote", None)
    assert _rows_of(conn, set_id) == 0
    # 次 cycle は正しい値で収束する。
    gen2 = os_.new_generation(conn, now=NOW)
    _stage_all(conn, set_id, gen2, rows)
    assert os_.staging_rows(conn, set_id=set_id, generation=gen2) == rows
    assert os_.complete_set(conn, set_id=set_id, generation=gen2,
                            now=NOW).committed
    assert os_.complete_rows(conn, set_id) == rows


def test_discard_step_deletes_in_batches_before_resetting(tmp_path):
    conn = _conn(tmp_path)
    set_id = _event_set(conn, _event(conn))
    gen = os_.new_generation(conn, now=NOW)
    _stage_all(conn, set_id, gen, _bars(TS - timedelta(minutes=15), 25))
    first = os_.discard_staging_step(conn, set_id=set_id, generation=gen,
                                     max_rows=10)
    assert (first.rows, first.done) == (10, False)
    assert os_.get_set(conn, set_id)["state"] == "staging"
    assert os_.discard_staging_step(conn, set_id=set_id, generation=gen + 1,
                                    max_rows=10).status == "conflict"
    os_.discard_staging_step(conn, set_id=set_id, generation=gen, max_rows=10)
    last = os_.discard_staging_step(conn, set_id=set_id, generation=gen,
                                    max_rows=10)
    assert (last.rows, last.done) == (5, True)
    assert os_.get_set(conn, set_id)["state"] == "awaiting"


def test_generations_increase_and_old_staging_is_reclaimed(tmp_path):
    conn = _conn(tmp_path)
    set_id = _event_set(conn, _event(conn))
    os_.record_event_spread(conn, set_id, spread_pips=1.0, spread_source="quote")
    g1 = os_.new_generation(conn, now=NOW)
    rows = _bars(TS - timedelta(minutes=15), 255)
    _stage_all(conn, set_id, g1, rows)
    g2 = os_.new_generation(conn, now=NOW)
    assert g2 > g1
    # 旧世代の staging は現世代として完備にならない。
    assert os_.complete_set(conn, set_id=set_id, generation=g2,
                            now=NOW).status == "conflict"
    stale = os_.stale_staging_sets(conn, current_generation=g2)
    assert [(r["id"], r["generation"]) for r in stale] == [(set_id, g1)]
    for r in stale:
        assert os_.discard_staging(conn, set_id=r["id"],
                                   generation=r["generation"]).done
    assert os_.stale_staging_sets(conn, current_generation=g2) == []
    row = os_.get_set(conn, set_id)
    assert (row["state"], row["spread_pips"]) == ("awaiting", 1.0)
    assert os_.complete_rows(conn, set_id) is None


def test_shadow_set_is_created_in_staging_and_discard_removes_it(tmp_path):
    conn = _conn(tmp_path)
    shadow_id = _shadow(conn)
    gen = os_.new_generation(conn, now=NOW)
    start, end = NOW, NOW + timedelta(minutes=20)
    first = os_.stage_shadow_rows(conn, shadow_id=shadow_id, pair="USDJPY",
                                  range_start=start, range_end=end,
                                  generation=gen, rows=_bars(start, 12),
                                  now=NOW)
    assert first.committed
    row = os_.get_set(conn, first.set_id)
    assert (row["state"], row["generation"], row["expected_rows"],
            row["shadow_id"], row["event_id"], row["spread_pips"]) == (
        "staging", gen, 20, shadow_id, None, None)
    moved = os_.stage_shadow_rows(conn, shadow_id=shadow_id, pair="USDJPY",
                                  range_start=start,
                                  range_end=end + timedelta(minutes=1),
                                  generation=gen, rows=[], now=NOW)
    assert moved.status == "conflict"
    assert os_.discard_staging(conn, set_id=first.set_id, generation=gen).done
    assert os_.get_set(conn, first.set_id) is None
    assert ss.get(conn, shadow_id)["state"] == "pending_entry"


def test_a_shadow_range_holds_at_most_1680_minutes(tmp_path):
    conn = _conn(tmp_path)
    shadow_id = _shadow(conn)
    gen = os_.new_generation(conn, now=NOW)
    ok = os_.stage_shadow_rows(conn, shadow_id=shadow_id, pair="USDJPY",
                               range_start=NOW,
                               range_end=NOW + timedelta(minutes=1680),
                               generation=gen, rows=[], now=NOW)
    assert os_.get_set(conn, ok.set_id)["expected_rows"] == 1680
    with pytest.raises(ValueError, match="at most 1680"):
        os_.stage_shadow_rows(conn, shadow_id=_shadow(conn, n=1), pair="USDJPY",
                              range_start=NOW,
                              range_end=NOW + timedelta(minutes=1681),
                              generation=gen, rows=[], now=NOW)


def test_complete_commits_the_shadow_end_in_the_same_transaction(tmp_path):
    conn = _conn(tmp_path)
    shadow_id = _shadow(conn)
    gen = os_.new_generation(conn, now=NOW)
    start, end = NOW, NOW + timedelta(minutes=5)
    staged = os_.stage_shadow_rows(conn, shadow_id=shadow_id, pair="USDJPY",
                                   range_start=start, range_end=end,
                                   generation=gen, rows=_bars(start, 5),
                                   now=NOW)
    seen = ss.get(conn, shadow_id)["updated_at"]
    outcome = ss.ShadowOutcome.resolved(
        "tp", exit_price=150.2, pnl_pips=20.0, resolved_at=end,
        entry_price=150.0, entry_at=start)
    stale = ShadowCas(shadow_id, "pending_entry", "2000-01-01T00:00:00+00:00",
                      outcome)
    later = NOW + timedelta(days=2)
    assert os_.complete_set(conn, set_id=staged.set_id, generation=gen,
                            now=later, shadow=stale).status == "conflict"
    assert os_.get_set(conn, staged.set_id)["state"] == "staging"
    assert ss.get(conn, shadow_id)["state"] == "pending_entry"
    other = _shadow(conn, n=1)
    wrong_owner = ShadowCas(other, "pending_entry",
                            ss.get(conn, other)["updated_at"], outcome)
    assert os_.complete_set(conn, set_id=staged.set_id, generation=gen,
                            now=later, shadow=wrong_owner).status == "conflict"
    assert ss.get(conn, other)["state"] == "pending_entry"
    good = ShadowCas(shadow_id, "pending_entry", seen, outcome)
    assert os_.complete_set(conn, set_id=staged.set_id, generation=gen,
                            now=later, shadow=good).committed
    assert os_.get_set(conn, staged.set_id)["state"] == "complete"
    assert ss.get(conn, shadow_id)["result_code"] == "tp"
    with pytest.raises(ss.ShadowTransitionError):
        os_.complete_set(conn, set_id=staged.set_id, generation=gen, now=later,
                         shadow=ShadowCas(shadow_id, "resolved", seen, outcome))


def test_two_workers_complete_a_set_once(tmp_path):
    conn = _conn(tmp_path)
    set_id = _event_set(conn, _event(conn))
    gen = os_.new_generation(conn, now=NOW)
    _stage_all(conn, set_id, gen, _bars(TS - timedelta(minutes=15), 255))
    other = connect(tmp_path / "o.db")
    results = [os_.complete_set(c, set_id=set_id, generation=gen, now=NOW).status
               for c in (conn, other)]
    assert results == ["committed", "conflict"]
    other.close()


def test_unrecoverable_event_and_shadow_owners(tmp_path):
    conn = _conn(tmp_path)
    set_id = _event_set(conn, _event(conn))
    assert os_.mark_event_unrecoverable(conn, set_id=set_id, now=NOW).committed
    row = os_.get_set(conn, set_id)
    assert (row["state"], row["completed_at"]) == ("unrecoverable",
                                                   NOW.isoformat())
    assert os_.mark_event_unrecoverable(conn, set_id=set_id,
                                        now=NOW).status == "conflict"
    shadow_id = _shadow(conn)
    cas = ShadowCas(shadow_id, "pending_entry",
                    ss.get(conn, shadow_id)["updated_at"],
                    ss.ShadowOutcome.data_insufficient())
    out = os_.mark_shadow_unrecoverable(
        conn, shadow=cas, pair="USDJPY", range_start=NOW,
        range_end=NOW + timedelta(hours=1), now=NOW)
    assert out.committed
    assert os_.get_set(conn, out.set_id)["state"] == "unrecoverable"
    assert ss.get(conn, shadow_id)["result_code"] == "data_insufficient"
    assert os_.mark_shadow_unrecoverable(
        conn, shadow=cas, pair="USDJPY", range_start=NOW,
        range_end=NOW + timedelta(hours=1), now=NOW).status == "conflict"
    with pytest.raises(ValueError):
        os_.mark_shadow_unrecoverable(
            conn, shadow=replace(cas, outcome=ss.ShadowOutcome.no_fill()),
            pair="USDJPY", range_start=NOW, range_end=NOW + timedelta(hours=1),
            now=NOW)


# --- purge ---------------------------------------------------------------

def _complete_event_set(conn, eid, *, created_at):
    set_id = _event_set(conn, eid)
    gen = os_.new_generation(conn, now=NOW)
    _stage_all(conn, set_id, gen, _bars(TS - timedelta(minutes=15), 255))
    assert os_.complete_set(conn, set_id=set_id, generation=gen,
                            now=NOW).committed
    conn.execute("UPDATE ohlcv_slice_sets SET created_at=? WHERE id=?",
                 (created_at.isoformat(), set_id))
    conn.commit()
    return set_id


def test_purge_removes_rows_only_after_the_retention_days(tmp_path):
    conn = _conn(tmp_path)
    cutoff = NOW - timedelta(days=os_.RETENTION_DAYS)
    old = _complete_event_set(conn, _event(conn),
                              created_at=cutoff - timedelta(microseconds=1))
    edge = _complete_event_set(conn, _event(conn, name="PPI"),
                               created_at=cutoff)
    awaiting = _event_set(conn, _event(conn, name="GDP"))
    conn.execute("UPDATE ohlcv_slice_sets SET created_at=? WHERE id=?",
                 ((cutoff - timedelta(days=1)).isoformat(), awaiting))
    conn.execute(
        "INSERT INTO econ_reaction_values (set_id,window_min,status,range_pips,"
        "close_change_pips,abs_close_change_pips,net_abs_close_change_pips,"
        "rule_version,computed_at) VALUES (?,15,'resolved',1,1,1,0,1,?)",
        (old, NOW.isoformat()))
    conn.execute("INSERT INTO econ_reaction_keys (event_id,currency,set_id,"
                 "decided_at) SELECT event_id,'USD',id,? FROM ohlcv_slice_sets "
                 "WHERE id=?", (NOW.isoformat(), old))
    conn.commit()
    values_before = conn.execute("SELECT * FROM econ_reaction_values").fetchall()
    keys_before = conn.execute("SELECT * FROM econ_reaction_keys").fetchall()
    assert os_.purgeable_sets(conn, now=NOW, limit=10) == [old]
    assert os_.purge_step(conn, set_id=edge, now=NOW).status == "conflict"
    assert os_.purge_step(conn, set_id=awaiting, now=NOW).status == "conflict"
    first = os_.purge_step(conn, set_id=old, now=NOW)
    assert (first.rows, first.done) == (240, False)
    assert os_.get_set(conn, old)["purged_at"] is None
    last = os_.purge_step(conn, set_id=old, now=NOW)
    assert (last.rows, last.done) == (15, True)
    row = os_.get_set(conn, old)
    assert (row["state"], row["purged_at"]) == ("complete", NOW.isoformat())
    assert _rows_of(conn, old) == 0 and _rows_of(conn, edge) == 255
    assert conn.execute("SELECT * FROM econ_reaction_values").fetchall() == values_before
    assert conn.execute("SELECT * FROM econ_reaction_keys").fetchall() == keys_before
    assert os_.purgeable_sets(conn, now=NOW, limit=10) == []
    assert os_.purge_step(conn, set_id=old, now=NOW).status == "conflict"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO econ_reaction_keys (event_id,currency,set_id,"
                     "decided_at) SELECT event_id,'USD',?,? FROM "
                     "ohlcv_slice_sets WHERE id=?", (edge, NOW.isoformat(), old))
    conn.rollback()
    with pytest.raises(ValueError, match="at least"):
        os_.purgeable_sets(conn, now=NOW, limit=10, retention_days=399)


def test_purge_keeps_the_shadow_end_of_an_unrecoverable_owner(tmp_path):
    conn = _conn(tmp_path)
    shadow_id = _shadow(conn)
    cas = ShadowCas(shadow_id, "pending_entry",
                    ss.get(conn, shadow_id)["updated_at"],
                    ss.ShadowOutcome.data_insufficient())
    out = os_.mark_shadow_unrecoverable(conn, shadow=cas, pair="USDJPY",
                                        range_start=NOW,
                                        range_end=NOW + timedelta(hours=1),
                                        now=NOW)
    later = NOW + timedelta(days=os_.RETENTION_DAYS, seconds=1)
    shadow_before = ss.get(conn, shadow_id)
    assert os_.purgeable_sets(conn, now=later, limit=5) == [out.set_id]
    assert os_.purge_step(conn, set_id=out.set_id, now=later).done
    assert os_.get_set(conn, out.set_id)["purged_at"] == later.isoformat()
    assert ss.get(conn, shadow_id) == shadow_before


# --- 容量 ----------------------------------------------------------------

def test_events_per_month_derive_from_the_observed_rows():
    assert {k: os_.events_per_month(k) for k in (3, 2, 1)} == {
        3: 54, 2: 110, 1: 463}


def test_row_capacity_matches_the_fixed_example_exactly():
    assert os_.row_capacity(daily_signals=96, importance_min=3,
                            pair_count=1) == 64_693_085
    # 式: ceil(D × (S × 1680 + E × 12 / 365 × 255 × P))、exact 有理数。
    for s, imp, p in ((1, 1, 3), (500, 2, 7), (0, 3, 1)):
        e = os_.events_per_month(imp)
        numerator = 400 * (s * 1680 * 365 + e * 12 * 255 * p)
        assert os_.row_capacity(daily_signals=s, importance_min=imp,
                                pair_count=p) == -(-numerator // 365)


def test_byte_capacity_is_base_plus_rows_times_beta():
    assert os_.byte_capacity(0) == os_.SLICE_BASE_BYTES
    assert os_.byte_capacity(10, base_bytes=7, bytes_per_row=3) == 37
    c = os_.row_capacity(daily_signals=96, importance_min=3, pair_count=1)
    assert os_.byte_capacity(c) == (os_.SLICE_BASE_BYTES
                                    + c * os_.SLICE_BYTES_PER_ROW)


@pytest.mark.parametrize("override, producer, value, below", [
    (None, 96, 96, False),
    (200, 96, 200, False),
    (96, 96, 96, False),
    (95, 96, 96, True),
])
def test_effective_signal_capacity(override, producer, value, below):
    got = os_.effective_signal_capacity(override=override, producer=producer)
    assert (got.value, got.producer, got.override, got.below_producer) == (
        value, producer, override, below)


def test_startup_refuses_an_override_below_the_producer_maximum():
    os_.check_startup_capacity(override=None, producer=96)
    os_.check_startup_capacity(override=96, producer=96)
    with pytest.raises(os_.CapacityOverrideBelowProducer):
        os_.check_startup_capacity(override=95, producer=96)


def _strategy(name="a", h="h1", pairs=("USDJPY",), tf="15m"):
    return SimpleNamespace(kind="strategy", name=name, content_hash=h,
                           pairs=list(pairs), timeframe=tf)


def test_capacity_plan_changes_only_with_inventory_override_and_importance():
    base = dict(override=None, strategies=[_strategy()],
                settings_pairs=["USDJPY"], importance_min=3)
    plan = os_.capacity_plan(**base)
    assert (plan.signals.value, plan.rows) == (96, 64_693_085)
    assert plan.bytes == os_.byte_capacity(plan.rows)
    assert "signal_daily_max" not in inspect.signature(os_.capacity_plan).parameters
    assert "signal_min_interval_min" not in inspect.signature(
        os_.capacity_plan).parameters
    assert os_.capacity_plan(**{**base, "strategies": [
        _strategy(tf="1h")]}).signals.value == 24
    assert os_.capacity_plan(**{**base, "strategies": [
        _strategy(), _strategy(name="b")]}).signals.value == 192
    two = os_.capacity_plan(**{**base, "settings_pairs": ["USDJPY", "EURUSD"],
                               "strategies": [_strategy(
                                   pairs=("USDJPY", "EURUSD"))]})
    assert two.signals.value == 192 and two.rows > plan.rows
    assert os_.capacity_plan(**{**base, "override": 300}).signals.value == 300
    below = os_.capacity_plan(**{**base, "override": 10})
    assert (below.signals.value, below.signals.below_producer) == (96, True)
    assert os_.capacity_plan(**{**base, "importance_min": 1}).rows > plan.rows


@pytest.mark.parametrize("used, level", [
    (0, "ok"), (79, "ok"), (80, "warn"), (99, "warn"), (100, "full"),
    (130, "full")])
def test_capacity_level_thresholds(used, level):
    assert os_.capacity_level(used, 100) == level


def test_slice_row_count(tmp_path):
    conn = _conn(tmp_path)
    set_id = _event_set(conn, _event(conn))
    _stage_all(conn, set_id, os_.new_generation(conn, now=NOW),
               _bars(TS - timedelta(minutes=15), 7))
    assert os_.slice_row_count(conn) == 7
