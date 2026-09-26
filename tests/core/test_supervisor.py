"""MissionSupervisor (プラン8, 設計書 §3.3)。"""
from __future__ import annotations

import threading
import time
from types import MappingProxyType

import pytest

from agentic_fx.core.supervisor import MissionSupervisor, SubmitResult


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
    submitted = sup.try_submit("trade", trigger="cron")
    assert submitted.accepted is True
    future = submitted.future
    result = future.result(timeout=2.0)
    assert result == {"trade": {"t": "cron"}, "reflection_count": 0}


def test_try_submit_rejects_when_busy():
    # I1 fix: time.sleep → threading.Event
    release = threading.Event()
    started = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release, started),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    r1 = sup.try_submit("trade", trigger="cron")
    assert r1.accepted is True
    f1 = r1.future
    started.wait(timeout=5.0)  # f1 が確実にディスパッチされてから 2 回目を試す

    r2 = sup.try_submit("trade", trigger="signal")
    assert r2.accepted is False and r2.reason == "running"

    release.set()
    f1.result(timeout=2.0)


def test_try_submit_accepts_again_after_previous_completes():
    sup = MissionSupervisor(trade_fn=lambda t: {"t": t},
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron").future
    f1.result(timeout=2.0)
    r2 = sup.try_submit("trade", trigger="cron")
    assert r2.accepted is True
    f2 = r2.future
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
    future = sup.try_submit("trade", trigger="cron").future
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
    r1 = sup.try_submit("trade", trigger="cron")
    assert r1.accepted is True
    f1 = r1.future
    # reflection が開始されるまで待つ
    reflection_started.wait(timeout=5.0)
    # reflection 実行中に submit を試みる
    r2 = sup.try_submit("trade", trigger="cron")
    assert r2.accepted is False and r2.reason == "running"
    # reflection を完了させる
    reflection_release.set()
    f1.result(timeout=2.0)
    assert calls == ["trade", "reflection"]


def test_ask_job_returns_via_future():
    sup = MissionSupervisor(trade_fn=lambda t: None, reflection_fn=lambda: 0,
                            ask_fn=lambda q: f"answer: {q}")
    sup.start()
    future = sup.try_submit("ask", question="usdjpy どう？").future
    assert future.result(timeout=2.0) == "answer: usdjpy どう？"


def test_shutdown_fails_pending_future_without_blocking():
    # I1 fix: time.sleep → threading.Event
    release = threading.Event()
    started = threading.Event()
    sup = MissionSupervisor(trade_fn=_blocking_trade_fn(release, started),
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    f1 = sup.try_submit("trade", trigger="cron").future  # 即座にディスパッチされ実行中になる
    started.wait(timeout=5.0)  # dispatch が開始されたことを待つ
    r2 = sup.try_submit("trade", trigger="cron")
    assert r2.accepted is False and r2.reason == "running"

    # shutdown は "未着手" (queue 内で待機中) の Future を例外完了させる契約
    # だが、上のケースでは queue が空 (f1 は既にディスパッチ済み) のため
    # shutdown 呼び出し自体は何もしない — 実行中ジョブの終了は待たない
    # (ブロックしないことの確認)。
    start = time.monotonic()
    sup.shutdown(drain_exc=RuntimeError("shutting down"))
    assert time.monotonic() - start < 0.5  # ブロックしていない

    release.set()
    f1.result(timeout=2.0)  # 実行中ジョブは通常どおり完了する


def test_shutdown_and_try_submit_are_serialized_so_accepted_always_resolves():
    """`shutdown` (stop_event 設定 + queue ドレイン) と
    `try_submit` の受理判定を同じ lock で直列化する。以前は `shutdown` が
    lock を使わずに stop_event を立てて queue を drain していたため、
    「stop_event 判定の直後・enqueue の直前」に shutdown が割り込むと、
    受理された Future が誰にも消費されず永久に未解決のまま残り得た
    (queue.put が shutdown のドレインより後に実行されてしまうため)。"""
    sup = MissionSupervisor(trade_fn=lambda t: t, reflection_fn=lambda: 0,
                            ask_fn=lambda q: q)
    # supervisor スレッドは起動しない (queue の消費者がいない状態で試験する
    # — try_submit が受理した場合、誰かが必ず解決させることを検証したい)。

    about_to_put = threading.Event()
    resume_put = threading.Event()
    drain_attempted = threading.Event()
    real_put = sup._queue.put
    real_get_nowait = sup._queue.get_nowait

    def wrapped_put(item, *a, **kw):
        about_to_put.set()
        assert resume_put.wait(5.0), "shutdown 側が進まなかった"
        return real_put(item, *a, **kw)

    def wrapped_get_nowait(*a, **kw):
        drain_attempted.set()
        return real_get_nowait(*a, **kw)

    sup._queue.put = wrapped_put
    sup._queue.get_nowait = wrapped_get_nowait

    results: list[SubmitResult] = []
    submit_thread = threading.Thread(
        target=lambda: results.append(sup.try_submit("trade", trigger="cron")),
        daemon=True)
    submit_thread.start()
    assert about_to_put.wait(5.0), "try_submit が enqueue 直前まで進まなかった"

    shutdown_thread = threading.Thread(
        target=lambda: sup.shutdown(drain_exc=RuntimeError("shutting down")),
        daemon=True)
    shutdown_thread.start()
    # 直列化されていない実装では shutdown がほぼ即座に (lock を待たず)
    # queue を空のまま drain してしまう — その猶予を与えてから put を
    # 再開させる。直列化されている実装では shutdown は lock 待ちで
    # ブロックしたままなので、この待ちは無害 (タイムアウトしても進む)。
    drain_attempted.wait(0.3)
    resume_put.set()

    submit_thread.join(timeout=5.0)
    shutdown_thread.join(timeout=5.0)
    assert not submit_thread.is_alive() and not shutdown_thread.is_alive()

    r = results[0]
    if r.accepted:
        assert r.future is not None
        try:
            r.future.exception(timeout=2.0)
        except TimeoutError:
            pytest.fail(
                "受理された future が shutdown 後も解決されない — "
                "try_submit と shutdown が直列化されていない")
    else:
        assert r.reason == "shutdown"


def test_shutdown_rejects_new_submissions():
    """C2 fix: shutdown 後は try_submit が reason="shutdown" で拒否する。この検査は idle
    状態 (busy=False) からの shutdown で行う必要がある。"""
    sup = MissionSupervisor(trade_fn=lambda t: {"t": t},
                            reflection_fn=lambda: 0, ask_fn=lambda q: q)
    sup.start()
    # idle 状態で shutdown する
    sup.shutdown(drain_exc=RuntimeError("shutting down"))
    # 以降の try_submit はすべて reason="shutdown" で拒否する
    result = sup.try_submit("trade", trigger="cron")
    assert result.accepted is False and result.reason == "shutdown", "shutdown 後も try_submit が受理されている"


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
    f1 = sup.try_submit("trade", trigger="cron").future
    started.wait(timeout=5.0)  # dispatch が開始されたことを待つ
    assert sup.busy_since is not None
    assert sup.busy_since <= time.monotonic()

    release.set()
    f1.result(timeout=2.0)
    # result() が戻ったので finally 節は既に実行済み
    assert sup.busy_since is None


class _FakeMonotonic:
    def __init__(self, t: float) -> None:
        self._t = t
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._t

    def set(self, t: float) -> None:
        with self._lock:
            self._t = t


def test_try_submit_reports_running_until_fake_worker_releases_slot():
    """fake worker は注入クロックが dispatch 開始から Cw (405 秒) 経つまで
    戻らない。その間 try_submit は running + busy_since で拒否し、解放後に
    受理する。実時間は待たない。"""
    clock = _FakeMonotonic(1000.0)
    cw = 405.0
    started = threading.Event()

    def trade_fn(trigger):
        started.set()
        deadline = time.monotonic() + 5.0
        while clock() < 1000.0 + cw:
            assert time.monotonic() < deadline, "fake clock が進まなかった"
            time.sleep(0.001)
        return {"trigger": trigger}

    sup = MissionSupervisor(trade_fn=trade_fn, reflection_fn=lambda: 0,
                            ask_fn=lambda q: q, clock_fn=clock)
    sup.start()
    first = sup.try_submit("trade", trigger="cron")
    assert first.accepted is True and first.reason is None
    assert first.busy_since is None and first.future is not None
    assert started.wait(5.0)
    for t in (1000.0, 1200.0, 1404.9):
        clock.set(t)
        r = sup.try_submit("trade", trigger="cron")
        assert (r.accepted, r.reason, r.busy_since, r.future) == (
            False, "running", 1000.0, None)
        assert r.busy_elapsed_sec == pytest.approx(t - 1000.0)
    clock.set(1000.0 + cw)
    first.future.result(timeout=5.0)
    again = sup.try_submit("trade", trigger="cron")
    assert again.accepted is True
    again.future.result(timeout=5.0)
    sup.shutdown(drain_exc=RuntimeError("test"))


def _submit_within(sup, timeout, kind, **kwargs):
    """try_submit を別スレッドで呼び、timeout 秒以内に戻らなければ失敗させる
    (受理済みの queue に 2 件目を積もうとして lock 内で詰まる実装を、
    テスト全体のハングではなく失敗として検出する)。"""
    box = []
    th = threading.Thread(target=lambda: box.append(sup.try_submit(kind, **kwargs)),
                          daemon=True)
    th.start()
    th.join(timeout)
    assert not th.is_alive(), "try_submit が戻らない (queue が満杯のまま積もうとした)"
    return box[0]


def test_try_submit_reports_queued_before_dispatch_starts():
    """受理済みだが dispatch 前 (supervisor スレッド未起動) は queued で拒否し、
    busy_since は None (dispatch していないので経過時間は無い)。"""
    sup = MissionSupervisor(trade_fn=lambda t: t, reflection_fn=lambda: 0,
                            ask_fn=lambda q: q, clock_fn=lambda: 50.0)
    assert sup.try_submit("trade", trigger="cron").accepted is True
    r = _submit_within(sup, 2.0, "trade", trigger="cron")
    assert (r.accepted, r.reason, r.busy_since, r.checked_at) == (
        False, "queued", None, 50.0)
    assert r.busy_elapsed_sec is None
    sup.fail_pending(exc=RuntimeError("drain"))
    assert sup.busy_since is None
    assert sup.try_submit("trade", trigger="cron").accepted is True


def test_slot_is_idle_before_future_callbacks_run():
    """future の完了 callback (supervisor スレッドで同期実行) の時点で
    スロットは既に空いている — result() が返ったら次を必ず受理できる。"""
    seen = []
    registered = threading.Event()

    def trade_fn(trigger):
        # callback の登録より先に完了させない (登録後に完了させると
        # callback は必ず supervisor スレッドの future 解決の中で走る)
        assert registered.wait(5.0)
        return trigger

    sup = MissionSupervisor(trade_fn=trade_fn, reflection_fn=lambda: 0,
                            ask_fn=lambda q: q)
    sup.start()
    first = sup.try_submit("trade", trigger="cron")
    done = threading.Event()

    def on_done(_future):
        r = sup.try_submit("ask", question="next")
        seen.append((r.accepted, r.reason, sup.busy_since))
        done.set()

    first.future.add_done_callback(on_done)
    registered.set()
    assert done.wait(5.0)
    assert seen == [(True, None, None)]
    sup.shutdown(drain_exc=RuntimeError("test"))


def test_shutdown_rejects_with_reason_shutdown_even_when_idle():
    sup = MissionSupervisor(trade_fn=lambda t: t, reflection_fn=lambda: 0,
                            ask_fn=lambda q: q, clock_fn=lambda: 7.0)
    sup.shutdown(drain_exc=RuntimeError("stop"))
    r = sup.try_submit("trade", trigger="cron")
    assert (r.accepted, r.reason, r.busy_since, r.future) == (
        False, "shutdown", None, None)


def test_trade_job_forwards_decision_bars_only_when_given():
    seen = []

    def trade_fn(trigger, **kwargs):
        seen.append((trigger, dict(kwargs)))
        return trigger

    sup = MissionSupervisor(trade_fn=trade_fn, reflection_fn=lambda: 0,
                            ask_fn=lambda q: q)
    sup.start()
    bars = MappingProxyType({("USDJPY", "15m"): 1})
    sup.try_submit("trade", trigger="cron", decision_bars=bars).future.result(timeout=5.0)
    sup.try_submit("trade", trigger="signal").future.result(timeout=5.0)
    assert seen == [("cron", {"decision_bars": bars}), ("signal", {})]
    sup.shutdown(drain_exc=RuntimeError("test"))


def test_dispatch_exception_is_delivered_through_the_future():
    """dispatch 中の例外は future の例外として呼び出し元に届き、スロットは
    空いて次を受理する。"""
    boom = RuntimeError("trade failed")

    def trade_fn(trigger):
        raise boom

    sup = MissionSupervisor(trade_fn=trade_fn, reflection_fn=lambda: 0,
                            ask_fn=lambda q: q)
    sup.start()
    first = sup.try_submit("trade", trigger="cron")
    with pytest.raises(RuntimeError, match="trade failed"):
        first.future.result(timeout=5.0)
    assert first.future.exception() is boom
    again = sup.try_submit("ask", question="q")
    assert again.accepted is True
    assert again.future.result(timeout=5.0) == "q"
    sup.shutdown(drain_exc=RuntimeError("test"))


def test_clock_is_read_only_while_holding_the_supervisor_lock():
    """busy_since (dispatch 開始) と checked_at (accepted/queued/running/
    shutdown の全結果) の時計は、どちらも MissionSupervisor の lock を
    保持した状態で読む (phase と時刻の組を 1 回の lock 区間で確定させる)。
    受理 (accepted) 時の checked_at も lock 内で読む — 拒否
    (running) 時だけでなく全分岐で同じ規律にする。"""
    holder = {}
    reads = []
    started = threading.Event()
    release = threading.Event()

    def clock():
        reads.append(holder["sup"]._lock.locked())
        return 100.0

    def trade_fn(trigger):
        started.set()
        assert release.wait(5.0)
        return trigger

    sup = MissionSupervisor(trade_fn=trade_fn, reflection_fn=lambda: 0,
                            ask_fn=lambda q: q, clock_fn=clock)
    holder["sup"] = sup
    sup.start()
    first = sup.try_submit("trade", trigger="cron")
    assert first.accepted is True and first.checked_at == 100.0
    assert started.wait(5.0)
    rejected = sup.try_submit("trade", trigger="cron")
    assert (rejected.accepted, rejected.reason, rejected.busy_since,
            rejected.checked_at) == (False, "running", 100.0, 100.0)
    release.set()
    first.future.result(timeout=5.0)
    # 3 回: try_submit(accepted) の checked_at / _run の busy_since /
    # try_submit(rejected) の checked_at。すべて lock 保持中の読み。
    assert len(reads) == 3
    assert reads == [True, True, True]
    sup.shutdown(drain_exc=RuntimeError("test"))


def test_shutdown_and_idle_submit_report_checked_at_from_the_clock():
    """shutdown で拒否された結果も checked_at を持つ (以前は
    None 固定 — IV-12 違反)。"""
    sup = MissionSupervisor(trade_fn=lambda t: t, reflection_fn=lambda: 0,
                            ask_fn=lambda q: q, clock_fn=lambda: 7.0)
    sup.shutdown(drain_exc=RuntimeError("stop"))
    r = sup.try_submit("trade", trigger="cron")
    assert (r.accepted, r.reason, r.busy_since, r.future, r.checked_at) == (
        False, "shutdown", None, None, 7.0)


def test_accepted_with_and_rejected_require_checked_at_explicitly():
    """`checked_at` の `time.monotonic()` フォールバックは注入時計を
    迂回できてしまう (IV-12 違反) — 省略時は `TypeError` で拒否する。"""
    with pytest.raises(TypeError):
        SubmitResult.accepted_with(None)
    with pytest.raises(TypeError):
        SubmitResult.rejected("shutdown")
    # 明示すれば通る (フォールバックが無いだけで正常系は壊れない)。
    accepted = SubmitResult.accepted_with(None, checked_at=0.0)
    assert accepted.checked_at == 0.0
    rejected = SubmitResult.rejected("shutdown", checked_at=0.0)
    assert rejected.checked_at == 0.0
