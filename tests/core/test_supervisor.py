"""MissionSupervisor (プラン8, 設計書 §3.3)。"""
from __future__ import annotations

import threading
import time

import pytest

from agentic_fx.core.supervisor import MissionSupervisor


def _blocking_trade_fn(release: threading.Event):
    def fn(trigger):
        release.wait(5.0)
        return {"trigger": trigger}
    return fn


def test_try_submit_accepts_when_idle():
    sup = MissionSupervisor(trade_fn=lambda t: {"t": t},
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    future = sup.try_submit("trade", trigger="cron")
    assert future is not None
    result = future.result(timeout=2.0)
    assert result == {"trade": {"t": "cron"}, "reflection_count": 0}


def test_try_submit_rejects_when_busy():
    release = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron")
    assert f1 is not None
    time.sleep(0.05)  # f1 が確実にディスパッチされてから 2 回目を試す

    f2 = sup.try_submit("trade", trigger="signal")
    assert f2 is None  # busy — 即座に拒否

    release.set()
    f1.result(timeout=2.0)


def test_try_submit_accepts_again_after_previous_completes():
    sup = MissionSupervisor(trade_fn=lambda t: {"t": t},
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron")
    f1.result(timeout=2.0)
    f2 = sup.try_submit("trade", trigger="cron")
    assert f2 is not None
    f2.result(timeout=2.0)


def test_trade_job_chains_reflection_after():
    calls: list[str] = []

    def trade_fn(trigger):
        calls.append("trade")
        return {"trigger": trigger}

    def reflection_fn():
        calls.append("reflection")
        return 2

    sup = MissionSupervisor(trade_fn=trade_fn, reflection_fn=reflection_fn,
                            ask_fn=lambda q: q)
    sup.start()
    future = sup.try_submit("trade", trigger="cron")
    result = future.result(timeout=2.0)
    assert calls == ["trade", "reflection"]
    assert result["reflection_count"] == 2


def test_ask_job_returns_via_future():
    sup = MissionSupervisor(trade_fn=lambda t: None, reflection_fn=lambda: 0,
                            ask_fn=lambda q: f"answer: {q}")
    sup.start()
    future = sup.try_submit("ask", question="usdjpy どう？")
    assert future.result(timeout=2.0) == "answer: usdjpy どう？"


def test_shutdown_fails_pending_future_without_blocking():
    release = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron")  # 即座にディスパッチされ実行中になる
    time.sleep(0.05)
    f2 = sup.try_submit("trade", trigger="cron")
    assert f2 is None  # busy のため queue には入っていない (実行中ジョブが 1 つあるのみ)

    # shutdown は "未着手" (queue 内で待機中) の Future を例外完了させる契約
    # だが、上のケースでは queue が空 (f1 は既にディスパッチ済み) のため
    # shutdown 呼び出し自体は何もしない — 実行中ジョブの終了は待たない
    # (ブロックしないことの確認)。
    start = time.monotonic()
    sup.shutdown(drain_exc=RuntimeError("shutting down"))
    assert time.monotonic() - start < 0.5  # ブロックしていない

    release.set()
    f1.result(timeout=2.0)  # 実行中ジョブは通常どおり完了する


def test_heartbeat_is_touched_during_long_dispatch():
    """裁定書 F-3 (CR-1) の回帰ピン: _dispatch が長時間ブロックしている
    間も heartbeat が touch され続ける (while ループ先頭だけでは更新
    されない — watchdog の heartbeat_grace_sec 誤検知を防ぐ)。real-sleep
    予算内に収めるため pump 間隔を 0.05s に短縮して注入する。"""
    release = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q,
                            heartbeat_pump_interval_sec=0.05)
    sup.start()
    sup.try_submit("trade", trigger="cron")
    time.sleep(0.02)
    hb_before = sup.heartbeat
    time.sleep(0.2)  # dispatch はまだ release 待ちでブロック中
    hb_during = sup.heartbeat
    assert hb_during > hb_before  # ポンプが touch している

    release.set()


def test_busy_since_is_set_during_dispatch_and_cleared_after():
    """裁定書 F-3 (CR-1) advisor 指摘反映の回帰ピン: busy_since は
    dispatch 開始時に time.monotonic() を持ち、完了後は None に戻る —
    Task 19 watchdog の dispatch_ceiling_sec 判定の入力になる。"""
    release = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    assert sup.busy_since is None
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron")
    time.sleep(0.05)
    assert sup.busy_since is not None
    assert sup.busy_since <= time.monotonic()

    release.set()
    f1.result(timeout=2.0)
    time.sleep(0.05)  # finally 節が busy_since=None を反映するまで
    assert sup.busy_since is None
