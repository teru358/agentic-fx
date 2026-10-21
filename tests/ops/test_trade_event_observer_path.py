"""observer の経路 (drop・例外) が logging も lock 待ちもしないこと、warning は sink の worker が出すこと。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import pytest

from agentic_fx.core import transitions
from agentic_fx.core.contracts import OrderStatus as S
from agentic_fx.ops import trade_event_sink as sink_mod
from agentic_fx.ops import trade_events
from agentic_fx.ops.trade_event_sink import TradeEventEmitter, TradeEventSink
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db

WALL = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
FIELDS = {"state": "placed", "pair": "USDJPY", "side": "long", "entry_type": "limit",
          "qty": "0.1", "price": "150.123", "sl": "149.8", "tp": None,
          "origin_code": "cron", "mode": "paper", "state_mode": None, "reason_code": None}


class Spy(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def spy():
    handler = Spy()
    loggers = [logging.getLogger(n) for n in ("", "agentic_fx", sink_mod.__name__,
                                               transitions.__name__)]
    saved = [(lg, lg.level, lg.disabled) for lg in loggers]
    for lg in loggers:
        lg.disabled = False
        lg.setLevel(logging.DEBUG)
    # 経路上の logger のどれに出ても数える (propagate 設定に依存しない)
    loggers[2].addHandler(handler)
    loggers[3].addHandler(handler)
    loggers[0].addHandler(handler)
    yield handler
    for lg in (loggers[2], loggers[3], loggers[0]):
        lg.removeHandler(handler)
    for lg, level, disabled in saved:
        lg.setLevel(level)
        lg.disabled = disabled


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    conn = connect(path)
    init_db(conn)
    conn.close()
    return path


def make(db_path, **kw):
    return TradeEventSink(db_path, wall_clock=lambda: WALL, **kw)


def pending_order(conn):
    return orders.insert(conn, pair="USDJPY", direction="long", entry_type="limit",
                         horizon="day", status=S.SUBMITTED, now=WALL, quantity=0.1,
                         requested_price=148.2, stop_loss=147.8)


def test_queue_overflow_does_not_log(db_path, spy):
    sink = make(db_path, queue_max=1)
    sink.submit("order_state_changed", "order:1", dict(FIELDS))
    sink.submit("order_state_changed", "order:2", dict(FIELDS))
    assert sink.counters()["queue_drop"] == 1
    assert spy.records == []


def test_invalid_value_and_builder_exception_do_not_log(db_path, spy, monkeypatch):
    conn = connect(db_path)
    sink = make(db_path)
    emitter = TradeEventEmitter(conn, sink, state_mode=None, ccy="JPY")
    oid = pending_order(conn)
    base = dict(orders.get(conn, oid))
    emitter(dict(base, quantity=float("nan")), "submitted", "pending_fill")
    monkeypatch.setattr(trade_events, "build_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    emitter(base, "submitted", "pending_fill")
    conn.close()
    got = sink.counters()
    assert got["invalid_value_drop"] == 1 and got["observer_error"] == 1
    assert spy.records == []


def test_observer_exception_in_transition_does_not_log_only_counts(db_path, spy):
    conn = connect(db_path)
    oid = pending_order(conn)

    def bad(row, frm, to):
        raise RuntimeError("boom")

    token = transitions.observe(conn, bad)
    before = transitions.observer_failures.value()
    transitions.transition(conn, oid, S.PENDING_FILL, WALL)
    transitions.unobserve(token)
    conn.close()
    assert transitions.observer_failures.value() == before + 1
    assert spy.records == []


def test_observer_drop_does_not_wait_for_the_counter_lock(db_path):
    sink = make(db_path, queue_max=1)
    sink.submit("order_state_changed", "order:1", dict(FIELDS))
    held, release = threading.Event(), threading.Event()

    def hold():
        with sink._lock:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(2)
    done = threading.Event()

    def observe():
        sink.submit("order_state_changed", "order:2", dict(FIELDS))  # queue 溢れ
        done.set()

    t = threading.Thread(target=observe, daemon=True)
    t.start()
    try:
        assert done.wait(1.0), "observer が counter の lock を待った"
    finally:
        release.set()
        holder.join(2)
    assert sink.counters()["queue_drop"] == 1


def test_drop_warning_is_emitted_by_the_worker_thread(db_path, spy):
    sink = make(db_path, queue_max=1)
    sink.submit("order_state_changed", "order:1", dict(FIELDS))
    sink.submit("order_state_changed", "order:2", dict(FIELDS))
    assert spy.records == []
    sink.start()
    end = time.monotonic() + 5
    while time.monotonic() < end and not spy.records:
        time.sleep(0.01)
    sink.stop()
    warned = [r for r in spy.records if "queue_drop" in r.getMessage()]
    assert warned and all(r.threadName == "afx-trade-event-sink" for r in warned)


def test_observer_drop_degrades_health_and_counts_exactly(db_path):
    sink = make(db_path, queue_max=1)
    assert sink.health() == "ok"
    sink.submit("order_state_changed", "order:1", dict(FIELDS))
    for _ in range(5):
        sink.submit("order_state_changed", "order:x", dict(FIELDS))
    assert sink.counters()["queue_drop"] == 5
    assert sink.counters()["queue_drop"] == 5  # 読むたびに増えない
    assert sink.health() == "degraded"


def test_lockfree_counter_is_exact_under_threads():
    counter = transitions.LockFreeCounter()

    def work():
        for _ in range(2000):
            counter.incr()

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    assert counter.value() >= 0
    for t in threads:
        t.join()
    assert counter.value() == 16000
    assert counter.value() == 16000


def test_late_submit_after_stop_drained_is_counted_not_stranded(db_path):
    sink = make(db_path)
    sink.start()
    real_put = sink._queue.put_nowait

    def racing_put(item):
        # submit が停止の確認を済ませた直後に stop が完了する競合を再現する
        sink.stop()
        real_put(item)

    sink._queue.put_nowait = racing_put
    sink._stopping.clear()  # 競合の「確認時点では停止前」を作る
    sink._closed.clear()
    sink._thread = None
    sink.submit("order_state_changed", "order:1", dict(FIELDS))
    assert sink._queue.empty()
    assert sink.counters()["shutdown_drop"] == 1


def test_stop_timeout_counts_inflight_and_worker_exits_after_release(db_path):
    release = threading.Event()
    sink = make(db_path, join_timeout=0.2)
    real = sink._insert
    sink._insert = lambda *a, **k: (release.wait(10), real(*a, **k))[1]
    sink.start()
    thread = sink._thread
    sink.submit("order_state_changed", "order:1", dict(FIELDS))
    time.sleep(0.1)
    sink.stop()
    assert sink.counters()["shutdown_drop"] == 1
    assert thread.is_alive()
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert sink._conn is None


def test_stop_leaves_no_worker_thread(db_path):
    sink = make(db_path)
    sink.start()
    thread = sink._thread
    sink.stop()
    assert not thread.is_alive()


def test_concurrent_submit_and_stop_account_for_every_item(db_path):
    for _ in range(20):
        sink = make(db_path, queue_max=10000)
        sink.start()
        go = threading.Event()
        total = 300

        def producer():
            go.wait()
            for i in range(total):
                sink.submit("order_state_changed", f"order:{i + 1}", dict(FIELDS))

        t = threading.Thread(target=producer)
        t.start()
        go.set()
        time.sleep(0.001)
        sink.stop()
        t.join()
        saved = len(rows_of(db_path))
        assert saved + sink.counters()["shutdown_drop"] == total
        assert sink._queue.empty()
        for p in (db_path,):
            conn = connect(p)
            conn.execute("DELETE FROM ops_events")
            conn.commit()
            conn.close()


def rows_of(db_path):
    conn = connect(db_path)
    try:
        return conn.execute("SELECT id FROM ops_events WHERE code='order_state_changed'").fetchall()
    finally:
        conn.close()


def test_no_insert_is_attempted_after_the_busy_deadline(db_path, monkeypatch):
    import sqlite3
    sink = make(db_path, busy_retry_sec=0.05)
    monkeypatch.setattr(sink_mod, "backoff_sec", lambda attempt, rng=None: 0.5)
    calls = []

    def busy(*a, **k):
        if a[1] == "order_state_changed":
            calls.append(time.monotonic())
        exc = sqlite3.OperationalError("busy")
        exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise exc

    sink._insert = busy
    sink.start()
    sink.submit("order_state_changed", "order:1", dict(FIELDS))
    end = time.monotonic() + 5
    while time.monotonic() < end and sink.counters()["db_busy_drop"] == 0:
        time.sleep(0.01)
    sink.stop()
    assert sink.counters()["db_busy_drop"] == 1
    assert len(calls) == 1
