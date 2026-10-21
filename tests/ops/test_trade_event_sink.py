"""sink: 専用接続・queue・counter・health (AC-12〜15b)。"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone

import pytest

from agentic_fx.ops import trade_event_sink as sink_mod
from agentic_fx.ops.trade_event_sink import BUSY_RETRY_SEC, COUNTERS, TradeEventSink
from agentic_fx.store.db import connect, init_db

WALL = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
FIELDS = {"state": "placed", "pair": "USDJPY", "side": "long", "entry_type": "limit",
          "qty": "0.1", "price": "150.123", "sl": "149.8", "tp": None,
          "origin_code": "cron", "mode": "paper", "state_mode": None, "reason_code": None}


class Mono:
    def __init__(self):
        self.t = 1000.0

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
    kw.setdefault("monotonic", Mono())
    return TradeEventSink(db_path, wall_clock=lambda: WALL, **kw)


def rows(db_path, code=None):
    conn = connect(db_path)
    try:
        q = "SELECT id,code,ref,fields_json FROM ops_events"
        args = ()
        if code:
            q += " WHERE code=?"
            args = (code,)
        return [dict(r) for r in conn.execute(q + " ORDER BY id", args)]
    finally:
        conn.close()


def wait_until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def test_counters_are_the_seven_reasons():
    assert COUNTERS == ("queue_drop", "observer_error", "db_busy_drop", "db_error_drop",
                        "invalid_value_drop", "worker_death", "shutdown_drop")


def test_event_is_written_by_the_worker_and_flushed_on_stop(db_path):
    sink = make(db_path)
    sink.start()
    for i in range(5):
        sink.submit("order_state_changed", f"order:{i + 1}", dict(FIELDS))
    sink.stop()
    got = rows(db_path, "order_state_changed")
    assert [r["ref"] for r in got] == [f"order:{i}" for i in range(1, 6)]
    assert json.loads(got[0]["fields_json"]) == FIELDS
    assert all(v == 0 for v in sink.counters().values())


def test_ac14_connection_has_zero_busy_timeout_and_insert_commit_only(db_path):
    sink = make(db_path)
    sink.start()
    try:
        assert sink.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 0
        statements = []
        sink.connection.set_trace_callback(statements.append)
        sink.submit("order_state_changed", "order:1", dict(FIELDS))
        assert wait_until(lambda: len(rows(db_path)) == 1)
    finally:
        sink.stop()
    body = [s.strip().split()[0].upper() for s in statements if s.strip()]
    assert body == ["BEGIN", "INSERT", "COMMIT"]


def test_ac14_busy_db_drops_immediately_without_waiting(db_path):
    holder = connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    sink = make(db_path)
    sink.start()
    try:
        started = time.monotonic()
        sink.submit("order_state_changed", "order:1", dict(FIELDS))
        assert wait_until(lambda: sink.counters()["db_busy_drop"] == 1)
        assert time.monotonic() - started < 2.0
    finally:
        sink.stop()
        holder.rollback()
        holder.close()
    assert rows(db_path) == []
    assert sink.counters()["db_error_drop"] == 0


def test_busy_retry_lets_the_worker_wait_without_ever_blocking_the_submitter(db_path):
    holder = connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    sink = make(db_path, busy_retry_sec=5.0)
    sink.start()
    try:
        started = time.monotonic()
        sink.submit("order_state_changed", "order:1", dict(FIELDS))
        assert time.monotonic() - started < 0.5  # 投入側は待たない
        time.sleep(0.15)
        assert sink.counters()["db_busy_drop"] == 0
        holder.rollback()  # lock を放す
        assert wait_until(lambda: len(rows(db_path, "order_state_changed")) == 1)
    finally:
        sink.stop()
        holder.close()
    assert sink.counters()["db_busy_drop"] == 0


def test_busy_retry_gives_up_after_its_budget(db_path):
    holder = connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    sink = make(db_path, busy_retry_sec=0.1)
    sink.start()
    try:
        sink.submit("order_state_changed", "order:1", dict(FIELDS))
        assert wait_until(lambda: sink.counters()["db_busy_drop"] == 1)
    finally:
        holder.rollback()
        sink.stop()
        holder.close()
    assert rows(db_path, "order_state_changed") == []


def test_ac14_submit_does_not_wait_for_a_stalled_worker(db_path):
    release = threading.Event()
    sink = make(db_path, queue_max=10)
    real = sink._insert

    def stalled(*a, **k):
        release.wait(10)
        return real(*a, **k)

    sink._insert = stalled
    sink.start()
    try:
        started = time.monotonic()
        for i in range(5):
            sink.submit("order_state_changed", f"order:{i + 1}", dict(FIELDS))
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
        sink.stop()


def test_ac13_full_queue_drops_without_blocking(db_path):
    sink = make(db_path, queue_max=1)  # worker は未起動
    sink.submit("order_state_changed", "order:1", dict(FIELDS))
    sink.submit("order_state_changed", "order:2", dict(FIELDS))
    assert sink.counters()["queue_drop"] == 1
    sink.start()
    sink.stop()
    assert [r["ref"] for r in rows(db_path, "order_state_changed")] == ["order:1"]


def test_ac12_insert_failure_is_dropped_counted_and_degrades(db_path, caplog):
    conn = connect(db_path)
    conn.execute("DROP TABLE ops_events")
    conn.commit()
    conn.close()
    sink = make(db_path)
    sink.start()
    with caplog.at_level(logging.WARNING):
        sink.submit("order_state_changed", "order:1", dict(FIELDS))
        assert wait_until(lambda: sink.counters()["db_error_drop"] == 1)
    sink.stop()
    assert sink.health() == "degraded"
    assert any("db_error_drop" in r.getMessage() for r in caplog.records)
    # 再試行されない
    assert sink.counters()["db_error_drop"] == 1


def test_ac12_invalid_event_is_never_stored(db_path):
    sink = make(db_path)
    sink.start()
    sink.submit("order_state_changed", "order:1", {**FIELDS, "qty": 0.1})
    sink.stop()
    assert rows(db_path, "order_state_changed") == []
    assert sink.counters()["invalid_value_drop"] == 1


def test_ac15_stop_flushes_remaining_queue(db_path):
    sink = make(db_path, queue_max=100)
    for i in range(30):
        sink.submit("order_state_changed", f"order:{i + 1}", dict(FIELDS))
    sink.start()
    sink.stop()
    assert len(rows(db_path)) == 30
    assert sink.counters()["shutdown_drop"] == 0


def test_ac15_stop_is_bounded_and_counts_shutdown_drop(db_path):
    release = threading.Event()
    sink = make(db_path, queue_max=100, join_timeout=0.3)
    real = sink._insert

    def stalled(*a, **k):
        release.wait(10)
        return real(*a, **k)

    sink._insert = stalled
    sink.start()
    for i in range(6):
        sink.submit("order_state_changed", f"order:{i + 1}", dict(FIELDS))
    time.sleep(0.05)
    started = time.monotonic()
    sink.stop()
    assert time.monotonic() - started < 3.0
    # worker が抱えた 1 件も、保存されたか分からないので数える
    assert sink.counters()["shutdown_drop"] == 6
    release.set()


def test_submit_after_stop_counts_shutdown_drop(db_path):
    sink = make(db_path)
    sink.start()
    sink.stop()
    sink.submit("order_state_changed", "order:1", dict(FIELDS))
    assert sink.counters()["shutdown_drop"] == 1
    assert rows(db_path) == []


def test_ac15_worker_death_is_counted_and_later_events_are_queue_drops(db_path):
    sink = make(db_path)

    class Fatal(BaseException):
        pass

    def die(*a, **k):
        raise Fatal()

    sink._insert = die
    sink.start()
    sink.submit("order_state_changed", "order:1", dict(FIELDS))
    assert wait_until(lambda: sink.counters()["worker_death"] == 1)
    assert wait_until(lambda: not sink.worker_alive())
    sink.submit("order_state_changed", "order:2", dict(FIELDS))
    assert sink.counters()["queue_drop"] == 1
    assert sink.health() == "degraded"
    sink.stop()


def test_ac15b_health_recovers_after_sixty_quiet_seconds_and_notifies_on_change(db_path):
    mono = Mono()
    sink = make(db_path, monotonic=mono, queue_max=1)
    sink.submit("order_state_changed", "order:1", dict(FIELDS))
    sink.submit("order_state_changed", "order:2", dict(FIELDS))  # queue_drop
    assert sink.health() == "degraded"
    mono.t += 59.9
    assert sink.health() == "degraded"
    mono.t += 0.2
    assert sink.health() == "ok"
    mono.t += 1000
    assert sink.health() == "ok"


def test_ac15b_health_notifications_only_on_state_change(db_path):
    mono = Mono()
    sink = make(db_path, monotonic=mono)
    sink.start()
    try:
        sink.record_drop("observer_error")
        sink.poll_health()
        sink.record_drop("observer_error")  # 続いている間は繰り返さない
        sink.poll_health()
        sink.poll_health()
        mono.t += 61
        sink.poll_health()
        sink.poll_health()
        mono.t += 5
        sink.record_drop("observer_error")  # 再 degraded
        sink.poll_health()
    finally:
        sink.stop()
    got = [json.loads(r["fields_json"]) for r in rows(db_path, "service_health_changed")]
    assert [g["health_code"] for g in got] == ["degraded", "ok", "degraded"]
    assert all(g["component_code"] == "trade_event_sink" for g in got)
    assert {r["ref"] for r in rows(db_path, "service_health_changed")} == {"instance"}


def test_ac15b_drop_warning_is_throttled_per_reason(db_path, caplog):
    mono = Mono()
    sink = make(db_path, monotonic=mono)
    with caplog.at_level(logging.WARNING):
        for _ in range(50):
            sink.record_drop("queue_drop")
        mono.t += 61
        sink.record_drop("queue_drop")
        sink.record_drop("observer_error")
    warnings = [r.getMessage() for r in caplog.records if "trade_event" in r.getMessage()]
    assert sum("queue_drop" in w for w in warnings) == 2
    assert sum("observer_error" in w for w in warnings) == 1
    assert sink.counters()["queue_drop"] == 51


def test_unknown_reason_is_rejected(db_path):
    sink = make(db_path)
    with pytest.raises(ValueError):
        sink.record_drop("nonsense")


def test_health_event_failure_does_not_count_as_drop(db_path):
    conn = connect(db_path)
    conn.execute("DROP TABLE ops_events")
    conn.commit()
    conn.close()
    sink = make(db_path)
    sink.start()
    try:
        sink.record_drop("observer_error")
        sink.poll_health()
        before = sink.counters()
        sink.poll_health()
        assert sink.counters() == before
    finally:
        sink.stop()


def test_busy_retry_default_budget_is_one_second_and_service_passes_it():
    import inspect

    from agentic_fx import service
    assert BUSY_RETRY_SEC == 1.0
    assert "busy_retry_sec=BUSY_RETRY_SEC" in inspect.getsource(service._start_trade_events)


def test_backoff_starts_at_5ms_caps_at_50ms_with_20pct_jitter():
    class Rng:
        def __init__(self, v):
            self.v = v

        def uniform(self, a, b):
            assert (a, b) == (-0.2, 0.2)
            return self.v * 0.2

    for v in (-1, 0, 1):
        got = [sink_mod.backoff_sec(n, Rng(v)) for n in range(12)]
        base = [0.005, 0.01, 0.02, 0.04] + [0.05] * 8
        for g, b in zip(got, base):
            assert g == pytest.approx(b * (1 + 0.2 * v))
    # 実乱数でも範囲内
    for n in range(12):
        base = min(0.005 * 2 ** n, 0.05)
        for _ in range(50):
            assert base * 0.8 <= sink_mod.backoff_sec(n) <= base * 1.2


def test_busy_retry_uses_the_backoff_not_a_fixed_step(db_path, monkeypatch):
    delays = []
    monkeypatch.setattr(sink_mod, "backoff_sec", lambda n, rng=None: delays.append(n) or 0.001)
    holder = connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    sink = make(db_path, busy_retry_sec=0.1)
    sink.start()
    try:
        sink.submit("order_state_changed", "order:1", dict(FIELDS))
        assert wait_until(lambda: sink.counters()["db_busy_drop"] == 1)
    finally:
        holder.rollback()
        sink.stop()
        holder.close()
    assert delays[:4] == [0, 1, 2, 3]


def test_busy_retry_is_bounded_by_the_deadline_even_with_long_backoff(db_path):
    holder = connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    sink = make(db_path, busy_retry_sec=0.3)
    sink.start()
    try:
        started = time.monotonic()
        sink.submit("order_state_changed", "order:1", dict(FIELDS))
        assert wait_until(lambda: sink.counters()["db_busy_drop"] == 1)
        assert time.monotonic() - started < 0.3 + 0.2
    finally:
        holder.rollback()
        sink.stop()
        holder.close()


def test_stop_request_ends_the_busy_wait_as_shutdown_drop(db_path):
    holder = connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    sink = make(db_path, busy_retry_sec=30.0, join_timeout=5.0)
    sink.start()
    try:
        sink.submit("order_state_changed", "order:1", dict(FIELDS))
        time.sleep(0.15)
        started = time.monotonic()
        sink.stop()
        assert time.monotonic() - started < 2.0
    finally:
        holder.rollback()
        holder.close()
    counters = sink.counters()
    assert counters["shutdown_drop"] == 1
    assert counters["db_busy_drop"] == 0
    assert rows(db_path, "order_state_changed") == []


def test_ac14a_released_within_deadline_saves_exactly_one(db_path):
    holder = connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    sink = make(db_path, busy_retry_sec=BUSY_RETRY_SEC)
    sink.start()
    try:
        started = time.monotonic()
        sink.submit("order_state_changed", "order:1", dict(FIELDS))
        assert time.monotonic() - started < 0.2
        time.sleep(0.3)
        holder.rollback()
        assert wait_until(lambda: len(rows(db_path, "order_state_changed")) == 1)
        time.sleep(0.2)
    finally:
        sink.stop()
        holder.close()
    assert len(rows(db_path, "order_state_changed")) == 1
    assert sink.counters()["db_busy_drop"] == 0


def test_ac14a_past_deadline_counts_one_busy_drop_without_duplicates(db_path):
    holder = connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    sink = make(db_path, busy_retry_sec=0.2)
    sink.start()
    try:
        sink.submit("order_state_changed", "order:1", dict(FIELDS))
        assert wait_until(lambda: sink.counters()["db_busy_drop"] == 1)
        holder.rollback()
        time.sleep(0.3)
    finally:
        sink.stop()
        holder.close()
    assert sink.counters()["db_busy_drop"] == 1
    assert rows(db_path, "order_state_changed") == []
