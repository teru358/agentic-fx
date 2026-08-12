"""Scheduler の ohlcv_cache maintenance フックのテスト (プラン 9 Task 16)。

`Env`/`WED`/`SETTINGS` は `tests/core/test_scheduler.py` のものを再利用する
(既存の Env は on_cache_maintenance を持たないので、各テストで
`env.sched.on_cache_maintenance` を直接差し替える —
`tests/core/test_scheduler_signal.py` と同じ流儀)。
"""
from __future__ import annotations

from datetime import timedelta

from tests.core.test_scheduler import WED, Env


def test_tick_calls_on_cache_maintenance_every_tick_when_configured(tmp_path):
    """on_signal_maintenance と同型: 毎 tick 呼ばれる (間引きしない —
    prune は「追いつくまで毎 maintenance」実行する設計 D2)。"""
    env = Env(tmp_path)
    calls = []
    env.sched.on_cache_maintenance = lambda now: calls.append(now)
    env.sched.tick(WED)
    env.sched.tick(WED + timedelta(minutes=1))
    assert len(calls) == 2


def test_tick_skips_cache_maintenance_when_not_configured(tmp_path):
    """既定 None = 機能無効 — 未設定でも tick が例外なく完了すること。"""
    env = Env(tmp_path)
    env.sched.tick(WED)  # on_cache_maintenance 未設定のまま


def test_cache_maintenance_failure_does_not_stop_tick(tmp_path):
    """_run_data_hook と同じ fail-open 契約 — prune 失敗が資金保護
    (_process_exits) を止めてはならない。"""
    def _boom(now):
        raise RuntimeError("prune failed")
    env = Env(tmp_path)
    env.sched.on_cache_maintenance = _boom
    env.sched.tick(WED)  # 例外を送出せず完了すること
