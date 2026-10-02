"""再起動をまたぐ取得の間隔 (経済指標・ニュース)。

再起動の連打がそのまま先方への連打 (HTTP 429) にならないこと。再起動は
「同じ DB で scheduler を作り直す」形で模擬する。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from agentic_fx.core.scheduler import Scheduler
from agentic_fx.store import fetch_attempts

from tests.core.test_scheduler import SETTINGS, WED, Env


def _restart(env: Env) -> Scheduler:
    """同じ DB・同じ hook で scheduler だけ作り直す (プロセス再起動の模擬)。"""
    return Scheduler(
        conn=env.conn, executor=env.executor, settings=SETTINGS,
        state_store=env.state, activity=env.sched.activity,
        bars_fn=lambda p: env.bars.get(p),
        on_trade_mission=env._trade, on_news_cycle=env._news,
        on_econ_cycle=env._econ)


def _activity(env: Env) -> str:
    return (env.tmp_path / "a.log").read_text(encoding="utf-8")


T_1038 = WED.replace(hour=10, minute=38)


def test_first_start_with_no_record_fetches_immediately(tmp_path):
    env = Env(tmp_path, base=T_1038)
    env.sched.tick(T_1038)
    assert (env.news_calls, env.econ_calls) == (1, 1)


def test_restart_soon_after_fetch_does_not_fetch_again(tmp_path):
    env = Env(tmp_path, base=T_1038)
    env.sched.tick(T_1038)
    sched2 = _restart(env)
    sched2.tick(T_1038 + timedelta(minutes=12))          # 10:50 に再起動
    assert (env.news_calls, env.econ_calls) == (1, 1)


def test_restart_skip_is_reported_once_with_next_time(tmp_path):
    env = Env(tmp_path, base=T_1038)
    env.sched.tick(T_1038)
    sched2 = _restart(env)
    sched2.tick(T_1038 + timedelta(minutes=12))
    sched2.tick(T_1038 + timedelta(minutes=13))
    log = _activity(env)
    assert log.count("econ_fetch_skipped") == 1
    assert log.count("news_fetch_skipped") == 1
    assert "次回は 16:38 UTC" in log          # econ: 10:38 + 6h
    assert "次回は 11:08 UTC" in log          # news: 10:38 + 30m


def test_restart_fetches_news_when_interval_has_just_elapsed(tmp_path):
    env = Env(tmp_path, base=T_1038)
    env.sched.tick(T_1038)
    sched2 = _restart(env)
    sched2.tick(T_1038 + timedelta(minutes=30) - timedelta(seconds=1))
    assert (env.news_calls, env.econ_calls) == (1, 1)
    sched3 = _restart(env)
    sched3.tick(T_1038 + timedelta(minutes=30))           # ちょうど
    assert (env.news_calls, env.econ_calls) == (2, 1)


def test_restart_fetches_econ_exactly_at_six_hours(tmp_path):
    env = Env(tmp_path, base=T_1038)
    env.sched.tick(T_1038)
    _restart(env).tick(T_1038 + timedelta(hours=6) - timedelta(seconds=1))
    assert env.econ_calls == 1
    _restart(env).tick(T_1038 + timedelta(hours=6))
    assert env.econ_calls == 2


def test_failed_fetch_is_not_retried_by_restart(tmp_path):
    def boom():
        raise RuntimeError("429")
    env = Env(tmp_path, base=T_1038, news_fn=boom, econ_fn=boom)
    env.sched.tick(T_1038)
    _restart(env).tick(T_1038 + timedelta(minutes=12))
    assert (env.news_calls, env.econ_calls) == (1, 1)


def test_next_cycle_after_restart_follows_the_persisted_time(tmp_path):
    env = Env(tmp_path, base=T_1038)
    env.sched.tick(T_1038)
    sched2 = _restart(env)
    sched2.tick(T_1038 + timedelta(hours=5, minutes=50))   # 16:28 再起動
    assert env.econ_calls == 1
    sched2.tick(T_1038 + timedelta(hours=6))                # 16:38 に定期取得
    assert env.econ_calls == 2


def test_persisted_time_in_the_future_waits_one_interval(tmp_path):
    env = Env(tmp_path, base=T_1038)
    fetch_attempts.record_attempt(
        env.conn, fetch_attempts.ECON_KEY, T_1038 + timedelta(days=2))
    sched2 = _restart(env)
    sched2.tick(T_1038)
    assert env.econ_calls == 0
    sched2.tick(T_1038 + timedelta(hours=6) - timedelta(seconds=1))
    assert env.econ_calls == 0
    sched2.tick(T_1038 + timedelta(hours=6))
    assert env.econ_calls == 1


def test_future_persisted_time_is_rebased_so_next_restart_is_not_stuck(tmp_path):
    env = Env(tmp_path, base=T_1038)
    fetch_attempts.record_attempt(
        env.conn, fetch_attempts.ECON_KEY, T_1038 + timedelta(days=2))
    _restart(env).tick(T_1038)
    _restart(env).tick(T_1038 + timedelta(hours=6))
    assert env.econ_calls == 1


def test_corrupt_persisted_value_waits_one_interval(tmp_path):
    env = Env(tmp_path, base=T_1038)
    with env.conn:
        env.conn.execute(
            "INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?)",
            (fetch_attempts.ECON_KEY, "not json", T_1038.isoformat()))
    sched2 = _restart(env)
    sched2.tick(T_1038)
    assert env.econ_calls == 0


def test_retry_after_longer_than_interval_extends_the_wait(tmp_path):
    def too_many():
        fetch_attempts.record_retry_after(
            env.conn, fetch_attempts.ECON_KEY, T_1038, timedelta(hours=9))
    env = Env(tmp_path, base=T_1038, econ_fn=too_many)
    env.sched.tick(T_1038)
    env.sched.tick(T_1038 + timedelta(hours=6, minutes=1))   # 通常の間隔は過ぎた
    assert env.econ_calls == 1
    _restart(env).tick(T_1038 + timedelta(hours=8, minutes=59))
    assert env.econ_calls == 1
    _restart(env).tick(T_1038 + timedelta(hours=9))
    assert env.econ_calls == 2


def test_retry_after_shorter_than_interval_keeps_the_interval(tmp_path):
    def too_many():
        fetch_attempts.record_retry_after(
            env.conn, fetch_attempts.ECON_KEY, T_1038, timedelta(minutes=5))
    env = Env(tmp_path, base=T_1038, econ_fn=too_many)
    env.sched.tick(T_1038)
    env.sched.tick(T_1038 + timedelta(hours=5, minutes=59))
    assert env.econ_calls == 1
    env.sched.tick(T_1038 + timedelta(hours=6))
    assert env.econ_calls == 2


def test_persistence_failure_does_not_stop_fetching(tmp_path, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("disk")
    monkeypatch.setattr(fetch_attempts, "record_attempt", broken)
    monkeypatch.setattr(fetch_attempts, "due_anchor", broken)
    env = Env(tmp_path, base=T_1038)
    env.sched.tick(T_1038)
    assert (env.news_calls, env.econ_calls) == (1, 1)


def test_attempt_is_stored_as_tz_aware_utc(tmp_path):
    env = Env(tmp_path, base=T_1038)
    env.sched.tick(T_1038)
    row = env.conn.execute(
        "SELECT value FROM alert_state WHERE key=?",
        (fetch_attempts.ECON_KEY,)).fetchone()
    stored = datetime.fromisoformat(json.loads(row["value"])["attempted_at"])
    assert stored == T_1038 and stored.utcoffset() == timedelta(0)


@pytest.mark.parametrize("raw", [
    "Infinity", "-Infinity", "NaN", "1e308", "-5", '"abc"', "true", "null",
    "86401"])
def test_corrupt_retry_after_waits_one_interval(tmp_path, raw):
    env = Env(tmp_path, base=T_1038)
    # 試行時刻だけ見れば 6 時間以上前で、直ちに取得してよい記録
    stored = ('{"attempted_at": "%s", "retry_after_sec": %s}'
              % ((T_1038 - timedelta(hours=7)).isoformat(), raw))
    with env.conn:
        env.conn.execute(
            "INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?)",
            (fetch_attempts.ECON_KEY, stored, T_1038.isoformat()))
    sched2 = _restart(env)
    sched2.tick(T_1038)
    assert env.econ_calls == 0
    sched2.tick(T_1038 + timedelta(hours=6) - timedelta(seconds=1))
    assert env.econ_calls == 0
    sched2.tick(T_1038 + timedelta(hours=6))
    assert env.econ_calls == 1
