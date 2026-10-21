"""取引 event の値の境界と sink の失敗経路 (grammar の型、組み立ての列選択、再試行の種別、rollback、停止時の接続)。"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone

import pytest

from agentic_fx.ops import trade_event_sink as sink_mod
from agentic_fx.ops import trade_events
from agentic_fx.ops.trade_event_sink import TradeEventSink
from agentic_fx.store.db import connect, init_db

WALL = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
ORDER_FIELDS = {"state": "placed", "pair": "USDJPY", "side": "long", "entry_type": "limit",
                "qty": "0.1", "price": "150.123", "sl": "149.8", "tp": None,
                "origin_code": "cron", "mode": "paper", "state_mode": None, "reason_code": None}


def row(**over):
    base = {"id": 7, "pair": "USDJPY", "direction": "long", "entry_type": "limit",
            "quantity": 0.1, "requested_price": 148.2, "avg_fill_price": 148.25,
            "close_price": 148.75, "stop_loss": 147.75, "take_profit": 149.5,
            "realized_pnl": 50.0, "close_reason": "tp_hit",
            "filled_at": "2026-10-20T00:00:00+00:00", "closed_at": "2026-10-20T01:00:00+00:00"}
    base.update(over)
    return base


def build(to_status, **over):
    return trade_events.build_event(row(**over), to_status, trigger="cron",
                                    state_mode="learning", ccy="JPY")


class StrSub(str):
    pass


@pytest.mark.parametrize("bad", [[], {}, StrSub("long")])
def test_enum_check_rejects_non_str_values_without_raising(bad):
    assert trade_events.valid_fields("order_state_changed", {**ORDER_FIELDS, "side": bad}) is False


def test_format_price_rejects_bool():
    with pytest.raises(trade_events.InvalidValue):
        trade_events.format_price(True)


def test_held_seconds_floors_fractions_and_ignores_naive_aware_mix():
    assert trade_events.held_seconds("2026-10-20T00:00:00+00:00",
                                     "2026-10-20T00:00:59.900000+00:00") == 59
    assert trade_events.held_seconds("2026-10-20T00:00:00",
                                     "2026-10-20T00:00:59+00:00") is None


def test_unparsable_non_null_timestamp_is_an_invalid_value_not_none():
    for bad in ("not-a-time", 123, b"2026-10-20T00:00:00+00:00"):
        with pytest.raises(trade_events.InvalidValue):
            build("closed", filled_at=bad)
        with pytest.raises(trade_events.InvalidValue):
            build("closed", closed_at=bad)
    # NULL は従来どおり held_sec None のまま event を作る
    assert build("closed", filled_at=None)[2]["held_sec"] is None
    assert build("closed", closed_at=None)[2]["held_sec"] is None


def test_close_event_without_stop_loss_keeps_the_event_with_r_none():
    code, ref, fields = build("closed", stop_loss=None)
    assert code == "position_closed" and ref == "order:7"
    assert fields["pnl_r"] is None and fields["pnl"] == "50.00"


def test_close_event_with_overflowing_r_keeps_the_event_with_r_none():
    # risk が極小の決済は R が grammar の上限を超える。pnl と他の field は保ったまま R だけ None
    code, ref, fields = build("closed", stop_loss=148.2499999)
    assert code == "position_closed" and ref == "order:7"
    assert fields["pnl_r"] is None and fields["pnl"] == "50.00"
    assert fields["entry_price"] == "148.25" and fields["close_price"] == "148.75"
    assert trade_events.valid_fields(code, fields)


def test_close_event_qty_is_the_quantity_not_a_price():
    fields = build("closed")[2]
    assert fields["qty"] == "0.1"
    assert fields["entry_price"] == "148.25" and fields["close_price"] == "148.75"


def test_only_cancelled_orders_carry_a_reason():
    assert build("expired", close_reason="should_not_leak")[2]["reason_code"] is None
    assert build("cancelled", close_reason="user_cancel")[2]["reason_code"] == "user_cancel"


def test_build_event_defaults_to_paper_mode():
    assert build("open")[2]["mode"] == "paper"
    assert build("closed")[2]["mode"] == "paper"


# ---- sink ----------------------------------------------------------------------


class Mono:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    conn = connect(path)
    init_db(conn)
    conn.close()
    return path


def make(db_path, **kw):
    # busy の期限は注入した時計で測る。待つ test は進む時計を使う
    kw.setdefault("monotonic", time.monotonic if kw.get("busy_retry_sec") else Mono())
    return TradeEventSink(db_path, wall_clock=lambda: WALL, **kw)


def wait_until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def sqlite_error(code, message="x"):
    exc = sqlite3.OperationalError(message)
    exc.sqlite_errorcode = code
    return exc


def test_health_is_ok_exactly_at_the_recovery_boundary(db_path):
    mono = Mono()
    sink = make(db_path, monotonic=mono)
    sink.record_drop("queue_drop")
    mono.t += 60.0
    assert sink.health() == "ok"


def test_first_drop_warns_even_when_the_monotonic_clock_is_small(db_path, caplog):
    sink = make(db_path, monotonic=Mono(10.0))
    with caplog.at_level(logging.WARNING):
        sink.record_drop("queue_drop")
    assert any("queue_drop" in r.getMessage() for r in caplog.records)


def test_locked_is_retried_like_busy(db_path):
    sink = make(db_path, busy_retry_sec=5.0)
    sink.start()
    real = sink._insert
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise sqlite_error(sqlite3.SQLITE_LOCKED, "database table is locked")
        return real(*a, **k)

    sink._insert = flaky
    try:
        sink.submit("order_state_changed", "order:1", dict(ORDER_FIELDS))
        assert wait_until(lambda: len(calls) >= 3)
        assert wait_until(lambda: sink.worker_alive())
    finally:
        sink.stop()
    assert sink.counters()["db_busy_drop"] == 0 and sink.counters()["shutdown_drop"] == 0
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM ops_events WHERE code='order_state_changed'"
                        ).fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize("code", [261, 517, 262], ids=["busy_recovery", "busy_snapshot",
                                                         "locked_sharedcache"])
def test_extended_busy_codes_are_retried_like_busy(db_path, code):
    assert code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    sink = make(db_path, busy_retry_sec=5.0)
    sink._conn = connect(db_path)
    real = sink._insert
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise sqlite_error(code, "database is busy")
        return real(*a, **k)

    sink._insert = flaky
    sink._write((WALL.isoformat(), "order_state_changed", "order:1", dict(ORDER_FIELDS)))
    assert len(calls) == 3
    counters = sink.counters()
    assert counters["db_busy_drop"] == 0 and counters["db_error_drop"] == 0
    sink._conn.close()


def test_extended_non_busy_code_is_still_an_error_without_retry(db_path):
    sink = make(db_path, busy_retry_sec=5.0)
    sink._conn = connect(db_path)
    calls = []

    def failing(*a, **k):
        calls.append(1)
        raise sqlite_error(sqlite3.SQLITE_IOERR_READ, "disk I/O error")

    sink._insert = failing
    sink._write((WALL.isoformat(), "order_state_changed", "order:1", dict(ORDER_FIELDS)))
    assert len(calls) == 1
    assert sink.counters()["db_error_drop"] == 1 and sink.counters()["db_busy_drop"] == 0
    sink._conn.close()


class Ticking:
    """呼ぶたびに進む偽の単調時計。実時間は進めない。"""

    def __init__(self, step):
        self.t, self.step, self.calls = 1000.0, step, 0

    def __call__(self):
        self.calls += 1
        self.t += self.step
        return self.t


def test_busy_deadline_is_measured_by_the_injected_monotonic_clock(db_path, monkeypatch):
    monkeypatch.setattr(sink_mod, "backoff_sec", lambda attempt, rng=None: 0.0)
    clock = Ticking(step=0.4)
    sink = make(db_path, busy_retry_sec=1.0, monotonic=clock)
    sink._conn = connect(db_path)
    calls = []

    def busy(*a, **k):
        calls.append(1)
        raise sqlite_error(sqlite3.SQLITE_BUSY, "database is locked")

    sink._insert = busy
    started = time.monotonic()
    sink._write((WALL.isoformat(), "order_state_changed", "order:1", dict(ORDER_FIELDS)))
    assert time.monotonic() - started < 0.5  # 実時間の 1 秒を待たない
    assert sink.counters()["db_busy_drop"] == 1
    assert 1 <= len(calls) <= 4
    sink._conn.close()


def test_non_busy_sqlite_error_is_dropped_without_retry(db_path):
    sink = make(db_path, busy_retry_sec=5.0)
    sink._conn = connect(db_path)
    calls = []

    def failing(*a, **k):
        calls.append(1)
        raise sqlite_error(sqlite3.SQLITE_IOERR, "disk I/O error")

    sink._insert = failing
    started = time.monotonic()
    sink._write((WALL.isoformat(), "order_state_changed", "order:1", dict(ORDER_FIELDS)))
    assert time.monotonic() - started < 1.0
    assert len(calls) == 1
    assert sink.counters()["db_error_drop"] == 1 and sink.counters()["db_busy_drop"] == 0
    sink._conn.close()


def test_non_sqlite_exception_counts_db_error_drop(db_path):
    sink = make(db_path)
    sink._conn = connect(db_path)

    def failing(*a, **k):
        raise RuntimeError("boom")

    sink._insert = failing
    sink._write((WALL.isoformat(), "order_state_changed", "order:1", dict(ORDER_FIELDS)))
    assert sink.counters()["db_error_drop"] == 1 and sink.counters()["observer_error"] == 0
    sink._conn.close()


def _insert_then_fail(sink):
    def failing(*a, **k):
        sink.connection.execute(
            "INSERT INTO ops_events(ts,code,ref,fields_json) VALUES ('t','x','instance','{}')")
        raise sqlite_error(sqlite3.SQLITE_IOERR, "disk I/O error")
    return failing


def test_write_failure_after_the_insert_rolls_the_transaction_back(db_path):
    sink = make(db_path)
    sink._conn = connect(db_path)
    sink._insert = _insert_then_fail(sink)
    sink._write((WALL.isoformat(), "order_state_changed", "order:1", dict(ORDER_FIELDS)))
    assert sink.connection.in_transaction is False
    sink._conn.close()


def test_health_notification_failure_rolls_the_transaction_back(db_path):
    sink = make(db_path)
    sink._conn = connect(db_path)
    sink.record_drop("observer_error")
    sink._insert = _insert_then_fail(sink)
    sink.poll_health()
    assert sink.connection.in_transaction is False
    sink._conn.close()


def test_stop_with_a_stuck_worker_leaves_the_connection_open(db_path):
    release = threading.Event()
    sink = make(db_path, join_timeout=0.2)
    real = sink._insert

    def stalled(*a, **k):
        release.wait(10)
        return real(*a, **k)

    sink._insert = stalled
    sink.start()
    sink.submit("order_state_changed", "order:1", dict(ORDER_FIELDS))
    time.sleep(0.05)
    sink.stop()
    try:
        assert sink._conn is not None  # 動いている worker がまだ使う接続を閉じない
    finally:
        release.set()
    assert wait_until(lambda: not sink.worker_alive())
    sink.stop()
    assert sink._conn is None
