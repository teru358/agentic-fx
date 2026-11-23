"""scheduler の tick からの週次の確定の起動: 毎 tick 呼ぶだけで、失敗・遅延で tick を塞がない。"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from agentic_fx.core.weekly_review_tick import WeeklyReviewTick
from agentic_fx.store import weekly_review as store
from agentic_fx.store.db import connect

from tests.core.test_scheduler import SAT, WED, Env

UTC = timezone.utc


def test_the_poll_is_called_on_every_tick_with_the_tick_time(tmp_path):
    env = Env(tmp_path)
    seen: list[datetime] = []
    env.sched.register_weekly_review(seen.append)
    env.sched.tick(WED)
    env.sched.tick(WED + timedelta(seconds=30))
    assert seen == [WED, WED + timedelta(seconds=30)]


def test_the_poll_runs_while_the_market_is_closed(tmp_path):
    env = Env(tmp_path, base=SAT)
    seen: list[datetime] = []
    env.sched.register_weekly_review(seen.append)
    env.sched.tick(SAT)
    assert seen == [SAT]


def test_a_poll_that_raises_does_not_break_the_tick_or_the_daily_report(tmp_path):
    env = Env(tmp_path)
    reports: list = []
    env.sched.register_daily_report(lambda close_at: reports.append(close_at))

    def broken(now):
        raise RuntimeError("poll exploded")
    env.sched.register_weekly_review(broken)
    for hook in env.sched.tick(WED):
        hook()
    assert env.sched.join_daily_report(5)
    assert len(reports) == 1


def test_no_poll_is_made_while_the_service_is_stopping(tmp_path):
    env = Env(tmp_path)
    stop = threading.Event()
    env.sched._stop_event = stop
    seen: list = []
    env.sched.register_weekly_review(seen.append)
    stop.set()
    env.sched.tick(WED)
    assert seen == []


def test_a_slow_weekly_run_does_not_hold_the_tick(tmp_path):
    env = Env(tmp_path)
    path = tmp_path / "w.db"
    conn = connect(path)
    from agentic_fx.store.db import init_db
    init_db(conn)
    conn.close()
    gate, entered = threading.Event(), threading.Event()

    def slow(c, label, close_at, now):
        entered.set()
        gate.wait(10)

    tick = WeeklyReviewTick(open_conn=lambda: connect(path), finalize=slow)
    env.sched.register_weekly_review(tick.poll)
    started = time.monotonic()
    env.sched.tick(WED)
    assert entered.wait(5)
    env.sched.tick(WED + timedelta(seconds=30))
    assert time.monotonic() - started < 3
    gate.set()
    assert tick.join(5)


def test_the_daily_report_registration_is_independent_of_the_weekly_one(tmp_path):
    env = Env(tmp_path)
    env.sched.register_weekly_review(lambda now: None)
    reports: list = []
    env.sched.register_daily_report(lambda close_at: reports.append(close_at))
    for hook in env.sched.tick(WED):
        hook()
    assert env.sched.join_daily_report(5)
    assert len(reports) == 1
