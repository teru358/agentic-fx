"""scheduler の書込み要求フラグと worker の書込み開始の全順序化。"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from agentic_fx.core.writer_admission import WriterAdmission


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_the_flag_is_down_until_requested_and_down_again_after_release():
    wa = WriterAdmission()
    assert wa.requested() is False
    wa.request()
    assert wa.requested() is True
    wa.release()
    assert wa.requested() is False


def test_a_release_without_a_request_is_a_bug():
    with pytest.raises(RuntimeError):
        WriterAdmission().release()


def test_nested_requests_keep_the_flag_up_until_the_outermost_release():
    clock = Clock()
    wa = WriterAdmission(monotonic=clock)
    wa.request()
    clock.t += 1.0
    wa.request()
    clock.t += 2.0
    wa.release()
    assert wa.requested() is True
    clock.t += 4.0
    wa.release()
    assert wa.requested() is False
    stats = wa.stats()
    assert stats.requests == 1                             # 入れ子は 1 回の要求
    assert stats.last_hold_sec == stats.max_hold_sec == stats.total_hold_sec == 7.0


def test_hold_time_is_recorded_per_request_with_max_and_total():
    clock = Clock()
    wa = WriterAdmission(monotonic=clock)
    for seconds in (0.5, 3.0, 1.0):
        wa.request()
        clock.t += seconds
        wa.release()
    stats = wa.stats()
    assert stats.requests == 3
    assert stats.last_hold_sec == 1.0 and stats.max_hold_sec == 3.0
    assert stats.total_hold_sec == 4.5


def test_no_hold_is_reported_before_any_request():
    stats = WriterAdmission().stats()
    assert stats.last_hold_sec is None and stats.max_hold_sec is None
    assert stats.requested is False and stats.requests == 0


def test_the_context_manager_releases_on_error():
    wa = WriterAdmission()
    with pytest.raises(ValueError):
        with wa.scheduler_write():
            assert wa.requested() is True
            raise ValueError("x")
    assert wa.requested() is False


def test_admit_runs_begin_when_the_flag_is_down_and_skips_it_when_up():
    wa = WriterAdmission()
    calls = []
    assert wa.admit(lambda: calls.append("begin")) is True
    wa.request()
    assert wa.admit(lambda: calls.append("begin")) is False
    wa.release()
    assert calls == ["begin"]
    stats = wa.stats()
    assert (stats.admitted, stats.yielded) == (1, 1)


def test_a_failing_begin_propagates_and_leaves_the_mutex_usable():
    wa = WriterAdmission()

    def boom():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        wa.admit(boom)
    assert wa.stats().admitted == 0
    wa.request()                                           # mutex が放されている
    wa.release()


def test_a_request_is_not_blocked_by_a_begin_in_progress():
    """admit は begin を mutex の外で呼ぶ。begin (BEGIN IMMEDIATE の待ち) の最中でも、
    scheduler の request() はすぐ登録できる。進行中の begin は観測値に数える。"""
    wa = WriterAdmission()
    in_begin = threading.Event()
    finish_begin = threading.Event()
    registered = threading.Event()
    order = []

    def begin():
        order.append("begin-start")
        in_begin.set()
        assert finish_begin.wait(10)
        order.append("begin-end")

    def scheduler():
        in_begin.wait(10)
        wa.request()
        order.append("registered")
        registered.set()
        wa.release()

    worker = threading.Thread(target=lambda: wa.admit(begin))
    sched = threading.Thread(target=scheduler)
    worker.start()
    sched.start()
    try:
        assert in_begin.wait(10)
        assert registered.wait(5)                          # begin の間でも登録できる
        assert wa.stats().beginning == 1
    finally:
        finish_begin.set()
    worker.join(10)
    sched.join(10)
    assert order == ["begin-start", "registered", "begin-end"]
    stats = wa.stats()
    assert stats.beginning == 0 and stats.admitted == 1


def test_a_failed_begin_leaves_no_transaction_in_progress():
    wa = WriterAdmission()

    def boom():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        wa.admit(boom)
    assert wa.stats().beginning == 0 and wa.stats().admitted == 0


def test_a_registered_request_makes_a_later_admit_yield_without_opening_a_transaction(tmp_path):
    conn = sqlite3.connect(tmp_path / "w.db", isolation_level=None)
    conn.execute("CREATE TABLE t (x)")
    wa = WriterAdmission()
    wa.request()
    assert wa.admit(lambda: conn.execute("BEGIN IMMEDIATE")) is False
    assert conn.in_transaction is False
    wa.release()
    assert wa.admit(lambda: conn.execute("BEGIN IMMEDIATE")) is True
    assert conn.in_transaction is True
    conn.execute("ROLLBACK")
    conn.close()


def test_tick_observation_is_recorded_but_does_not_affect_admission():
    wa = WriterAdmission()
    assert wa.stats().tick_active is False and wa.stats().next_tick_at is None
    wa.tick_started()
    wa.note_next_tick(160.0)
    stats = wa.stats()
    assert stats.tick_active is True and stats.next_tick_at == 160.0
    assert wa.admit(lambda: None) is True                  # 印は排他に使わない
    wa.tick_finished()
    assert wa.stats().tick_active is False and wa.stats().next_tick_at == 160.0
