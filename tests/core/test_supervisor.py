"""MissionSupervisor (プラン8, 設計書 §3.3)。"""
from __future__ import annotations

import threading
import time

import pytest

from agentic_fx.core.supervisor import MissionSupervisor


def _blocking_trade_fn(release: threading.Event, started: threading.Event | None = None):
    """I1 fix: started Event を持つ blocking trade function。
    テストは started.wait(timeout=...) で dispatch 開始を待つ。"""
    def fn(trigger):
        if started is not None:
            started.set()
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
    # I1 fix: time.sleep → threading.Event
    release = threading.Event()
    started = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release, started),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron")
    assert f1 is not None
    started.wait(timeout=5.0)  # f1 が確実にディスパッチされてから 2 回目を試す

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


def test_trade_job_rejects_during_reflection_chain():
    """I2 fix: trade → reflection 連鎖の途中 (reflection 実行中) に
    別の try_submit が拒否されることを検証する。"""
    reflection_started = threading.Event()
    reflection_release = threading.Event()
    calls: list[str] = []

    def trade_fn(trigger):
        calls.append("trade")
        return {"trigger": trigger}

    def reflection_fn():
        reflection_started.set()
        reflection_release.wait(5.0)
        calls.append("reflection")
        return 1

    sup = MissionSupervisor(trade_fn=trade_fn, reflection_fn=reflection_fn,
                            ask_fn=lambda q: q)
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron")
    assert f1 is not None
    # reflection が開始されるまで待つ
    reflection_started.wait(timeout=5.0)
    # reflection 実行中に submit を試みる
    f2 = sup.try_submit("trade", trigger="cron")
    assert f2 is None  # reflection 実行中なので拒否される
    # reflection を完了させる
    reflection_release.set()
    f1.result(timeout=2.0)
    assert calls == ["trade", "reflection"]


def test_ask_job_returns_via_future():
    sup = MissionSupervisor(trade_fn=lambda t: None, reflection_fn=lambda: 0,
                            ask_fn=lambda q: f"answer: {q}")
    sup.start()
    future = sup.try_submit("ask", question="usdjpy どう？")
    assert future.result(timeout=2.0) == "answer: usdjpy どう？"


def test_shutdown_fails_pending_future_without_blocking():
    # I1 fix: time.sleep → threading.Event
    release = threading.Event()
    started = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release, started),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron")  # 即座にディスパッチされ実行中になる
    started.wait(timeout=5.0)  # dispatch が開始されたことを待つ
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


def test_shutdown_rejects_new_submissions():
    """C2 fix: shutdown 後は try_submit が None を返す。この検査は idle
    状態 (busy=False) からの shutdown で行う必要がある。"""
    sup = MissionSupervisor(trade_fn=lambda t: {"t": t},
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    # idle 状態で shutdown する
    sup.shutdown(drain_exc=RuntimeError("shutting down"))
    # 以降の try_submit はすべて None を返す
    result = sup.try_submit("trade", trigger="cron")
    assert result is None, "shutdown 後も try_submit が受理されている"


def test_heartbeat_is_touched_during_long_dispatch():
    """裁定書 F-3 (CR-1) の回帰ピン: _dispatch が長時間ブロックしている
    間も heartbeat が touch され続ける (while ループ先頭だけでは更新
    されない — watchdog の heartbeat_grace_sec 誤検知を防ぐ)。real-sleep
    予算内に収めるため pump 間隔を 0.05s に短縮して注入する。
    I1 note: time.sleep は heartbeat の実時間経過を測定するため必須。"""
    release = threading.Event()
    started = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release, started),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q,
                            heartbeat_pump_interval_sec=0.05)
    sup.start()
    sup.try_submit("trade", trigger="cron")
    started.wait(timeout=5.0)  # dispatch が開始されたことを確認
    # 実時間経過測定 (heartbeat pump タイミング検証に必須)
    time.sleep(0.02)
    hb_before = sup.heartbeat
    time.sleep(0.2)  # dispatch はまだ release 待ちでブロック中
    hb_during = sup.heartbeat
    assert hb_during > hb_before  # ポンプが touch している

    release.set()


def test_busy_since_is_set_during_dispatch_and_cleared_after():
    """裁定書 F-3 (CR-1) advisor 指摘反映の回帰ピン: busy_since は
    dispatch 開始時に time.monotonic() を持ち、完了後は None に戻る —
    Task 19 watchdog の dispatch_ceiling_sec 判定の入力になる。
    I1 fix: time.sleep → threading.Event。ただし最終確認は result() 後に
    即座に行えるため sleep は不要。"""
    release = threading.Event()
    started = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release, started),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    assert sup.busy_since is None
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron")
    started.wait(timeout=5.0)  # dispatch が開始されたことを待つ
    assert sup.busy_since is not None
    assert sup.busy_since <= time.monotonic()

    release.set()
    f1.result(timeout=2.0)
    # result() が戻ったので finally 節は既に実行済み
    assert sup.busy_since is None
