"""全 set_gate_result call site の reject_category を 1 site 1 行で pin する
(プラン 9 Task 17 / 設計書 D3)。site 識別子は「現在行 + 関数名 + 直前条件」。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from agentic_fx.core.contracts import ConversionRate, Origin, TradeIntent
from agentic_fx.core.executor import CloseSnapshot, ExecutionSnapshot
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import missions, orders

from tests.core.test_executor import (
    NOW, QUOTE, _open_intent as _legacy_open_intent, _setup,
)
from tests.core.test_executor_snapshot import (
    SPECS, _close_intent, _insert_intent, _insert_open_order, _make_executor,
    _open_intent, _start_trade_mission,
)
from tests.loops.test_trade_loop import _loop


def _last(conn):
    return conn.execute("SELECT MAX(id) FROM trade_intents").fetchone()[0]


# ---- record_and_validate_intent -------------------------------------------

def route_origin_non_scheduler(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    ex.handle_intent(_legacy_open_intent(origin=Origin.ASK), mid)
    return conn, _last(conn)


def route_non_trade_mission(tmp_path):
    conn, ex, _, _ = _setup(tmp_path)
    mid = missions.start(conn, "ask", "local", "m", NOW)
    ex.handle_intent(_legacy_open_intent(), mid)
    return conn, _last(conn)


# ---- _open (legacy 一体経路) ----------------------------------------------

def route_legacy_open_no_account(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    conn.execute("DELETE FROM account_snapshots")
    conn.commit()
    ex.handle_intent(_legacy_open_intent(), mid)
    return conn, _last(conn)


def route_legacy_open_bad_conversion(tmp_path):
    def bad_rate_fn(ccy, account_ccy, now, **_kw):
        raise DataUnhealthy(f"no rate for {ccy}")
    conn, ex, _, mid = _setup(tmp_path, rate_fn=bad_rate_fn)
    ex.handle_intent(_legacy_open_intent(), mid)
    return conn, _last(conn)


def route_open_risk_gate_reject(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    ex.handle_intent(_legacy_open_intent(take_profit=148.30), mid)
    return conn, _last(conn)


def route_open_accepted(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    ex.handle_intent(_legacy_open_intent(), mid)
    return conn, _last(conn)


# ---- open_from_snapshot (commit-core) -------------------------------------

def _open_snapshot(intent, *, captured_at=NOW, specs=None, rates=None):
    spec = SPECS[intent.pair]
    return ExecutionSnapshot(
        quote=QUOTE, spec=spec,
        specs_by_pair=specs if specs is not None else {intent.pair: spec},
        rates=rates if rates is not None else {
            "USD": ConversionRate(148.51, "USD", "JPY", (NOW,)),
            "JPY": ConversionRate(1.0, "JPY", "JPY", (NOW,)),
            "EUR": ConversionRate(160.0, "EUR", "JPY", (NOW,))},
        captured_at=captured_at)


def route_open_snapshot_stale(tmp_path):
    ex = _make_executor(tmp_path / "a")
    mid = _start_trade_mission(ex.conn)
    intent = _open_intent()
    iid = _insert_intent(ex.conn, mid, intent)
    snap = _open_snapshot(intent, captured_at=NOW - timedelta(seconds=999))
    ex.open_from_snapshot(intent, iid, snap, max_snapshot_age_sec=5)
    return ex.conn, iid


def route_open_snapshot_no_account(tmp_path):
    ex = _make_executor(tmp_path / "a")
    mid = _start_trade_mission(ex.conn)
    intent = _open_intent()
    iid = _insert_intent(ex.conn, mid, intent)
    snap = _open_snapshot(intent)
    ex.conn.execute("DELETE FROM account_snapshots")
    ex.conn.commit()
    ex.open_from_snapshot(intent, iid, snap, max_snapshot_age_sec=5)
    return ex.conn, iid


def route_open_exposure_not_covered(tmp_path):
    ex = _make_executor(tmp_path / "a")
    mid = _start_trade_mission(ex.conn)
    intent = _open_intent(pair="USDJPY")
    iid = _insert_intent(ex.conn, mid, intent)
    snap = _open_snapshot(intent)              # EURUSD を含まない snapshot
    _insert_open_order(ex.conn, "EURUSD")      # snapshot 後に exposure が増えた
    ex.open_from_snapshot(intent, iid, snap, max_snapshot_age_sec=5)
    return ex.conn, iid


def route_open_pair_not_covered(tmp_path):
    ex = _make_executor(tmp_path / "a")
    mid = _start_trade_mission(ex.conn)
    intent = _open_intent(pair="USDJPY")
    iid = _insert_intent(ex.conn, mid, intent)
    snap = _open_snapshot(intent,
                          specs={"EURUSD": SPECS["EURUSD"]})   # USDJPY spec 欠落
    ex.open_from_snapshot(intent, iid, snap, max_snapshot_age_sec=5)
    return ex.conn, iid


def route_open_currency_not_covered(tmp_path):
    ex = _make_executor(tmp_path / "a")
    mid = _start_trade_mission(ex.conn)
    intent = _open_intent(pair="USDJPY")
    iid = _insert_intent(ex.conn, mid, intent)
    snap = _open_snapshot(intent, rates={})   # USD/JPY レート欠落
    ex.open_from_snapshot(intent, iid, snap, max_snapshot_age_sec=5)
    return ex.conn, iid


# ---- close_intent (legacy) -------------------------------------------------

def route_legacy_close_not_open(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    ex.handle_intent(TradeIntent.from_llm_dict(
        {"action": "close", "order_id": 999999}, origin=Origin.SCHEDULER), mid)
    return conn, _last(conn)


def route_legacy_close_accepted(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    out = ex.handle_intent(_legacy_open_intent(
        entry_type="market", limit_price=None, expires_in=None,
        stop_loss=148.00, take_profit=149.60), mid)
    ex.handle_intent(TradeIntent.from_llm_dict(
        {"action": "close", "order_id": out["order_id"]},
        origin=Origin.SCHEDULER), mid)
    return conn, _last(conn)


# ---- close_from_snapshot (commit-core) -------------------------------------

def _close_snapshot(row, *, order_id=None, pair=None, captured_at=NOW):
    return CloseSnapshot(
        order_id=order_id if order_id is not None else row["id"],
        pair=pair if pair is not None else row["pair"],
        price=148.49, spec=SPECS[row["pair"]],
        rate=ConversionRate(1.0, "JPY", "JPY", (NOW,)),
        rate_degraded=False, captured_at=captured_at)


def route_close_snapshot_not_open(tmp_path):
    ex = _make_executor(tmp_path / "a")
    mid = _start_trade_mission(ex.conn)
    row = _insert_open_order(ex.conn, "USDJPY")
    ex.conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (row["id"],))
    ex.conn.commit()
    intent = _close_intent(row["id"])
    iid = _insert_intent(ex.conn, mid, intent)
    ex.close_from_snapshot(intent, iid, _close_snapshot(row),
                           max_snapshot_age_sec=5)
    return ex.conn, iid


def route_close_snapshot_missing(tmp_path):
    ex = _make_executor(tmp_path / "a")
    mid = _start_trade_mission(ex.conn)
    row = _insert_open_order(ex.conn, "USDJPY")
    intent = _close_intent(row["id"])
    iid = _insert_intent(ex.conn, mid, intent)
    ex.close_from_snapshot(intent, iid, None, max_snapshot_age_sec=5)
    return ex.conn, iid


def route_close_snapshot_stale(tmp_path):
    ex = _make_executor(tmp_path / "a")
    mid = _start_trade_mission(ex.conn)
    row = _insert_open_order(ex.conn, "USDJPY")
    intent = _close_intent(row["id"])
    iid = _insert_intent(ex.conn, mid, intent)
    ex.close_from_snapshot(
        intent, iid,
        _close_snapshot(row, captured_at=NOW - timedelta(seconds=999)),
        max_snapshot_age_sec=5)
    return ex.conn, iid


def route_close_snapshot_accepted(tmp_path):
    ex = _make_executor(tmp_path / "a")
    mid = _start_trade_mission(ex.conn)
    row = _insert_open_order(ex.conn, "USDJPY")
    intent = _close_intent(row["id"])
    iid = _insert_intent(ex.conn, mid, intent)
    ex.close_from_snapshot(intent, iid, _close_snapshot(row),
                           max_snapshot_age_sec=5)
    return ex.conn, iid


def route_close_snapshot_mismatch(tmp_path):
    """`close_order_from_snapshot` の同一性検査は
    `snapshot.order_id != row["id"] or snapshot.pair != row["pair"]` なので、
    order_id を変えるだけで `SnapshotCoverageError` に到達する (実測)。"""
    ex = _make_executor(tmp_path / "a")
    mid = _start_trade_mission(ex.conn)
    row = _insert_open_order(ex.conn, "USDJPY")
    intent = _close_intent(row["id"])
    iid = _insert_intent(ex.conn, mid, intent)
    ex.close_from_snapshot(intent, iid,
                           _close_snapshot(row, order_id=row["id"] + 12345),
                           max_snapshot_age_sec=5)
    return ex.conn, iid


# ---- cancel_intent ---------------------------------------------------------

def route_cancel_not_pending(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    ex.handle_intent(TradeIntent.from_llm_dict(
        {"action": "cancel", "order_id": 999999}, origin=Origin.SCHEDULER), mid)
    return conn, _last(conn)


def route_cancel_pending(tmp_path):
    conn, ex, _, mid = _setup(tmp_path)
    out = ex.handle_intent(_legacy_open_intent(), mid)      # limit → pending_fill
    assert orders.get(conn, out["order_id"])["status"] == "pending_fill"
    ex.handle_intent(TradeIntent.from_llm_dict(
        {"action": "cancel", "order_id": out["order_id"]},
        origin=Origin.SCHEDULER), mid)
    return conn, _last(conn)


# ---- trade_loop commit-pre snapshot failure --------------------------------

def route_commit_pre_snapshot_failure(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed",
        {"action": "open", "pair": "USDJPY", "direction": "long",
         "entry_type": "market", "horizon": "day", "stop_loss": 148.00,
         "take_profit": 149.60, "reasoning": "t"}, [])])

    def boom(*a, **k):
        raise DataUnhealthy("gather failed")
    loop.executor.gather_open_snapshot = boom
    loop.run_once()
    return conn, _last(conn)


@pytest.mark.parametrize(
    ("site", "route", "expected"),
    [
        ("executor.py:451 record_and_validate_intent/origin is not SCHEDULER",
         route_origin_non_scheduler, "origin"),
        ("executor.py:461 record_and_validate_intent/loop != trade",
         route_non_trade_mission, "mission"),
        ("executor.py:492 _open/account is None",
         route_legacy_open_no_account, "risk_gate"),
        ("executor.py:509 _open/except DataUnhealthy",
         route_legacy_open_bad_conversion, "risk_gate"),
        ("executor.py:534 _evaluate_and_execute_open/not result.accepted",
         route_open_risk_gate_reject, "risk_gate"),
        ("executor.py:546 _evaluate_and_execute_open/accepted",
         route_open_accepted, None),
        ("executor.py:700 open_from_snapshot/age_sec > max",
         route_open_snapshot_stale, "execution"),
        ("executor.py:709 open_from_snapshot/account is None",
         route_open_snapshot_no_account, "risk_gate"),
        ("executor.py:723 open_from_snapshot/except SnapshotCoverageError",
         route_open_exposure_not_covered, "execution"),
        ("executor.py:744 open_from_snapshot/spec is None",
         route_open_pair_not_covered, "execution"),
        ("executor.py:757 open_from_snapshot/rate is None",
         route_open_currency_not_covered, "execution"),
        ("executor.py:831 close_intent/row not open",
         route_legacy_close_not_open, "execution"),
        ("executor.py:837 close_intent/accepted",
         route_legacy_close_accepted, None),
        ("executor.py:950 close_from_snapshot/row not open",
         route_close_snapshot_not_open, "execution"),
        ("executor.py:961 close_from_snapshot/snapshot is None",
         route_close_snapshot_missing, "execution"),
        ("executor.py:973 close_from_snapshot/age_sec > max",
         route_close_snapshot_stale, "execution"),
        ("executor.py:979 close_from_snapshot/accepted",
         route_close_snapshot_accepted, None),
        ("executor.py:986 close_from_snapshot/except SnapshotCoverageError",
         route_close_snapshot_mismatch, "execution"),
        ("executor.py:1025 cancel_intent/row not pending_fill",
         route_cancel_not_pending, "execution"),
        ("executor.py:1031 cancel_intent/accepted",
         route_cancel_pending, None),
        ("trade_loop.py:322 _run_once_impl/snapshot_error is not None",
         route_commit_pre_snapshot_failure, "execution"),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_every_set_gate_result_site_persists_category(
        tmp_path, site, route, expected):
    conn, iid = route(tmp_path)
    row = conn.execute(
        "SELECT gate_result,reject_category FROM trade_intents WHERE id=?",
        (iid,),
    ).fetchone()
    assert row is not None, f"{site} did not persist its intent"
    assert row["reject_category"] == expected
    assert row["gate_result"] == ("accepted" if expected is None else "rejected")
