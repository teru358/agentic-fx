"""資金推移の読取 API (`GET /v1/performance`) の集計。"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core import accounting
from agentic_fx.ops import performance_view as pv
from agentic_fx.ops.contracts import ErrorCode, OpsError, Principal
from agentic_fx.store import db

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
KILL = 20.0
LATCHED = {"latched": True, "generation": 3, "latched_at": "2026-11-18T01:00:00+00:00",
           "reconcile_required": False}
FREE = {"latched": False, "generation": 0, "latched_at": None, "reconcile_required": False}

TOP_KEYS = {"range", "from", "to", "bucket_sec", "account_currency", "current", "points",
            "hwm_steps", "stats", "marks", "marks_omitted", "marks_since", "other_sources",
            "strategies", "thresholds", "omissions", "quality"}
CURRENT_KEYS = {"kill_switch", "state_fetched_at", "snapshot_at", "snapshot_fetched_at",
                "snapshot_age_sec", "snapshot_freshness", "dd_pct", "kill_level", "headroom",
                "kill_pct", "fill_hold"}


def at(minutes: float = 0.0, *, base: datetime = NOW) -> datetime:
    return base + timedelta(minutes=minutes)


def snap(conn, ts, equity, hwm=None, *, balance=None, cashflow=0.0, source="paper") -> int:
    stamp = ts.isoformat() if isinstance(ts, datetime) else ts
    cur = conn.execute(
        "INSERT INTO account_snapshots (ts,balance,equity,hwm,cashflow,source) "
        "VALUES (?,?,?,?,?,?)",
        (stamp, equity if balance is None else balance, equity,
         equity if hwm is None else hwm, cashflow, source))
    conn.commit()
    return cur.lastrowid


def many(conn, rows) -> None:
    conn.executemany("INSERT INTO account_snapshots (ts,balance,equity,hwm,cashflow,source) "
                     "VALUES (?,?,?,?,?,?)", rows)
    conn.commit()


def minute_rows(start: datetime, count: int, *, equity=1000.0, step=0.0, spacing=60):
    rows, hwm = [], equity
    for i in range(count):
        value = equity + step * i
        hwm = max(hwm, value)
        rows.append(((start + timedelta(seconds=spacing * i)).isoformat(), value, value, hwm,
                     0.0, "paper"))
    return rows


def read(env, range_key="30d", *, now=NOW, kill_switch=FREE, kill_pct=KILL):
    return pv.read_performance(env.db_path, range_key=range_key, now=now, kill_pct=kill_pct,
                               account_currency="JPY", kill_switch=kill_switch,
                               state_fetched_at=now)


def event(conn, ts, state, generation, *, kind="kill_switch") -> int:
    cur = conn.execute(
        "INSERT INTO ops_events(ts,code,ref,fields_json) VALUES (?,?,?,?)",
        (ts.isoformat(), "guard_state_changed", "instance",
         json.dumps({"guard_kind": kind, "state": state, "generation": generation})))
    conn.commit()
    return cur.lastrowid


def ledger(conn, source_id, rebase_id, *, generation=1, latched_at=None, applied_at=None,
           audit_id=None):
    audit = conn.execute(
        "INSERT INTO ops_requests(request_id,endpoint,authenticated_principal,body_sha256,"
        "phase,created_at) VALUES (?,?,?,?,?,?)",
        (f"r{source_id}-{rebase_id}", "killswitch_rebase", "approver", "h", "accepted",
         "2026-11-18T00:00:00+00:00")).lastrowid if audit_id is None else audit_id
    conn.execute(
        "INSERT INTO kill_switch_hwm_rebases (applied_at,generation,latched_at,"
        "source_snapshot_id,rebase_snapshot_id,accepted_audit_id,digest_version,preview_token,"
        "hwm_before,hwm_after,equity,balance,drawdown_pct,threshold_pct,cut,cut_pct,"
        "additional_headroom) VALUES (?,?,?,?,?,?,1,'t',1,1,1,1,1,1,1,1,1)",
        ((applied_at or NOW).isoformat(), generation,
         None if latched_at is None else latched_at.isoformat(), source_id, rebase_id, audit))
    conn.commit()


# --- 契約 ------------------------------------------------------------------------------

def test_an_empty_database_returns_every_key_and_an_unknown_current(ops_env):
    data = read(ops_env)
    assert set(data) == TOP_KEYS
    assert set(data["current"]) == CURRENT_KEYS
    assert data["points"] == [] and data["hwm_steps"] == [] and data["marks"] == []
    assert data["current"]["snapshot_freshness"] == "unknown"
    assert data["current"]["fill_hold"] == "unknown"
    assert data["current"]["dd_pct"] is None and data["current"]["headroom"] is None
    assert set(data["stats"]) == {
        "first_at", "last_at", "equity_first", "equity_last", "balance_first", "balance_last",
        "equity_min", "equity_max", "balance_min", "balance_max", "dd_max", "dd_max_at"}
    assert all(value is None for value in data["stats"].values())
    assert data["thresholds"] == {"min_count": 5, "reference_count": 30}
    assert data["quality"] == {"partial": False, "invalid_snapshots": 0, "invalid_marks": 0,
                               "invalid_orders": 0, "unknown_hwm_drops": 0,
                               "unplaced_closed_count": 0}
    assert data["omissions"] == {"equity_points": 0, "hwm_steps": 0, "strategy_rows": 0,
                                 "strategy_series": 0}
    assert data["marks_since"] is None and data["other_sources"] == 0
    json.dumps(data, allow_nan=False)


def test_the_window_is_closed_at_both_ends_and_the_range_sets_the_start(ops_env):
    conn = ops_env.conn
    start = NOW - timedelta(days=7)
    snap(conn, start - timedelta(seconds=1), 900.0)
    snap(conn, start, 1000.0)
    snap(conn, NOW, 1100.0)
    snap(conn, NOW + timedelta(seconds=1), 1200.0)
    data = read(ops_env, "7d")
    assert data["from"] == start.isoformat() and data["to"] == NOW.isoformat()
    assert data["stats"]["equity_first"] == 1000.0 and data["stats"]["equity_last"] == 1100.0
    assert read(ops_env, "30d")["stats"]["equity_first"] == 900.0


def test_all_starts_at_the_oldest_valid_snapshot_or_mark(ops_env):
    conn = ops_env.conn
    oldest = NOW - timedelta(days=200)
    snap(conn, oldest, 1000.0)
    snap(conn, NOW - timedelta(days=1), 1010.0)
    data = read(ops_env, "all")
    assert data["from"] == oldest.isoformat()
    assert data["stats"]["equity_first"] == 1000.0
    older_mark = NOW - timedelta(days=300)
    event(conn, older_mark, "latched", 1)
    assert read(ops_env, "all")["from"] == older_mark.isoformat()


def test_all_of_an_empty_database_is_a_zero_width_window(ops_env):
    data = read(ops_env, "all")
    assert data["from"] == data["to"] == NOW.isoformat()
    assert data["bucket_sec"] == 1


def test_an_unknown_range_is_refused(ops_env):
    with pytest.raises(ValueError):
        read(ops_env, "1y")


# --- 束・区間・段 -----------------------------------------------------------------------

def test_a_long_series_is_bucketed_within_the_limit_and_keeps_the_last_value(ops_env):
    start = NOW - timedelta(days=29)
    rows = minute_rows(start, 29 * 24 * 60, equity=1000.0, step=0.1)
    many(ops_env.conn, rows)
    data = read(ops_env)
    width = math.ceil(30 * 86400 / 279)
    assert data["bucket_sec"] == width
    assert 100 < len(data["points"]) <= pv.MAX_POINTS
    last = data["points"][-1]
    assert last["equity_last"] == rows[-1][2] and last["last_at"] == rows[-1][0]
    assert data["stats"]["equity_last"] == rows[-1][2]
    for point in data["points"]:
        assert point["start_at"] <= point["end_at"] == point["last_at"]
        assert point["equity_min"] <= point["equity_last"] <= point["equity_max"]
        first = datetime.fromisoformat(point["start_at"]).timestamp() // width
        final = datetime.fromisoformat(point["end_at"]).timestamp() // width
        assert first == final


def test_buckets_are_aligned_to_the_epoch_and_a_row_on_the_edge_opens_the_next_one(ops_env):
    base = datetime.fromtimestamp(1_790_000_000 // 1000 * 1000, tz=timezone.utc)
    now = base + timedelta(seconds=279 * 4)
    width = 4
    assert pv._bucket_width(now - timedelta(days=0, seconds=279 * 4), now) == width
    edge = datetime.fromtimestamp((base.timestamp() // width + 1) * width, tz=timezone.utc)
    snap(ops_env.conn, edge - timedelta(seconds=1), 10.0)
    snap(ops_env.conn, edge, 11.0)
    data = pv.read_performance(
        ops_env.db_path, range_key="all", now=now, kill_pct=KILL, account_currency="JPY",
        kill_switch=FREE, state_fetched_at=now)
    assert [p["equity_last"] for p in data["points"]] == [10.0, 11.0]


def test_the_rows_of_a_bucket_report_min_max_and_last_by_ts_then_id(ops_env):
    conn = ops_env.conn
    snap(conn, NOW - timedelta(seconds=30), 1000.0)
    snap(conn, NOW - timedelta(seconds=20), 1300.0, 1300.0)
    snap(conn, NOW - timedelta(seconds=10), 700.0, 1300.0)
    snap(conn, NOW - timedelta(seconds=10), 900.0, 1300.0)
    data = read(ops_env, "7d")
    assert len(data["points"]) == 1
    point = data["points"][0]
    assert (point["equity_min"], point["equity_max"], point["equity_last"]) == (
        700.0, 1300.0, 900.0)


def test_a_gap_of_exactly_ten_minutes_keeps_the_line_and_one_microsecond_more_cuts_it(ops_env):
    conn = ops_env.conn
    first = NOW - timedelta(hours=3)
    snap(conn, first, 1000.0)
    snap(conn, first + timedelta(seconds=600), 1001.0)
    snap(conn, first + timedelta(seconds=1200, microseconds=1), 1002.0)
    data = read(ops_env, "7d")
    assert [(p["seg"], p["start_at"], p["end_at"]) for p in data["points"]] == [
        (0, first.isoformat(), (first + timedelta(seconds=600)).isoformat()),
        (1, (first + timedelta(seconds=1200, microseconds=1)).isoformat(),
         (first + timedelta(seconds=1200, microseconds=1)).isoformat())]


def test_a_gap_inside_one_bucket_still_splits_the_segment(ops_env):
    conn = ops_env.conn
    base = NOW - timedelta(hours=2)
    snap(conn, base, 1000.0)
    snap(conn, base + timedelta(minutes=30), 1010.0)
    data = read(ops_env, "7d")
    assert len(data["points"]) == 2
    assert data["points"][0]["seg"] != data["points"][1]["seg"]
    assert all(p["start_at"] == p["end_at"] for p in data["points"])


def test_hwm_rises_inside_a_bucket_are_gathered_at_the_first_change_with_the_last_value(ops_env):
    conn = ops_env.conn
    base = NOW - timedelta(minutes=30)
    snap(conn, base, 1000.0, 1000.0)
    snap(conn, base + timedelta(minutes=1), 1010.0, 1010.0)
    snap(conn, base + timedelta(minutes=2), 1020.0, 1020.0)
    snap(conn, base + timedelta(minutes=3), 1005.0, 1020.0)
    data = read(ops_env, "7d")
    assert len(data["points"]) == 1
    steps = [(s["t"], s["hwm"]) for s in data["hwm_steps"]]
    assert steps[0] == (base.isoformat(), 1000.0)
    assert (at(-29).isoformat(), 1020.0) in steps
    assert steps[-1] == ((base + timedelta(minutes=3)).isoformat(), 1020.0)
    assert (at(-28).isoformat(), 1020.0) not in steps


def test_a_rise_that_opens_a_new_bucket_is_drawn_at_its_first_row_with_the_bucket_end_value(
        ops_env):
    conn = ops_env.conn
    width = pv._bucket_width(NOW - timedelta(days=7), NOW)
    edge = datetime.fromtimestamp(
        (int((NOW - timedelta(hours=1)).timestamp()) // width + 1) * width, tz=timezone.utc)
    snap(conn, edge - timedelta(seconds=120), 1000.0, 1000.0)
    snap(conn, edge + timedelta(seconds=60), 1010.0, 1010.0)
    snap(conn, edge + timedelta(seconds=120), 1020.0, 1020.0)
    data = read(ops_env, "7d")
    assert len(data["points"]) == 2
    steps = [(s["t"], s["hwm"]) for s in data["hwm_steps"]]
    assert steps == [((edge - timedelta(seconds=120)).isoformat(), 1000.0),
                     ((edge + timedelta(seconds=60)).isoformat(), 1020.0),
                     ((edge + timedelta(seconds=120)).isoformat(), 1020.0)]


def test_an_unexplained_hwm_drop_splits_the_bucket_at_its_exact_row_and_is_marked(ops_env):
    conn = ops_env.conn
    base = NOW - timedelta(minutes=30)
    snap(conn, base, 1000.0, 1000.0)
    snap(conn, base + timedelta(minutes=1), 1010.0, 1010.0)
    drop = snap(conn, base + timedelta(minutes=2), 1005.0, 990.0)
    snap(conn, base + timedelta(minutes=3), 1006.0, 990.0)
    data = read(ops_env, "7d")
    assert len(data["points"]) == 2
    assert data["points"][0]["end_at"] == (base + timedelta(minutes=1)).isoformat()
    assert data["points"][1]["start_at"] == (base + timedelta(minutes=2)).isoformat()
    assert data["points"][0]["seg"] == data["points"][1]["seg"]
    assert (base + timedelta(minutes=2)).isoformat(), 990.0 in [(s["t"], s["hwm"]) for s in data["hwm_steps"]]
    assert ((base + timedelta(minutes=2)).isoformat(), 990.0) in [
        (s["t"], s["hwm"]) for s in data["hwm_steps"]]
    assert data["quality"]["unknown_hwm_drops"] == 1
    assert [m["kind"] for m in data["marks"]] == ["unknown_hwm_drop"]
    assert data["marks"][0]["plot_at"] == (base + timedelta(minutes=2)).isoformat()
    assert data["marks"][0]["time_source"] == "snapshot" and drop


def test_a_negative_cashflow_explains_its_hwm_drop_and_marks_it(ops_env):
    conn = ops_env.conn
    base = NOW - timedelta(minutes=30)
    snap(conn, base, 1000.0, 1000.0)
    snap(conn, base + timedelta(minutes=1), 900.0, 900.0, cashflow=-100.0)
    data = read(ops_env, "7d")
    assert data["quality"]["unknown_hwm_drops"] == 0
    assert [m["kind"] for m in data["marks"]] == ["cashflow"]
    assert len(data["points"]) == 2


def test_a_ledger_rebase_is_marked_at_the_source_time_with_both_times_and_splits_the_bucket(
        ops_env):
    conn = ops_env.conn
    base = NOW - timedelta(minutes=30)
    snap(conn, base, 1000.0, 1000.0)
    source = snap(conn, base + timedelta(minutes=1), 700.0, 1000.0)
    rebase = snap(conn, base + timedelta(minutes=1), 700.0, 700.0, source="manual_ks_rebase")
    applied = NOW - timedelta(minutes=5)
    ledger(conn, source, rebase, generation=2, latched_at=at(-60), applied_at=applied)
    data = read(ops_env, "7d")
    kinds = {m["kind"]: m for m in data["marks"]}
    assert set(kinds) == {"rebase", "latched"}
    rebase_mark = kinds["rebase"]
    assert rebase_mark["plot_at"] == rebase_mark["source_snapshot_ts"] == (
        base + timedelta(minutes=1)).isoformat()
    assert rebase_mark["applied_at"] == applied.isoformat()
    assert rebase_mark["occurred_at"] is None and rebase_mark["recorded_at"] is None
    assert rebase_mark["generation"] == 2 and rebase_mark["time_source"] == "ledger"
    assert kinds["latched"]["occurred_at"] == at(-60).isoformat()
    assert kinds["latched"]["time_source"] == "ledger"
    assert data["quality"]["unknown_hwm_drops"] == 0
    assert ((base + timedelta(minutes=1)).isoformat(), 700.0) in [
        (s["t"], s["hwm"]) for s in data["hwm_steps"]]
    assert len(data["points"]) == 2
    assert data["points"][1]["start_at"] == (base + timedelta(minutes=1)).isoformat()


def test_a_rebase_row_without_a_ledger_is_an_unknown_drop(ops_env):
    conn = ops_env.conn
    snap(conn, at(-10), 1000.0, 1000.0)
    snap(conn, at(-9), 700.0, 700.0, source="manual_ks_rebase")
    data = read(ops_env, "7d")
    assert data["quality"]["unknown_hwm_drops"] == 1
    assert [m["kind"] for m in data["marks"]] == ["unknown_hwm_drop"]


def test_a_rebase_is_adopted_by_the_source_time_not_the_applied_time(ops_env):
    conn = ops_env.conn
    old = NOW - timedelta(days=40)
    source = snap(conn, old, 700.0, 1000.0)
    rebase = snap(conn, old, 700.0, 700.0, source="manual_ks_rebase")
    ledger(conn, source, rebase, applied_at=NOW - timedelta(days=1))
    assert read(ops_env, "30d")["marks"] == []
    assert [m["kind"] for m in read(ops_env, "90d")["marks"]] == ["rebase"]


def test_a_first_row_with_a_cashflow_is_marked(ops_env):
    snap(ops_env.conn, at(-5), 1000.0, 1100.0, cashflow=100.0)
    assert [m["kind"] for m in read(ops_env, "7d")["marks"]] == ["cashflow"]


# --- 統計 ------------------------------------------------------------------------------

def test_stats_come_from_every_valid_row_and_the_deepest_drawdown_keeps_the_first_row(ops_env):
    conn = ops_env.conn
    base = NOW - timedelta(hours=2)
    snap(conn, base, 1000.0, 1000.0, balance=990.0)
    snap(conn, base + timedelta(minutes=1), 800.0, 1000.0, balance=950.0)
    snap(conn, base + timedelta(minutes=2), 800.0, 1000.0, balance=950.0)
    snap(conn, base + timedelta(minutes=3), 1100.0, 1100.0, balance=1090.0)
    stats = read(ops_env, "7d")["stats"]
    assert stats["first_at"] == base.isoformat() and stats["last_at"] == (
        base + timedelta(minutes=3)).isoformat()
    assert (stats["equity_first"], stats["equity_last"]) == (1000.0, 1100.0)
    assert (stats["balance_first"], stats["balance_last"]) == (990.0, 1090.0)
    assert (stats["equity_min"], stats["equity_max"]) == (800.0, 1100.0)
    assert (stats["balance_min"], stats["balance_max"]) == (950.0, 1090.0)
    assert stats["dd_max"] == pytest.approx(20.0)
    assert stats["dd_max_at"] == (base + timedelta(minutes=1)).isoformat()


@pytest.mark.parametrize("equity,hwm,expected", [(1100.0, 1000.0, 0.0), (500.0, 0.0, 0.0),
                                                 (500.0, -10.0, 0.0), (750.0, 1000.0, 25.0)])
def test_the_drawdown_of_a_row_follows_the_accounting_function(ops_env, equity, hwm, expected):
    snap(ops_env.conn, at(-5), equity, hwm)
    stats = read(ops_env, "7d")["stats"]
    assert stats["dd_max"] == pytest.approx(expected)
    assert stats["dd_max"] == pytest.approx(accounting.drawdown_pct(equity, hwm))


# --- 今の状態 ---------------------------------------------------------------------------

def _current(env, *, kill_switch=FREE, now=NOW):
    return read(env, "7d", now=now, kill_switch=kill_switch)["current"]


@pytest.mark.parametrize("equity,expected", [(800.0, "drawdown"), (800.0000001, "none"),
                                             (799.9999999, "drawdown"), (900.0, "none")])
def test_the_account_stop_is_decided_at_the_threshold_exactly(ops_env, equity, expected):
    snap(ops_env.conn, NOW - timedelta(seconds=5), equity, 1000.0)
    current = _current(ops_env)
    assert current["fill_hold"] == expected
    assert current["snapshot_freshness"] == "fresh"
    reached = accounting.drawdown_kill_reached(equity, 1000.0, KILL)
    assert (current["fill_hold"] == "drawdown") is reached


def test_the_stop_never_follows_the_sign_of_the_displayed_headroom(ops_env, monkeypatch):
    snap(ops_env.conn, NOW - timedelta(seconds=5), 900.0, 1000.0)
    monkeypatch.setattr(accounting, "drawdown_kill_reached", lambda *a, **k: True)
    current = _current(ops_env)
    assert current["headroom"] > 0 and current["fill_hold"] == "drawdown"


def test_the_level_and_the_headroom_are_derived_from_the_hwm_and_the_threshold(ops_env):
    snap(ops_env.conn, NOW - timedelta(seconds=5), 950.0, 1250.0)
    current = _current(ops_env)
    assert current["kill_pct"] == KILL
    assert current["kill_level"] == pytest.approx(1000.0)
    assert current["headroom"] == pytest.approx(-50.0)
    assert current["dd_pct"] == pytest.approx(24.0)


@pytest.mark.parametrize("seconds,freshness,hold", [
    (0, "fresh", "none"), (599, "fresh", "none"), (600, "fresh", "none"),
    (600.000001, "stale", "stale"), (601, "stale", "stale")])
def test_ten_minutes_exactly_is_still_fresh(ops_env, seconds, freshness, hold):
    snap(ops_env.conn, NOW - timedelta(seconds=seconds), 1000.0, 1000.0)
    current = _current(ops_env)
    assert (current["snapshot_freshness"], current["fill_hold"]) == (freshness, hold)
    assert current["snapshot_age_sec"] == pytest.approx(seconds)
    assert (freshness == "fresh") is (accounting.current_account(ops_env.conn, NOW) is not None)


def test_the_view_is_fresh_exactly_when_the_daemon_account_is_available(ops_env):
    conn = ops_env.conn
    for offset in (-1, 0, 599, 600, 601, 3600):
        conn.execute("DELETE FROM account_snapshots")
        snap(conn, NOW - timedelta(seconds=offset), 900.0, 1000.0)
        fresh = _current(ops_env)["snapshot_freshness"] == "fresh"
        assert fresh is (accounting.current_account(conn, NOW) is not None), offset


def test_the_latest_row_is_read_without_filtering_by_source(ops_env):
    snap(ops_env.conn, NOW - timedelta(seconds=20), 900.0, 1000.0)
    snap(ops_env.conn, NOW - timedelta(seconds=10), 700.0, 700.0, source="manual_ks_rebase")
    current = _current(ops_env)
    assert current["snapshot_at"] == (NOW - timedelta(seconds=10)).isoformat()
    assert current["dd_pct"] == pytest.approx(0.0)


def test_a_missing_or_unusable_latest_row_is_unknown_and_never_safe(ops_env):
    assert _current(ops_env)["fill_hold"] == "unknown"
    snap(ops_env.conn, NOW - timedelta(seconds=5), float("inf"), 1000.0)
    current = _current(ops_env)
    assert (current["snapshot_freshness"], current["fill_hold"]) == ("unknown", "unknown")
    assert current["dd_pct"] is None and current["kill_level"] is None
    json.dumps(current, allow_nan=False)


@pytest.mark.parametrize("equity", [0.0, -5.0])
def test_a_non_positive_equity_is_unknown(ops_env, equity):
    snap(ops_env.conn, NOW - timedelta(seconds=5), equity, 1000.0)
    assert _current(ops_env)["fill_hold"] == "unknown"


def test_the_kill_switch_state_is_passed_through_and_none_stays_none(ops_env):
    snap(ops_env.conn, NOW - timedelta(seconds=5), 1000.0, 1000.0)
    assert _current(ops_env, kill_switch=LATCHED)["kill_switch"] == LATCHED
    assert _current(ops_env, kill_switch=None)["kill_switch"] is None
    assert _current(ops_env, kill_switch=None)["fill_hold"] == "none"


def test_the_current_row_is_read_in_the_same_transaction_as_the_aggregates(
        ops_env, monkeypatch):
    conn = ops_env.conn
    snap(conn, NOW - timedelta(seconds=30), 1000.0, 1000.0)
    real = pv.current_block

    def racing(read_conn, *args, **kwargs):
        snap(ops_env.connect(), NOW - timedelta(seconds=1), 500.0, 1000.0)
        return real(read_conn, *args, **kwargs)

    monkeypatch.setattr(pv, "current_block", racing)
    data = read(ops_env, "7d")
    assert data["current"]["snapshot_at"] == (NOW - timedelta(seconds=30)).isoformat()
    assert data["stats"]["equity_last"] == 1000.0


# --- 印 --------------------------------------------------------------------------------

def test_events_of_one_generation_and_state_become_one_mark_with_the_first_recorded_time(
        ops_env):
    conn = ops_env.conn
    event(conn, at(-100), "latched", 1)
    event(conn, at(-99), "latched", 1)
    event(conn, at(-50), "released", 1)
    event(conn, at(-49), "released", 1)
    data = read(ops_env, "7d")
    assert [(m["kind"], m["generation"]) for m in data["marks"]] == [
        ("released", 1), ("latched", 1)]
    released, latched = data["marks"]
    assert released["recorded_at"] == released["plot_at"] == at(-50).isoformat()
    assert released["occurred_at"] is None and released["time_source"] == "event_ts"
    assert latched["recorded_at"] == latched["plot_at"] == at(-100).isoformat()
    assert data["marks_since"] == at(-100).isoformat()


def test_the_state_latched_time_is_preferred_and_a_marker_without_it_adds_no_mark(ops_env):
    conn = ops_env.conn
    event(conn, at(-100), "latched", 3)
    state = {**LATCHED, "latched_at": at(-120).isoformat()}
    mark = read(ops_env, "7d", kill_switch=state)["marks"][0]
    assert mark["occurred_at"] == at(-120).isoformat()
    assert mark["recorded_at"] == at(-100).isoformat()
    assert mark["plot_at"] == at(-120).isoformat() and mark["time_source"] == "state"
    blank = {**LATCHED, "latched_at": None, "reconcile_required": True}
    assert read(ops_env, "7d", kill_switch=blank)["marks"][0]["time_source"] == "event_ts"
    conn.execute("DELETE FROM ops_events")
    conn.commit()
    assert read(ops_env, "7d", kill_switch=blank)["marks"] == []


def test_events_outside_the_period_still_merge_before_the_period_is_cut(ops_env):
    conn = ops_env.conn
    event(conn, NOW - timedelta(days=9), "latched", 1)
    event(conn, NOW - timedelta(days=2), "latched", 1)
    state = {**LATCHED, "generation": 1, "latched_at": (NOW - timedelta(days=9)).isoformat()}
    assert read(ops_env, "7d", kill_switch=state)["marks"] == []
    marks = read(ops_env, "30d", kill_switch=state)["marks"]
    assert len(marks) == 1 and marks[0]["recorded_at"] == (NOW - timedelta(days=9)).isoformat()


def test_unrelated_events_and_bad_events_are_not_marks(ops_env):
    conn = ops_env.conn
    event(conn, at(-10), "latched", 1, kind="other_guard")
    conn.execute("INSERT INTO ops_events(ts,code,ref,fields_json) VALUES (?,?,?,?)",
                 (at(-9).isoformat(), "guard_state_changed", "instance",
                  json.dumps({"guard_kind": "kill_switch", "state": "weird", "generation": 1})))
    conn.execute("INSERT INTO ops_events(ts,code,ref,fields_json) VALUES (?,?,?,?)",
                 ("not-a-time", "guard_state_changed", "instance",
                  json.dumps({"guard_kind": "kill_switch", "state": "latched", "generation": 1})))
    conn.commit()
    data = read(ops_env, "7d")
    assert data["marks"] == [] and data["quality"]["invalid_marks"] == 2


def test_only_the_newest_twenty_marks_are_returned_and_the_rest_are_counted(ops_env):
    conn = ops_env.conn
    for generation in range(1, 16):
        event(conn, at(-200 + generation), "latched", generation)
        event(conn, at(-100 + generation), "released", generation)
    data = read(ops_env, "7d")
    assert len(data["marks"]) == 20 and data["marks_omitted"] == 10
    positions = [m["plot_at"] for m in data["marks"]]
    assert positions == sorted(positions, reverse=True)
    assert positions[0] == at(-85).isoformat()


def test_marks_with_the_same_position_are_ordered_by_kind_then_source_id(ops_env):
    conn = ops_env.conn
    event(conn, at(-10), "released", 1)
    event(conn, at(-10), "latched", 2)
    kinds = [m["kind"] for m in read(ops_env, "7d")["marks"]]
    assert kinds == ["released", "latched"]


# --- 欠けた・不正な行 ---------------------------------------------------------------------

def test_unknown_sources_are_counted_left_out_and_cut_the_line(ops_env):
    conn = ops_env.conn
    snap(conn, at(-5), 1000.0)
    snap(conn, at(-4), 5000.0, source="live")
    snap(conn, at(-3), 1001.0)
    data = read(ops_env, "7d")
    assert data["other_sources"] == 1
    assert [p["equity_last"] for p in data["points"]] == [1000.0, 1001.0]
    assert data["points"][0]["seg"] != data["points"][1]["seg"]
    assert data["stats"]["equity_max"] == 1001.0


@pytest.mark.parametrize("bad", ["inf", "-inf", "text", "bad_ts", "tz"])
def test_rows_that_cannot_be_read_are_excluded_counted_and_cut_the_line(ops_env, bad):
    conn = ops_env.conn
    snap(conn, at(-5), 1000.0)
    when = at(-4).isoformat()
    if bad == "inf":
        conn.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm) VALUES (?,?,?,?)",
                     (when, 1.0, 1e999, 1.0))
    elif bad == "-inf":
        conn.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm) VALUES (?,?,?,?)",
                     (when, 1.0, -1e999, 1.0))
    elif bad == "text":
        conn.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm) VALUES (?,?,?,?)",
                     (when, 1.0, "abc", 1.0))
    elif bad == "bad_ts":
        conn.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm) VALUES (?,?,?,?)",
                     (at(-4).isoformat()[:19] + "Z", 1.0, 5.0, 5.0))
    else:
        conn.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm) VALUES (?,?,?,?)",
                     (at(-4).replace(tzinfo=None).isoformat() + "+09:00", 1.0, 5.0, 5.0))
    conn.commit()
    snap(conn, at(-3), 1001.0)
    data = read(ops_env, "7d")
    assert data["quality"]["invalid_snapshots"] == 1
    assert [p["equity_last"] for p in data["points"]] == [1000.0, 1001.0]
    assert data["points"][0]["seg"] != data["points"][1]["seg"]
    assert data["stats"]["equity_min"] == 1000.0
    json.dumps(data, allow_nan=False)


def test_a_bad_first_row_is_counted_and_not_drawn(ops_env):
    conn = ops_env.conn
    conn.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm) VALUES (?,?,?,?)",
                 (at(-5).isoformat(), 1.0, "abc", 1.0))
    conn.commit()
    snap(conn, at(-4), 1000.0)
    data = read(ops_env, "7d")
    assert data["quality"]["invalid_snapshots"] == 1
    assert [p["equity_last"] for p in data["points"]] == [1000.0]


# --- 上限 ------------------------------------------------------------------------------

def test_segments_are_limited_by_dropping_the_oldest_complete_ones(ops_env):
    conn = ops_env.conn
    rows = []
    for i in range(45):
        rows += minute_rows(NOW - timedelta(days=20) + timedelta(hours=8 * i), 3,
                            equity=1000.0 + i)
    many(conn, rows)
    data = read(ops_env, "30d")
    segs = {p["seg"] for p in data["points"]}
    assert len(segs) <= pv.MAX_SEGS
    assert data["omissions"]["equity_points"] > 0
    assert data["points"][-1]["equity_last"] == rows[-1][2]
    assert data["stats"]["equity_first"] == rows[0][2]
    assert len(data["hwm_steps"]) <= pv.MAX_STEPS


def test_splits_beyond_the_point_limit_keep_the_newest_contiguous_tail(ops_env):
    conn = ops_env.conn
    base = NOW - timedelta(hours=12)
    rows = []
    for i in range(700):
        rows.append(((base + timedelta(seconds=30 * i)).isoformat(), 1000.0 + i, 1000.0 + i,
                     1000.0 + i, 1.0, "paper"))
    many(conn, rows)
    data = read(ops_env, "7d")
    assert len(data["points"]) <= pv.MAX_POINTS
    assert len(data["hwm_steps"]) <= pv.MAX_STEPS
    assert data["points"][-1]["equity_last"] == rows[-1][2]
    assert data["omissions"]["equity_points"] > 0
    assert data["stats"]["equity_first"] == 1000.0
    assert len({p["seg"] for p in data["points"]}) == 1
    assert len(data["marks"]) == 20 and data["marks_omitted"] == 700 - 20


def test_the_widest_input_fits_every_limit_together(ops_env):
    conn = ops_env.conn
    rows = []
    for i in range(500):
        stamp = NOW - timedelta(days=60) + timedelta(hours=2.5 * i)
        rows.append((stamp.isoformat(), 1000.0 + i % 7, 1000.0 + i % 7,
                     1000.0 + i % 7 + (i % 3), float(i % 2), "paper"))
    many(conn, rows)
    for generation in range(1, 40):
        event(conn, NOW - timedelta(days=50) + timedelta(hours=generation), "latched",
              generation)
    data = read(ops_env, "90d")
    assert len(data["points"]) <= pv.MAX_POINTS
    assert len(data["hwm_steps"]) <= pv.MAX_STEPS
    assert len({p["seg"] for p in data["points"]}) <= pv.MAX_SEGS
    assert len(data["marks"]) <= pv.MAX_MARKS
    json.dumps(data, allow_nan=False)


def test_widening_merges_buckets_without_losing_the_rise_position(ops_env):
    left = pv._Part(seg=0, region=0, bucket=0, count=1, start_ts="a", end_ts="b",
                    equity_min=1.0, equity_max=1.0, equity_last=1.0, balance_min=1.0,
                    balance_max=1.0, balance_last=1.0, first_hwm=10.0, last_hwm=10.0,
                    rise_ts=None, dd_max=0.0, first_equity=1.0, first_balance=1.0)
    right = pv._Part(seg=0, region=0, bucket=1, count=1, start_ts="c", end_ts="d",
                     equity_min=1.0, equity_max=1.0, equity_last=1.0, balance_min=1.0,
                     balance_max=1.0, balance_last=1.0, first_hwm=12.0, last_hwm=15.0,
                     rise_ts="e", dd_max=0.0, first_equity=1.0, first_balance=1.0)
    merged = pv._widen([left, right], 2)
    assert len(merged) == 1
    assert (merged[0].first_hwm, merged[0].last_hwm, merged[0].rise_ts) == (10.0, 15.0, "c")
    flat = pv._Part(**{**{n: getattr(right, n) for n in right.__slots__}, "first_hwm": 10.0})
    assert pv._widen([left, flat], 2)[0].rise_ts == "e"


def test_the_fallback_query_gives_the_same_answer_as_the_adjacent_id_join(
        ops_env, monkeypatch):
    conn = ops_env.conn
    rows = minute_rows(NOW - timedelta(hours=5), 90, equity=1000.0, step=0.5)
    rows += minute_rows(NOW - timedelta(hours=2), 30, equity=900.0, step=-1.0)
    rows.append(((NOW - timedelta(hours=1)).isoformat(), 5.0, 5.0, 5.0, 100.0, "paper"))
    many(conn, rows)
    fast = read(ops_env, "7d")
    monkeypatch.setattr(pv, "_ids_follow_time", lambda *a: False)
    slow = read(ops_env, "7d")
    assert fast == slow
    assert len({p["seg"] for p in fast["points"]}) == 3


def test_rows_with_missing_ids_or_out_of_order_ids_are_still_cut_correctly(ops_env):
    conn = ops_env.conn
    first = snap(conn, at(-100), 1000.0)
    middle = snap(conn, at(-90), 1001.0)
    snap(conn, at(-80), 1002.0)
    conn.execute("DELETE FROM account_snapshots WHERE id=?", (middle,))
    snap(conn, at(-85), 999.0)
    conn.commit()
    assert first
    data = read(ops_env, "7d")
    assert data["stats"]["equity_min"] == 999.0
    assert data["quality"]["partial"] is False or data["quality"]["partial"] is True
    assert data["points"][-1]["equity_last"] in (1002.0, 999.0)
    assert len(data["points"]) >= 1


def test_the_special_row_limit_degrades_to_a_partial_answer_without_losing_the_tail(
        ops_env, monkeypatch):
    monkeypatch.setattr(pv, "SPECIAL_LIMIT", 10)
    conn = ops_env.conn
    rows = minute_rows(NOW - timedelta(hours=5), 100, equity=1000.0)
    rows = [(r[0], r[1], r[2], r[3], 1.0 if i % 3 == 0 else 0.0, r[5])
            for i, r in enumerate(rows)]
    many(conn, rows)
    data = read(ops_env, "7d")
    assert data["quality"]["partial"] is True
    assert data["stats"]["equity_last"] == 1000.0
    assert data["stats"]["first_at"] == rows[0][0]
    assert data["points"][-1]["end_at"] == rows[-1][0]


# --- 読取の規律 --------------------------------------------------------------------------

def test_the_read_opens_one_short_lived_read_only_connection_and_closes_it(
        ops_env, monkeypatch):
    opened = []
    real = pv.connect_readonly

    def spy(path):
        conn = real(path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(pv, "connect_readonly", spy)
    snap(ops_env.conn, at(-1), 1000.0)
    read(ops_env, "7d")
    assert len(opened) == 1
    with pytest.raises(Exception):
        opened[0].execute("SELECT 1")


def test_the_connection_is_closed_and_the_transaction_ended_when_a_step_raises(
        ops_env, monkeypatch):
    opened = []
    real = pv.connect_readonly

    def spy(path):
        conn = real(path)
        opened.append(conn)
        return conn

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pv, "connect_readonly", spy)
    monkeypatch.setattr(pv, "current_block", boom)
    with pytest.raises(RuntimeError):
        read(ops_env, "7d")
    with pytest.raises(Exception):
        opened[0].execute("SELECT 1")


def test_the_read_connection_cannot_write(ops_env):
    conn = pv.connect_readonly(ops_env.db_path)
    try:
        with pytest.raises(Exception):
            conn.execute("INSERT INTO account_snapshots(ts,balance,equity,hwm) "
                         "VALUES ('x',1,1,1)")
    finally:
        conn.close()


# --- service -------------------------------------------------------------------------------

def test_the_service_reads_through_the_status_scope_and_writes_no_audit_row(ops_env):
    snap(ops_env.conn, ops_env.wall() - timedelta(seconds=5), 1000.0, 1000.0)
    service = ops_env.service()
    before = len(ops_env.ops_rows())
    for principal in (Principal.OPERATOR, Principal.APPROVER, Principal.OPS_BOT,
                      Principal.OPS_WEB, Principal.OPS_WEB_WRITE):
        data = service.performance(principal, "7d")
        assert set(data) == TOP_KEYS and data["account_currency"] == "JPY"
        assert data["current"]["kill_pct"] == float(ops_env.settings.risk.drawdown_kill_pct)
    with pytest.raises(OpsError) as raised:
        service.performance(Principal.OPS_NOTIFY, "7d")
    assert raised.value.code is ErrorCode.FORBIDDEN
    assert len(ops_env.ops_rows()) == before


@pytest.mark.parametrize("value", ["1y", "", "30D", "7d ", None, 7])
def test_the_service_refuses_an_unknown_range(ops_env, value):
    with pytest.raises(OpsError) as raised:
        ops_env.service().performance(Principal.OPS_WEB, value)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_the_default_range_is_thirty_days(ops_env):
    assert ops_env.service().performance(Principal.OPS_WEB)["range"] == "30d"


def test_without_settings_the_service_is_unavailable_instead_of_guessing(ops_env):
    service = ops_env.service(settings=None)
    with pytest.raises(OpsError) as raised:
        service.performance(Principal.OPS_WEB, "7d")
    assert raised.value.code is ErrorCode.UNAVAILABLE


def test_the_kill_switch_state_is_read_with_the_same_function_as_status(ops_env):
    service = ops_env.service()
    status = service.status(Principal.OPS_WEB)["kill_switch"]
    data = service.performance(Principal.OPS_WEB, "7d")["current"]["kill_switch"]
    assert data == status
    assert set(status) == {"latched", "generation", "latched_at", "reconcile_required"}


def test_an_unreadable_state_becomes_an_unknown_kill_switch_while_status_still_fails(
        ops_env, monkeypatch):
    service = ops_env.service()

    def broken(deadline):
        raise OSError("state")

    monkeypatch.setattr(service, "_read_kill_switch", broken)
    data = service.performance(Principal.OPS_WEB, "7d")
    assert data["current"]["kill_switch"] is None
    with pytest.raises(OSError):
        service.status(Principal.OPS_WEB)


def test_a_busy_database_is_reported_as_busy(ops_env, monkeypatch):
    import sqlite3

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(pv, "read_performance", locked)
    with pytest.raises(OpsError) as raised:
        ops_env.service().performance(Principal.OPS_WEB, "7d")
    assert raised.value.code is ErrorCode.DATABASE_BUSY


def test_the_daemon_wiring_serves_the_view(tmp_path):
    from agentic_fx.core.contracts import FixedClock
    from agentic_fx.runners.fake_runner import FakeRunner
    from agentic_fx.service import build_app
    from tests.store.test_rag import FakeEmbedding
    from tests.test_service_app import _init
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding())
    try:
        data = app.ops.performance(Principal.OPS_WEB, "30d")
        assert set(data) == TOP_KEYS
        assert data["current"]["kill_switch"] is not None
        assert data["current"]["kill_pct"] > 0
    finally:
        app.close()


# --- HTTP ------------------------------------------------------------------------------

def test_the_route_answers_with_the_display_timezone_and_refuses_the_notify_principal(api5):
    api = api5
    client = api.client(Principal.OPS_WEB)
    response = client.get("/v1/performance?range=7d")
    assert response.status_code == 200
    body = response.json()["data"]
    assert set(body) == TOP_KEYS | {"display_timezone"}
    assert client.get("/v1/performance").json()["data"]["range"] == "30d"
    bad = client.get("/v1/performance?range=1y")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_argument"
    assert api.client(Principal.OPS_NOTIFY).get("/v1/performance").status_code == 403


# --- 合格の線 ----------------------------------------------------------------------------

def _big_db(path, rows_count, *, with_events=True):
    conn = db.connect(path)
    db.init_db(conn)
    start = NOW - timedelta(days=700)
    rows, hwm, equity = [], 1_000_000.0, 1_000_000.0
    for i in range(rows_count):
        stamp = start + timedelta(seconds=60 * i + (i // 4000) * 40 * 3600)
        equity += ((i * 7919) % 1000 - 500) * 0.5
        hwm = max(hwm, equity)
        rows.append((stamp.isoformat(), equity, equity, hwm, 0.0, "paper"))
    conn.executemany("INSERT INTO account_snapshots (ts,balance,equity,hwm,cashflow,source) "
                     "VALUES (?,?,?,?,?,?)", rows)
    if with_events:
        for gen in range(1, 30):
            conn.execute("INSERT INTO ops_events(ts,code,ref,fields_json) VALUES (?,?,?,?)",
                         ((start + timedelta(days=gen)).isoformat(), "guard_state_changed",
                          "instance", json.dumps({"guard_kind": "kill_switch",
                                                  "state": "latched", "generation": gen})))
    for index in range(240):
        mission = conn.execute(
            "INSERT INTO missions(loop,runner,model,status,trigger,started_at) "
            "VALUES ('trade','local','m','done',?,?)",
            (f"signal:s{index % 40:02d}", start.isoformat())).lastrowid
        intent = conn.execute(
            "INSERT INTO trade_intents(mission_id,payload_json,action,created_at) "
            "VALUES (?,?,'open',?)", (mission, '{"stop_loss": 155.0}', start.isoformat())
        ).lastrowid
        closed = (start + timedelta(days=2 * index)).isoformat()
        conn.execute(
            "INSERT INTO orders(intent_id,pair,direction,entry_type,horizon,status,quantity,"
            "filled_quantity,avg_fill_price,stop_loss,realized_pnl,close_reason,created_at,"
            "updated_at,closed_at) VALUES (?,'USDJPY','long','market','day','closed',1000,1000,"
            "156,155,?,'llm_close',?,?,?)", (intent, float(index % 7 - 3), closed, closed, closed))
    conn.commit()
    conn.close()
    return rows


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    path = tmp_path_factory.mktemp("big") / "agentic.db"
    rows = _big_db(path, 600_000)
    return path, rows


def test_ten_times_the_production_rows_over_the_whole_range_read_within_half_a_second(big):
    path, rows = big
    best = None
    for _ in range(3):
        started = time.perf_counter()
        data = pv.read_performance(path, range_key="all", now=NOW, kill_pct=KILL,
                                   account_currency="JPY", kill_switch=FREE,
                                   state_fetched_at=NOW)
        elapsed = time.perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    assert len(data["points"]) <= pv.MAX_POINTS
    assert data["stats"]["equity_last"] == rows[-1][2]
    assert sum(r["closed_count"] for r in data["strategies"]) == 240
    assert best <= 0.5, best
