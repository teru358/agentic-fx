"""TradeLoop 五相再構成の lock 境界 (プラン8, 設計書 §3.1)。"""
from __future__ import annotations

import threading
import time

import pytest

from agentic_fx.runners.base import Mission, MissionResult
from agentic_fx.store import orders
from tests.loops.test_trade_loop import NOW, QUOTE, _loop


class _SlowRunner:
    """runner.run() が core_lock 非保持で呼ばれることを検証する fake —
    run() の中で「別スレッドが core_lock を取得できるか」を確認する。"""

    def __init__(self, core_lock: threading.RLock, result: MissionResult) -> None:
        self._core_lock = core_lock
        self._result = result
        self.lock_was_free_during_run = False

    def run(self, mission: Mission) -> MissionResult:
        acquired = self._core_lock.acquire(blocking=False)
        if acquired:
            self.lock_was_free_during_run = True
            self._core_lock.release()
        return self._result


def test_run_once_does_not_hold_core_lock_during_runner_run(tmp_path):
    """設計書 §3.1: run 相 (runner.run) は core_lock を保持しない —
    別スレッド (ここでは runner.run 自身の中) が同じロックを取得できる
    ことで検証する。"""
    conn, loop, runner, tp = _loop(tmp_path, [])
    slow = _SlowRunner(loop._core_lock, MissionResult(
        "completed", {"action": "hold", "reasoning": "x"}, []))
    loop.runner = slow

    loop.run_once("cron")

    assert slow.lock_was_free_during_run is True


def test_scheduler_tick_can_acquire_lock_while_worker_runner_blocks(tmp_path):
    """統合的な確認: run_once を別スレッドで実行中、メインスレッドが
    core_lock を (scheduler tick が行うのと同じ形で) 取得できる。"""
    conn, loop, runner, tp = _loop(tmp_path, [])
    release = threading.Event()

    class BlockingRunner:
        def run(self, mission):
            release.wait(5.0)
            return MissionResult("completed",
                                 {"action": "hold", "reasoning": "x"}, [])

    loop.runner = BlockingRunner()
    t = threading.Thread(target=lambda: loop.run_once("cron"), daemon=True)
    t.start()
    time.sleep(0.1)  # run_once が prepare を終えて run 相に入るまで待つ

    acquired = loop._core_lock.acquire(timeout=1.0)
    assert acquired, "core_lock は run 相の間、他スレッドから取得できるはず"
    loop._core_lock.release()

    release.set()
    t.join(timeout=5.0)


def test_scheduler_tick_can_acquire_lock_while_close_quote_fetch_blocks(tmp_path):
    """裁定書 F-1 (CR-2/P8-01) の回帰ピン: CLOSE intent の quote 取得
    (gather_close_snapshot) は commit-pre (lock 非保持) で行われるため、
    quote_fn がブロックしていても scheduler tick は core_lock を取得できる
    (既定構成の yfinance には timeout が効かないため、この lock-free 化
    自体が安全性の担保になる)。"""
    conn, loop, runner, tp = _loop(tmp_path, [])
    order_id = orders.insert(
        conn, pair="USDJPY", direction="long", entry_type="market",
        horizon="day", status="open", now=NOW, quantity=0.1,
        avg_fill_price=148.50)
    runner._results = [MissionResult(
        "completed", {"action": "close", "order_id": order_id,
                      "reasoning": "x"}, [])]

    release = threading.Event()

    def blocking_quote_fn(pair):
        release.wait(5.0)
        return QUOTE

    loop.executor.quote_fn = blocking_quote_fn
    t = threading.Thread(target=lambda: loop.run_once("cron"), daemon=True)
    t.start()
    time.sleep(0.1)  # commit-pre の gather_close_snapshot がブロック中

    acquired = loop._core_lock.acquire(timeout=1.0)
    assert acquired, ("core_lock は CLOSE の quote 取得中も他スレッドから"
                      "取得できるはず (commit-pre は lock 非保持)")
    loop._core_lock.release()

    release.set()
    t.join(timeout=5.0)


def test_commit_core_holds_core_lock(tmp_path):
    """設計書 §3.1: commit-core 相 (consume/Risk Gate/執行/finish) は
    core_lock を保持したまま実行される。run 相が lock 非保持であることと
    対になる不変条件で、**こちらが崩れると DB 書込が無保護になる**。

    executor の呼び出しを spy で捕まえて「実行中」に留め、別スレッドから
    core_lock を取れないことを確認する (RLock は同一スレッドからは常に
    取れてしまうため、必ず別スレッドで確かめる)。
    """
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "x"}, [])])

    entered = threading.Event()
    proceed = threading.Event()
    original = loop.executor.record_and_validate_intent

    def spy(*args, **kwargs):
        entered.set()
        assert proceed.wait(5.0), "checker スレッドが確認を完了しなかった"
        return original(*args, **kwargs)

    loop.executor.record_and_validate_intent = spy
    t = threading.Thread(target=lambda: loop.run_once("cron"), daemon=True)
    t.start()
    assert entered.wait(5.0), "commit-core に到達しなかった"

    acquired: list[bool] = []
    checker = threading.Thread(
        target=lambda: acquired.append(
            loop._core_lock.acquire(blocking=False)))
    checker.start()
    checker.join(timeout=5.0)
    if acquired and acquired[0]:
        loop._core_lock.release()
    assert acquired == [False], (
        "commit-core 実行中は他スレッドから core_lock を取得できないはず")

    proceed.set()
    t.join(timeout=5.0)
    assert not t.is_alive()


def test_read_exposure_pairs_covers_full_exposure_status_set(tmp_path):
    """指揮者追加の変異ピン (Step 11 下限リスト項目 3):
    `_read_exposure_pairs` が `executor._EXPOSURE` (OPEN 以外の未解決状態も
    含む全状態) を使うことを直接検証する。`(S.OPEN,)` のみに縮退させる
    変異はこのテストで red になる — 全体テストスイートには専用の
    integration pin が無く、その変異は統合的には SURVIVED した
    (指揮者が実測で確認済み。報告書に記載)。"""
    from agentic_fx.core.contracts import OrderStatus as S
    conn, loop, runner, tp = _loop(tmp_path, [])
    orders.insert(conn, pair="USDJPY", direction="long", entry_type="market",
                  horizon="day", status=S.OPEN, now=NOW, quantity=0.1,
                  avg_fill_price=148.20)
    orders.insert(conn, pair="EURUSD", direction="long", entry_type="limit",
                  horizon="day", status=S.PENDING_FILL, now=NOW,
                  quantity=0.1, requested_price=1.10)

    pairs = loop._read_exposure_pairs()

    assert pairs == ["EURUSD", "USDJPY"]
