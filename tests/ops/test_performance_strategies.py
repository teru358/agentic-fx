"""戦略ごとの成績 (起動理由ごとの帰属・指標・R・行の合算・累積損益)。"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.backtest import metrics as backtest_metrics
from agentic_fx.core.contracts import OrderStatus
from agentic_fx.ops import performance_view as pv

from .test_performance_view import FREE, KILL, NOW, read, snap

ENTRY = 156.0
STOP = 155.0
QTY = 1000.0


def mission(conn, trigger) -> int:
    cur = conn.execute(
        "INSERT INTO missions(loop,runner,model,status,trigger,started_at) "
        "VALUES ('trade','local','m','done',?,?)", (trigger, NOW.isoformat()))
    conn.commit()
    return cur.lastrowid


def intent(conn, mission_id, *, stop=STOP, payload=None) -> int:
    body = payload if payload is not None else json.dumps({"action": "open", "stop_loss": stop})
    cur = conn.execute(
        "INSERT INTO trade_intents(mission_id,payload_json,action,created_at) "
        "VALUES (?,?,'open',?)", (mission_id, body, NOW.isoformat()))
    conn.commit()
    return cur.lastrowid


def order(conn, intent_id, *, status="closed", pnl=500.0, closed=None, pair="USDJPY",
          stop=STOP, avg=ENTRY, qty=QTY, reason="llm_close", filled=None, direction="long"):
    closed_at = (NOW - timedelta(hours=1)) if closed is None else closed
    cur = conn.execute(
        "INSERT INTO orders(intent_id,pair,direction,entry_type,horizon,status,quantity,"
        "filled_quantity,avg_fill_price,stop_loss,realized_pnl,close_reason,created_at,"
        "updated_at,closed_at) VALUES (?,?,?,'market','day',?,?,?,?,?,?,?,?,?,?)",
        (intent_id, pair, direction, status, qty, qty if filled is None else filled, avg, stop,
         pnl, reason, NOW.isoformat(), NOW.isoformat(),
         closed_at.isoformat() if isinstance(closed_at, datetime) else closed_at))
    conn.commit()
    return cur.lastrowid


def put(conn, trigger="signal:alpha", **kwargs):
    return order(conn, intent(conn, mission(conn, trigger), stop=kwargs.get("stop", STOP)),
                 **kwargs)


def rows(env, range_key="30d"):
    return {r["origin"]: r for r in read(env, range_key)["strategies"]}


def test_no_orders_means_no_rows(ops_env):
    assert read(ops_env)["strategies"] == []


def test_the_row_is_decided_by_the_mission_that_built_the_position(ops_env):
    conn = ops_env.conn
    built = mission(conn, "signal:alpha")
    cron = mission(conn, "cron")
    intent(conn, cron)
    order(conn, intent(conn, built))
    data = rows(ops_env)
    assert set(data) == {"signal:alpha"}
    assert data["signal:alpha"]["closed_count"] == 1


@pytest.mark.parametrize("trigger,expected", [
    ("cron", "cron"), ("signal:beta_2", "signal:beta_2"), (None, "unknown"),
    ("", "unknown"), ("Signal:x", "unknown"), ("signal:", "unknown"),
    ("signal:" + "a" * 70, "unknown"), ("improve", "unknown")])
def test_the_origin_follows_the_grammar_and_anything_else_is_unknown(ops_env, trigger, expected):
    put(ops_env.conn, trigger)
    assert set(rows(ops_env)) == {expected}


def test_an_order_without_an_intent_or_with_a_missing_mission_is_unknown_and_counted(ops_env):
    conn = ops_env.conn
    order(conn, None)
    conn.execute("PRAGMA foreign_keys=OFF")
    orphan = conn.execute(
        "INSERT INTO trade_intents(mission_id,payload_json,action,created_at) "
        "VALUES (9999,'{}','open',?)", (NOW.isoformat(),)).lastrowid
    conn.commit()
    order(conn, orphan)
    data = rows(ops_env)
    assert set(data) == {"unknown"} and data["unknown"]["closed_count"] == 2


EXPECTED = {
    "closed": (1, 0, 0), "open": (0, 1, 0), "protection_pending": (0, 1, 0),
    "closing": (0, 0, 1), "close_unknown": (0, 0, 1), "cancelling": (0, 1, 0),
    "cancel_unknown": (0, 1, 0)}


@pytest.mark.parametrize("status", [s.value for s in OrderStatus])
def test_every_order_status_is_classified_by_what_it_says_about_the_position(ops_env, status):
    put(ops_env.conn, status=status, pnl=500.0 if status == "closed" else None, filled=QTY)
    data = rows(ops_env).get("signal:alpha")
    if status not in EXPECTED:
        assert data is None, status
        return
    assert (data["closed_count"], data["open_count"], data["close_pending_count"]) == (
        EXPECTED[status])


@pytest.mark.parametrize("status", ["cancelling", "cancel_unknown"])
def test_a_cancelling_order_holds_only_when_something_was_filled(ops_env, status):
    put(ops_env.conn, status=status, pnl=None, filled=0.0)
    assert rows(ops_env) == {}


def test_the_status_table_covers_exactly_the_order_status_vocabulary():
    assert {s.value for s in OrderStatus} >= set(pv._ORDER_STATUSES)
    assert set(pv._ORDER_STATUSES) == set(EXPECTED)


def test_holding_and_pending_rows_do_not_depend_on_the_period(ops_env):
    conn = ops_env.conn
    put(conn, "signal:old", status="open", pnl=None)
    conn.execute("UPDATE orders SET created_at='2020-01-01T00:00:00+00:00'")
    conn.commit()
    data = rows(ops_env, "7d")
    assert data["signal:old"]["open_count"] == 1 and data["signal:old"]["closed_count"] == 0
    assert data["signal:old"]["win_rate"] is None and data["signal:old"]["total_pnl"] is None


def test_only_settlements_inside_the_closed_window_count_and_unplaced_ones_are_reported(
        ops_env):
    conn = ops_env.conn
    start = NOW - timedelta(days=7)
    put(conn, closed=start - timedelta(seconds=1))
    put(conn, closed=start)
    put(conn, closed=NOW)
    put(conn, closed=NOW + timedelta(seconds=1))
    put(conn, closed="garbage")
    order(conn, intent(conn, mission(conn, "cron")), closed=None)
    conn.execute("UPDATE orders SET closed_at=NULL WHERE id=(SELECT MAX(id) FROM orders)")
    conn.commit()
    data = read(ops_env, "7d")
    assert {r["origin"]: r["closed_count"] for r in data["strategies"]} == {"signal:alpha": 2}
    assert data["quality"]["unplaced_closed_count"] == 2


def test_win_rate_pf_and_totals_follow_the_backtest_definitions_on_the_same_rows(ops_env):
    conn = ops_env.conn
    cases = [(900.0, STOP, ENTRY), (-400.0, STOP, ENTRY), (0.0, STOP, ENTRY),
             (-100.0, 155.5, ENTRY), (250.0, STOP, 156.5)]
    for pnl, stop, avg in cases:
        put(conn, pnl=pnl, stop=stop, avg=avg)
    row = rows(ops_env)["signal:alpha"]
    closed = [dict(r) for r in conn.execute("SELECT * FROM orders WHERE status='closed'")]
    total, profit, loss, wins, r_values = backtest_metrics._closed_metrics(closed)
    assert row["wins"] == wins == 2 and row["losses"] == 2 and row["flats"] == 1
    assert row["win_rate"] == wins / len(closed)
    assert row["pf"] == profit / loss
    assert row["total_pnl"] == total
    assert row["avg_r"] == pytest.approx(sum(r_values) / len(r_values))
    assert (row["pnl_known_count"], row["r_known_count"]) == (5, 5)
    assert (row["pnl_unknown_count"], row["r_unknown_count"]) == (0, 0)


def test_the_profit_factor_has_no_value_without_a_loss_and_rates_are_ratios(ops_env):
    put(ops_env.conn, pnl=100.0)
    row = rows(ops_env)["signal:alpha"]
    assert row["pf"] is None and row["win_rate"] == 1.0


def test_a_settlement_without_a_result_is_counted_but_never_a_win_or_a_loss(ops_env):
    conn = ops_env.conn
    put(conn, pnl=None)
    put(conn, pnl=300.0)
    row = rows(ops_env)["signal:alpha"]
    assert (row["closed_count"], row["pnl_known_count"], row["pnl_unknown_count"]) == (2, 1, 1)
    assert (row["r_known_count"], row["r_unknown_count"]) == (1, 1)
    assert row["wins"] + row["losses"] + row["flats"] == 1
    assert row["win_rate"] == 1.0 and row["total_pnl"] == 300.0
    ops_env.conn.execute("DELETE FROM orders WHERE realized_pnl IS NOT NULL")
    ops_env.conn.commit()
    row = rows(ops_env)["signal:alpha"]
    assert row["win_rate"] is None and row["total_pnl"] is None and row["avg_r"] is None


@pytest.mark.parametrize("case", [
    "other_currency", "unknown_pair", "payload_missing_sl", "payload_bad_json",
    "sl_mismatch", "sl_equals_fill", "zero_qty", "nonfinite_pnl", "null_sl"])
def test_r_is_unknown_when_its_conditions_do_not_hold_but_the_result_stays(ops_env, case):
    conn = ops_env.conn
    kwargs = {"pnl": 200.0}
    payload = None
    if case == "other_currency":
        kwargs["pair"] = "EURUSD"
    elif case == "unknown_pair":
        kwargs["pair"] = "GBPJPY"
    elif case == "payload_missing_sl":
        payload = json.dumps({"action": "open"})
    elif case == "payload_bad_json":
        payload = "{not json"
    elif case == "sl_mismatch":
        payload = json.dumps({"action": "open", "stop_loss": 150.0})
    elif case == "sl_equals_fill":
        kwargs["stop"] = ENTRY
        payload = json.dumps({"action": "open", "stop_loss": ENTRY})
    elif case == "zero_qty":
        kwargs["qty"] = 0.0
    elif case == "nonfinite_pnl":
        kwargs["pnl"] = float("inf")
    elif case == "null_sl":
        kwargs["stop"] = None
    mission_id = mission(conn, "signal:alpha")
    order(conn, intent(conn, mission_id, payload=payload), **kwargs)
    data = read(ops_env)
    row = data["strategies"][0]
    assert row["r_known_count"] == 0 and row["avg_r"] is None
    assert row["closed_count"] == 1
    if case == "nonfinite_pnl":
        assert row["pnl_known_count"] == 0
    else:
        assert row["pnl_known_count"] == 1 and row["total_pnl"] == 200.0
    counted = data["quality"]["invalid_orders"]
    if case in ("other_currency", "unknown_pair", "sl_equals_fill", "zero_qty"):
        assert counted == (1 if case == "zero_qty" else 0)
    else:
        assert counted == 1
    json.dumps(data, allow_nan=False)


def test_r_uses_the_money_formula_with_the_contract_size_for_the_matching_currency(ops_env):
    put(ops_env.conn, pnl=1000.0, qty=0.5)
    risk = abs(ENTRY - STOP) * 0.5 * 100_000
    assert rows(ops_env)["signal:alpha"]["avg_r"] == pytest.approx(1000.0 / risk)


def test_a_non_default_account_currency_flips_which_pairs_have_an_r(ops_env):
    put(ops_env.conn, pair="EURUSD", pnl=10.0, qty=1.0, avg=1.1, stop=1.09)
    from agentic_fx.ops import performance_view as module
    data = module.read_performance(
        ops_env.db_path, range_key="30d", now=NOW, kill_pct=KILL, account_currency="USD",
        kill_switch=FREE, state_fetched_at=NOW)
    assert data["strategies"][0]["r_known_count"] == 1


def test_close_reasons_are_counted_in_four_buckets_and_unknown_words_are_other(ops_env):
    conn = ops_env.conn
    for reason in ("llm_close", "llm_close", "sl", "tp", "day_rollover", None):
        put(conn, reason=reason)
    assert rows(ops_env)["signal:alpha"]["close_reasons"] == {
        "llm_close": 2, "sl": 1, "tp": 1, "other": 2}


def test_the_thresholds_are_shipped_as_data(ops_env):
    assert read(ops_env)["thresholds"] == {"min_count": 5, "reference_count": 30}
    assert (pv.MIN_COUNT, pv.REFERENCE_COUNT) == (5, 30)


def test_rows_are_ordered_by_settlements_then_name(ops_env):
    conn = ops_env.conn
    for trigger, count in (("signal:b", 2), ("signal:a", 2), ("cron", 3), ("signal:c", 1)):
        for _ in range(count):
            put(conn, trigger)
    assert [r["origin"] for r in read(ops_env)["strategies"]] == [
        "cron", "signal:a", "signal:b", "signal:c"]


# --- 累積損益 ---------------------------------------------------------------------------

def test_the_cumulative_line_steps_at_each_settlement_and_ends_at_the_total(ops_env):
    conn = ops_env.conn
    base = NOW - timedelta(days=2)
    put(conn, pnl=100.0, closed=base)
    put(conn, pnl=-30.0, closed=base + timedelta(hours=1))
    put(conn, pnl=5.0, closed=base + timedelta(hours=1))
    data = read(ops_env, "7d")
    row = data["strategies"][0]
    assert row["cumulative"] == [
        {"t": data["from"], "pnl": 0.0}, {"t": base.isoformat(), "pnl": 100.0},
        {"t": (base + timedelta(hours=1)).isoformat(), "pnl": 75.0},
        {"t": data["to"], "pnl": 75.0}]
    assert row["cumulative"][-1]["pnl"] == row["total_pnl"]


def test_only_the_newest_hundred_and_twenty_settlements_are_drawn_and_the_rest_is_the_start(
        ops_env):
    conn = ops_env.conn
    base = NOW - timedelta(days=5)
    for i in range(130):
        put(conn, pnl=float(i + 1), closed=base + timedelta(minutes=i))
    data = read(ops_env, "7d")
    series = data["strategies"][0]["cumulative"]
    assert len(series) == 122
    earlier = sum(range(1, 11))
    assert series[0] == {"t": (base + timedelta(minutes=10)).isoformat(), "pnl": float(earlier)}
    assert series[1]["t"] == (base + timedelta(minutes=10)).isoformat()
    assert series[-1] == {"t": data["to"], "pnl": data["strategies"][0]["total_pnl"]}


def test_the_newest_hundred_and_twenty_are_chosen_among_all_placeable_settlements(ops_env):
    conn = ops_env.conn
    base = NOW - timedelta(days=5)
    for i in range(120):
        put(conn, pnl=float(i + 1), closed=base + timedelta(minutes=i))
    put(conn, pnl=None, closed=base + timedelta(minutes=500))
    data = read(ops_env, "7d")
    row = data["strategies"][0]
    series = row["cumulative"]
    assert row["closed_count"] == 121 and row["pnl_unknown_count"] == 1
    omitted = row["closed_count"] - pv.SERIES_EVENTS
    assert omitted == 1
    assert series[0] == {"t": (base + timedelta(minutes=1)).isoformat(), "pnl": 1.0}
    assert len(series) == 1 + 119 + 1
    assert series[-1] == {"t": data["to"], "pnl": row["total_pnl"]}
    assert series[-2]["pnl"] == series[-1]["pnl"]


def test_exactly_a_hundred_and_twenty_settlements_keep_the_period_start(ops_env):
    conn = ops_env.conn
    base = NOW - timedelta(days=5)
    for i in range(120):
        put(conn, pnl=1.0, closed=base + timedelta(minutes=i))
    data = read(ops_env, "7d")
    series = data["strategies"][0]["cumulative"]
    assert len(series) == 122 and series[0] == {"t": data["from"], "pnl": 0.0}


def test_a_row_with_no_known_result_draws_a_flat_zero_line(ops_env):
    put(ops_env.conn, pnl=None)
    series = read(ops_env)["strategies"][0]["cumulative"]
    assert [p["pnl"] for p in series] == [0.0, 0.0]


def test_only_four_strategies_and_the_cron_row_are_drawn_and_the_rest_are_counted(ops_env):
    conn = ops_env.conn
    for index, trigger in enumerate(["signal:a", "signal:b", "signal:c", "signal:d",
                                     "signal:e", "cron", None]):
        for _ in range(10 - index):
            put(conn, trigger)
    data = read(ops_env)
    drawn = {r["origin"] for r in data["strategies"] if r["cumulative"]}
    assert drawn == {"signal:a", "signal:b", "signal:c", "signal:d", "cron"}
    assert data["omissions"]["strategy_series"] == 2


# --- 行の上限と合算 ----------------------------------------------------------------------

def test_more_than_thirty_two_rows_keep_cron_and_unknown_and_fold_the_rest_without_loss(
        ops_env):
    conn = ops_env.conn
    total_closed = total_pnl = 0
    for index in range(40):
        for _ in range(1 + index % 3):
            put(conn, f"signal:s{index:02d}", pnl=10.0 + index)
            total_closed += 1
            total_pnl += 10.0 + index
    put(conn, "cron", pnl=1.0)
    put(conn, None, pnl=2.0)
    total_closed += 2
    total_pnl += 3.0
    data = read(ops_env)
    shown = data["strategies"]
    assert len(shown) == pv.MAX_STRATEGY_ROWS
    origins = [r["origin"] for r in shown]
    assert "cron" in origins and "unknown" in origins and origins[-1] == "other"
    other = shown[-1]
    assert other["origin_count"] == data["omissions"]["strategy_rows"] == 42 - 31
    assert sum(r["closed_count"] for r in shown) == total_closed
    assert sum(r["total_pnl"] for r in shown) == pytest.approx(total_pnl)
    assert other["cumulative"] == []
    assert other["win_rate"] == 1.0
    assert other["pnl_known_count"] == other["closed_count"]


def test_folded_rates_come_from_the_folded_counts(ops_env):
    conn = ops_env.conn
    for index in range(34):
        put(conn, f"signal:s{index:02d}", pnl=10.0)
    for index in range(34):
        put(conn, f"signal:s{index:02d}", pnl=-5.0)
    other = read(ops_env)["strategies"][-1]
    assert other["origin"] == "other"
    assert other["pf"] == 2.0 and other["win_rate"] == 0.5
    assert other["avg_r"] == pytest.approx(2.5e-8)


def test_the_widest_input_with_every_limit_at_once_stays_within_the_limits(ops_env):
    conn = ops_env.conn
    base = NOW - timedelta(days=20)
    for index in range(45):
        for i in range(25):
            put(conn, f"signal:s{index:02d}", pnl=float(i - 10),
                closed=base + timedelta(minutes=index * 100 + i))
    put(conn, "cron", pnl=1.0)
    put(conn, None, pnl=1.0)
    data = read(ops_env)
    assert len(data["strategies"]) <= pv.MAX_STRATEGY_ROWS
    for row in data["strategies"]:
        assert len(row["cumulative"]) <= 122
    assert sum(1 for r in data["strategies"] if r["cumulative"]) <= pv.MAX_SERIES + 1
    json.dumps(data, allow_nan=False)


def test_all_starts_at_the_oldest_settlement_when_it_is_older_than_everything_else(ops_env):
    conn = ops_env.conn
    old = NOW - timedelta(days=400)
    put(conn, closed=old)
    snap(conn, NOW - timedelta(days=1), 1000.0)
    data = read(ops_env, "all")
    assert data["from"] == old.isoformat()
    assert data["strategies"][0]["closed_count"] == 1


@pytest.mark.parametrize("bad", ["0000-bad", "garbage", ""])
def test_all_ignores_unreadable_settlement_times_and_counts_them_as_unplaced(ops_env, bad):
    conn = ops_env.conn
    old = NOW - timedelta(days=400)
    put(conn, closed=bad)
    put(conn, closed=old)
    put(conn, closed=NOW - timedelta(days=1))
    data = read(ops_env, "all")
    assert data["from"] == old.isoformat()
    assert data["strategies"][0]["closed_count"] == 2
    assert data["quality"]["unplaced_closed_count"] == 1


def test_a_settlement_time_that_sorts_below_the_window_is_still_counted_when_unreadable(
        ops_env):
    conn = ops_env.conn
    put(conn, closed="0000-bad")
    put(conn, closed=NOW - timedelta(days=1))
    data = read(ops_env, "7d")
    assert data["strategies"][0]["closed_count"] == 1
    assert data["quality"]["unplaced_closed_count"] == 1


def test_a_settlement_with_a_readable_non_utc_offset_is_placed_by_its_real_time(ops_env):
    conn = ops_env.conn
    inside = (NOW - timedelta(days=1)).astimezone(timezone(timedelta(hours=9)))
    outside = (NOW - timedelta(days=30)).astimezone(timezone(timedelta(hours=9)))
    put(conn, closed=inside.isoformat())
    put(conn, closed=outside.isoformat())
    data = read(ops_env, "7d")
    assert data["strategies"][0]["closed_count"] == 1
    assert data["quality"]["unplaced_closed_count"] == 0


def test_a_sum_that_overflows_is_reported_as_unknown_not_as_infinity(ops_env):
    put(ops_env.conn, pnl=1e308)
    put(ops_env.conn, pnl=1e308)
    data = read(ops_env)
    json.dumps(data, allow_nan=False)
    row = data["strategies"][0]
    assert row["total_pnl"] is None and row["pf"] is None
