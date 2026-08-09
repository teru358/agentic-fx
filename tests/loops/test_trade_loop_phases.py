"""TradeLoop 五相再構成の lock 境界 (プラン8, 設計書 §3.1)。"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agentic_fx.runners.base import Mission, MissionResult
from agentic_fx.store import orders, signals
from tests.loops.test_trade_loop import NOW, QUOTE, SETTINGS, _loop


class _RecordingNotifier:
    """記録型スタブ。「呼ばれたら raise」は except Exception に飲まれる
    ので使わない。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


# レビュー 1 周目 codex B2 (指揮者も前回報告で同じ弱点を発見): 旧
# `_SlowRunner` / `test_run_once_does_not_hold_core_lock_during_runner_run`
# は runner.run() 自身の中 (= run_once を呼んだのと同じスレッド) で
# `core_lock.acquire(blocking=False)` を試していた。RLock は同一スレッド
# からの acquire が常に成功する (再入可能) ため、run 相を `with
# self._core_lock:` で誤って包む変異を入れても検出できない恒真に近い
# テストだった (指揮者の前回報告・codex 双方が指摘)。
#
# 直後の `test_scheduler_tick_can_acquire_lock_while_worker_runner_blocks`
# が既に「別スレッドの checker から core_lock を取得できる」形で同じ
# 不変条件 (run 相は lock 非保持) を正しく検証しているため、役割が完全に
# 重複する弱いテストは削除した (指揮者裁定)。


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


def test_requeue_signal_does_not_send_notification_itself(tmp_path):
    """codex A1 (レビュー 1 周目・最重要): `_requeue_signal` 自体は通知を
    送らず、文面を返すだけであることを直接確認する。Notifier.send は
    urlopen(timeout=10) の同期実行なので、lock 保持中に呼ぶと SL/TP 監視が
    最大 10 秒止まる (`_requeue_signal` は core_lock 保持中に呼ばれる)。"""
    conn, loop, runner, tp = _loop(tmp_path, [])
    recording = _RecordingNotifier()
    loop.notifier = recording

    max_requeue = SETTINGS.plugin.signal_requeue_max
    sid = signals.add(
        conn, plugin="sig1", content_hash="h1", pair="USDJPY",
        timeframe="1h", bar_ts=NOW.isoformat(), kind="signal",
        payload={"direction": "long", "strength": 0.7, "rationale": "up"},
        now=NOW)
    for _ in range(max_requeue):
        claimed = signals.claim_oldest(conn, mission_id=1, now=NOW,
                                       freshness_bars=None)
        assert claimed is not None, "claim できなかった (テスト前提が崩れている)"
        signals.requeue(conn, sid, now=NOW, max_requeue=max_requeue)
    claimed = signals.claim_oldest(conn, mission_id=1, now=NOW,
                                   freshness_bars=None)
    assert claimed is not None

    msg = loop._requeue_signal(claimed)

    assert msg is not None and "abandoned" in msg
    assert recording.sent == [], (
        "_requeue_signal 自体は通知を送ってはならない (呼び出し元が"
        "lock 解放後に送る)")


def test_signal_prepare_exception_after_claim_requeues_signal(tmp_path):
    """codex A2 (レビュー 1 周目): claim 成功後、set_trigger/build_prompt
    等 prepare の残りで例外が出ても claimed signal が回収され、mission が
    running のまま残らないことを確認する (以前は try/finally が prepare の
    外側にしか無く、この区間の例外は回収されなかった)。"""
    conn, loop, runner, tp = _loop(tmp_path, [])
    sid = signals.add(
        conn, plugin="sig1", content_hash="h1", pair="USDJPY",
        timeframe="1h", bar_ts=NOW.isoformat(), kind="signal",
        payload={"direction": "long", "strength": 0.7, "rationale": "up"},
        now=NOW)

    with patch("agentic_fx.loops.trade_loop.missions.set_trigger",
              side_effect=RuntimeError("set_trigger_boom")):
        with pytest.raises(RuntimeError):
            loop._run_once_impl("signal")

    row = conn.execute(
        "SELECT status, requeue_count FROM signals WHERE id=?",
        (sid,)).fetchone()
    assert row["status"] == "pending", "claimed signal が回収されていない"
    assert row["requeue_count"] == 1
    m = conn.execute(
        "SELECT status FROM missions ORDER BY id DESC LIMIT 1").fetchone()
    assert m["status"] == "failed", "mission が running のまま残っている"


def test_commit_pre_unexpected_exception_finalizes_mission(tmp_path):
    """codex A3 (レビュー 1 周目): commit-pre の想定外例外
    (`_read_exposure_pairs` 等、既存の try/except に入っていない箇所) でも
    mission が running のまま残らないことを確認する。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed",
        {"action": "open", "pair": "USDJPY", "direction": "long",
         "entry_type": "market", "horizon": "day", "limit_price": None,
         "expires_in": None, "stop_loss": 147.80, "take_profit": 149.00,
         "reasoning": "x"}, [])])
    loop._read_exposure_pairs = MagicMock(
        side_effect=RuntimeError("exposure_boom"))

    with pytest.raises(RuntimeError):
        loop._run_once_impl("cron")

    m = conn.execute(
        "SELECT status FROM missions ORDER BY id DESC LIMIT 1").fetchone()
    assert m["status"] == "failed", "mission が running のまま残っている"


def test_ask_prepare_run_unexpected_exception_finalizes_mission(tmp_path):
    """codex A3 (レビュー 1 周目): ask 経路でも prepare/run 相の想定外例外
    (watch.begin 等) で mission が running のまま残らないことを確認する。"""
    conn, loop, runner, tp = _loop(tmp_path, [])
    loop.watch.begin = MagicMock(side_effect=RuntimeError("watch_boom"))

    with pytest.raises(RuntimeError):
        loop._ask_once_impl("質問？")

    m = conn.execute(
        "SELECT status FROM missions ORDER BY id DESC LIMIT 1").fetchone()
    assert m["status"] == "failed", "mission が running のまま残っている"


def test_commit_pre_snapshot_failure_becomes_recorded_gate_rejection(tmp_path):
    """指揮者の変異スイープ C1: commit-pre の snapshot 取得失敗
    (`snapshot_error`) が、汎用の `intent_execution_failed` ではなく
    ライブ経路と同じ形の「記録済み gate 拒否」になることを確認する
    (Task 14 からの申し送りの回収 — 守るテストが 1 本も無く SURVIVED して
    いた)。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed",
        {"action": "open", "pair": "USDJPY", "direction": "long",
         "entry_type": "market", "horizon": "day", "limit_price": None,
         "expires_in": None, "stop_loss": 147.80, "take_profit": 149.00,
         "reasoning": "x"}, [])])
    loop.executor.gather_open_snapshot = MagicMock(
        side_effect=RuntimeError("snapshot_boom"))

    out = loop.run_once("cron")

    assert out is not None
    assert out["result"] == "rejected"
    iid_row = conn.execute(
        "SELECT gate_result FROM trade_intents ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert iid_row["gate_result"] == "rejected"
    act_text = (tp / "a.log").read_text(encoding="utf-8")
    assert "gate_rejected" in act_text
    assert "intent_execution_failed" not in act_text, (
        "commit-pre のスナップショット失敗が汎用の intent_execution_failed "
        "に落ちている (記録済み gate 拒否になっていない)")


def test_commit_post_drain_sends_deferred_notification(tmp_path):
    """指揮者の変異スイープ C2: commit-core で溜めた通知が commit-post で
    実際に送信されることを確認する (遅延の仕組み自体は pin されていたが、
    出口の drain ループには専用テストが無く SURVIVED していた)。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "x"}, [])])
    recording = _RecordingNotifier()
    loop.notifier = recording
    loop.executor.record_and_validate_intent = MagicMock(
        side_effect=RuntimeError("dispatch_boom"))

    out = loop.run_once("cron")

    assert out is None
    assert len(recording.sent) == 1, (
        "commit-core で溜めた通知が commit-post で送信されていない")
    assert "注文処理失敗" in recording.sent[0]


def test_prepare_phase_holds_core_lock(tmp_path):
    """指揮者の変異スイープ C3: prepare 相 (missions.start/
    signals.claim_oldest 等) は core_lock を保持したまま実行される
    (commit-core は `test_commit_core_holds_core_lock` で pin 済みだが、
    prepare 相には専用テストが無く SURVIVED していた)。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "x"}, [])])

    entered = threading.Event()
    proceed = threading.Event()
    from agentic_fx.store import missions as missions_store
    original_start = missions_store.start

    def spy_start(*args, **kwargs):
        entered.set()
        assert proceed.wait(5.0), "checker スレッドが確認を完了しなかった"
        return original_start(*args, **kwargs)

    with patch("agentic_fx.loops.trade_loop.missions.start", spy_start):
        t = threading.Thread(target=lambda: loop.run_once("cron"), daemon=True)
        t.start()
        assert entered.wait(5.0), "prepare (missions.start) に到達しなかった"

        acquired: list[bool] = []
        checker = threading.Thread(
            target=lambda: acquired.append(
                loop._core_lock.acquire(blocking=False)))
        checker.start()
        checker.join(timeout=5.0)
        if acquired and acquired[0]:
            loop._core_lock.release()
        assert acquired == [False], (
            "prepare 実行中は他スレッドから core_lock を取得できないはず")

        proceed.set()
        t.join(timeout=5.0)
        assert not t.is_alive()


def test_ask_prepare_phase_holds_core_lock(tmp_path):
    """指揮者の変異スイープ C4: ask の prepare 相も core_lock を保持したまま
    実行される (SURVIVED していた)。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"answer": "test"}, [])])

    entered = threading.Event()
    proceed = threading.Event()
    from agentic_fx.store import missions as missions_store
    original_start = missions_store.start

    def spy_start(*args, **kwargs):
        entered.set()
        assert proceed.wait(5.0), "checker スレッドが確認を完了しなかった"
        return original_start(*args, **kwargs)

    with patch("agentic_fx.loops.trade_loop.missions.start", spy_start):
        t = threading.Thread(target=lambda: loop.ask_once("質問？"), daemon=True)
        t.start()
        assert entered.wait(5.0), "ask prepare (missions.start) に到達しなかった"

        acquired: list[bool] = []
        checker = threading.Thread(
            target=lambda: acquired.append(
                loop._core_lock.acquire(blocking=False)))
        checker.start()
        checker.join(timeout=5.0)
        if acquired and acquired[0]:
            loop._core_lock.release()
        assert acquired == [False], (
            "ask prepare 実行中は他スレッドから core_lock を取得できないはず")

        proceed.set()
        t.join(timeout=5.0)
        assert not t.is_alive()


def test_signals_consume_failure_is_caught_and_recorded(tmp_path):
    """レビュー 2 周目 codex D3: `signals.consume` (fail-closed — CAS
    不一致で ValueError) が commit-core の `try:` の**外**にあると、
    例外が `with self._core_lock, defer_notifications():` ブロックを
    丸ごと素通りし、`record_and_validate_intent` すら呼ばれないまま
    completed した有効な intent が DB に痕跡を残さず捨てられていた。
    `try:` の内側に移した後は、①少なくとも `intent_execution_failed` が
    activity に残る ②signal が requeue される ③mission が終端される
    ことを確認する。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "x"}, [])])
    sid = signals.add(
        conn, plugin="sig1", content_hash="h1", pair="USDJPY",
        timeframe="1h", bar_ts=NOW.isoformat(), kind="signal",
        payload={"direction": "long", "strength": 0.7, "rationale": "up"},
        now=NOW)

    with patch("agentic_fx.loops.trade_loop.signals.consume",
              side_effect=ValueError("signal not claimed by mission")):
        out = loop.run_once("signal")

    assert out is None
    act_text = (tp / "a.log").read_text(encoding="utf-8")
    assert "intent_execution_failed" in act_text, (
        "signals.consume の例外が commit-core の try 外へ素通りし、"
        "activity に何も残らないまま公開境界の汎用失敗になっている")
    row = conn.execute(
        "SELECT status, requeue_count FROM signals WHERE id=?",
        (sid,)).fetchone()
    assert row["status"] == "pending", "claimed signal が回収されていない"
    assert row["requeue_count"] == 1
    m = conn.execute(
        "SELECT status FROM missions ORDER BY id DESC LIMIT 1").fetchone()
    # 注: _finalize_mission は runner の MissionResult.status (ここでは
    # FakeRunner が返した "completed") で finalize する — 失敗したのは
    # 「completed した Mission の intent 執行」であって Mission 自体の
    # 判断ではないため、mission 行は "completed" になる。ここでの主張は
    # 「running のまま残らない」こと。
    assert m["status"] != "running", "mission が running のまま残っている"
    assert m["status"] == "completed"
