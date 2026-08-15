import sqlite3
import threading
from pathlib import Path
from shutil import copyfile
from unittest.mock import patch

import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops.trade_loop import gate_reject_streak
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import _scheduler_tick_once, build_app, run_init
from agentic_fx.store import alert_state, intents, missions
from agentic_fx.store.db import connect, init_db
from tests.loops.test_trade_loop import NOW, _loop
from tests.store.test_rag import FakeEmbedding


class _RecordingNotifier:
    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, text):
        if self.fail:
            raise RuntimeError("notify failed")
        self.sent.append(text)


class _LockProbingNotifier(_RecordingNotifier):
    """`core_lock` 保持中に通知する変異を殺すための notifier
    (着手前検証 2026-08-15 で追加)。

    ⚠️ **別スレッド**から取得を試すこと。`core_lock` は `RLock` なので、
    同一スレッドで `acquire(blocking=False)` すると**保持中でも成功する**
    ため検査が vacuous になる (実測で確認)。"""
    def __init__(self, core_lock):
        super().__init__()
        self._core_lock = core_lock
        self.lock_was_held = None

    def send(self, text):
        got = []

        def _probe():
            acquired = self._core_lock.acquire(timeout=0.3)
            got.append(acquired)
            if acquired:
                self._core_lock.release()

        t = threading.Thread(target=_probe)
        t.start()
        t.join()
        self.lock_was_held = not got[0]
        return super().send(text)


class _RejectTradeIntentReads:
    """alert_state の get/set は通し、判定 SQL を conn_core で読む変異だけ殺す。"""
    def __init__(self, real):
        self.real = real

    def execute(self, sql, *args, **kwargs):
        if "FROM trade_intents" in str(sql):
            raise AssertionError("trade_intents must use conn_supervisor")
        return self.real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.real, name)


def _conn(tmp_path):
    c = connect(tmp_path / "alerts.db")
    init_db(c)
    return c


def _intent(c, action, gate_result, reject_category):
    mid = missions.start(c, "trade", "local", "test", NOW)
    iid = intents.insert(c, mid, {"action": action}, NOW, action=action)
    intents.set_gate_result(
        c, iid, accepted=gate_result == "accepted",
        reject_reason=None if gate_result == "accepted" else "test rejection",
        reject_category=reject_category)
    return iid


def _settings_with_threshold(settings, threshold):
    return settings.model_copy(update={
        "alert": settings.alert.model_copy(update={
            "consecutive_gate_reject": threshold})})


def _completed_hold_loop(tmp_path, threshold=10, notifier=None):
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    loop.settings = _settings_with_threshold(loop.settings, threshold)
    loop.executor.settings = loop.settings
    loop.notifier = notifier if notifier is not None else _RecordingNotifier()
    return conn, loop, runner, tp


def _loop_with_threshold(tmp_path, threshold):
    conn, loop, _, _ = _completed_hold_loop(tmp_path, threshold)
    notifier = loop.notifier
    return loop, conn, loop._conn_supervisor, notifier


def _app_with_threshold(tmp_path, threshold):
    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    copyfile(Path(__file__).resolve().parents[2] /
             "config" / "settings.yaml.example",
             root / "config" / "settings.yaml.example")
    with patch("agentic_fx.service.PriceProvider") as provider, \
         patch("agentic_fx.service._check_llama_swap"):
        provider.return_value.healthcheck.return_value = "test"
        run_init(root)
        app = build_app(root, runner=FakeRunner([]), clock=FixedClock(NOW),
                        embedding_fn=FakeEmbedding())
    app.settings = _settings_with_threshold(app.settings, threshold)
    app.trade_loop.settings = app.settings
    app.executor.settings = app.settings
    app.notifier = _RecordingNotifier()
    app.trade_loop.notifier = app.notifier
    return app


def _inject_alert_failure(loop, stage, monkeypatch):
    if stage == "evaluate":
        monkeypatch.setattr(
            "agentic_fx.loops.trade_loop.gate_reject_streak",
            # ⚠️ (着手前検証 2026-08-15) 3 stage すべてを RuntimeError にすると
            # `except Exception` → `except RuntimeError` の変異が生存する
            # (実測 SURVIVED)。1 stage は非 RuntimeError にする。
            lambda conn: (_ for _ in ()).throw(
                sqlite3.OperationalError("evaluate")))
    elif stage == "notify":
        loop.notifier.fail = True
    else:
        monkeypatch.setattr(
            alert_state, "set",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("state_update")))


def test_streak_id_is_previous_accepted_open_id(tmp_path):
    """⚠️ accepted を **2 行**作ること。1 行だと `MAX(id)` と `MIN(id)` が
    同値になり **`MAX`→`MIN` 変異が生き残る**。この変異は致命的で、`MIN` だと
    streak_id が最初の accepted に固定され続け、**accepted が入っても連続が
    切れなくなる** (= 取引できているのに通知が鳴り続ける)。
    また `risk_gate` と `execution` を**同数にしない** — SQL ③ の
    `ORDER BY c DESC` の勝者がタイでは非決定的になるため。"""
    c = _conn(tmp_path)
    _intent(c, "open", "accepted", None)                 # 古い accepted (id=1)
    _intent(c, "open", "rejected", "risk_gate")          # これは数えてはいけない
    accepted = _intent(c, "open", "accepted", None)      # 最新 accepted (id=3)
    _intent(c, "open", "rejected", "risk_gate")
    _intent(c, "open", "rejected", "risk_gate")
    _intent(c, "open", "rejected", "execution")          # risk_gate 2 : execution 1
    assert gate_reject_streak(c) == (accepted, 3, "risk_gate")


def test_dominant_category_is_the_majority_not_a_tie(tmp_path):
    """支配的カテゴリが**多数派**で決まることを単独で pin する
    (件数に差を付けてタイの非決定性を持ち込まない)。"""
    c = _conn(tmp_path)
    _intent(c, "open", "rejected", "risk_gate")
    _intent(c, "open", "rejected", "execution")
    _intent(c, "open", "rejected", "execution")
    assert gate_reject_streak(c)[2] == "execution"


def test_dominant_category_is_deterministic_on_a_tie(tmp_path):
    """(着手前検証 2026-08-15 で追加) SQL ③ の `reject_category ASC`
    タイブレークの pin。これが無いと同数タイの勝者が SQLite 任せになり、
    通知文面が非決定的になる (タイブレーク削除変異はこのテストでのみ死ぬ)。"""
    c = _conn(tmp_path)
    _intent(c, "open", "rejected", "risk_gate")
    _intent(c, "open", "rejected", "execution")
    assert gate_reject_streak(c)[2] == "execution"    # ASC で execution < risk_gate


def test_streak_count_does_not_filter_reject_category(tmp_path):
    c = _conn(tmp_path)
    _intent(c, "open", "rejected", "risk_gate")
    _intent(c, "open", "rejected", "execution")
    assert gate_reject_streak(c)[1] == 2


def test_close_rejection_is_not_counted(tmp_path):
    c = _conn(tmp_path)
    _intent(c, "close", "rejected", "execution")
    assert gate_reject_streak(c)[1] == 0


def test_threshold_notifies_once_per_accepted_streak(tmp_path):
    loop, core, ro, notifier = _loop_with_threshold(tmp_path, threshold=2)
    _intent(core, "open", "rejected", "risk_gate")
    _intent(core, "open", "rejected", "execution")
    loop._notify_gate_reject_streak()
    loop._notify_gate_reject_streak()
    assert len(notifier.sent) == 1
    assert "2 件連続" in notifier.sent[0]


def test_below_threshold_does_not_notify(tmp_path):
    """(着手前検証 2026-08-15 で追加) **閾値判定そのものを削除する変異**の
    killer。旧対応先の `test_threshold_notifies_once_per_accepted_streak` は
    threshold=2 / count=2 なので判定を消しても通知は 1 回のままで green
    (実測 SURVIVED)。境界の逆方向 (`<` → `<=`) は上のテストが殺すので、
    両方向をカバーするには 2 本要る。"""
    loop, core, ro, notifier = _loop_with_threshold(tmp_path, threshold=3)
    _intent(core, "open", "rejected", "risk_gate")
    _intent(core, "open", "rejected", "risk_gate")
    loop._notify_gate_reject_streak()
    assert notifier.sent == []


def test_new_accepted_open_rearms_notification(tmp_path):
    loop, core, ro, notifier = _loop_with_threshold(tmp_path, threshold=1)
    _intent(core, "open", "rejected", "risk_gate")
    loop._notify_gate_reject_streak()
    _intent(core, "open", "accepted", None)
    _intent(core, "open", "rejected", "execution")
    loop._notify_gate_reject_streak()
    assert len(notifier.sent) == 2


def test_notification_failure_does_not_update_streak_id(tmp_path):
    """⚠️ これは**将来 raise する notifier 実装向けの順序契約の pin**である
    (着手前検証 2026-08-15)。現行 `core/notifier.py` の `send()` は
    `except Exception` で全失敗を握り潰し `discord.enabled=false` (example の
    既定) では即 return するため、**production ではこの分岐に入らない**。
    固定しているのは「通知 → ラッチ更新の順序」であって到達保証ではない。"""
    loop, core, ro, notifier = _loop_with_threshold(tmp_path, threshold=1)
    _intent(core, "open", "rejected", "risk_gate")
    notifier.fail = True
    loop._notify_gate_reject_streak()
    assert alert_state.get(core, alert_state.GATE_REJECT_STREAK_KEY) is None


def test_gate_alert_reads_only_conn_supervisor(tmp_path, monkeypatch):
    """`loop.conn` と `loop._conn_supervisor` は `_loop` fixture では同一
    オブジェクトだが**属性束縛は別**なので、この monkeypatch は
    `_conn_supervisor` に影響しない (実測で確認 — 恒真にならない)。"""
    loop, core, ro, notifier = _loop_with_threshold(tmp_path, threshold=1)
    _intent(core, "open", "rejected", "risk_gate")
    monkeypatch.setattr(loop, "conn", _RejectTradeIntentReads(core))
    loop._notify_gate_reject_streak()
    assert notifier.sent


def test_gate_alert_notification_is_sent_without_holding_core_lock(tmp_path):
    """(着手前検証 2026-08-15 で追加) 呼び出しを `with self._core_lock:` で
    包む / `notifier.send` を lock 区間へ入れる変異の killer。Global
    Constraints「`core_lock` を保持したまま外部 I/O をしない」の直接違反が、
    **旧テスト群 16 本では全スイート green で生存していた** (実測)。
    `test_gate_alert_reads_only_conn_supervisor` は接続しか見ない。

    ⚠️ **罠が 2 つある (両方とも着手前検証で実際に踏んだ)**:
    ①`core_lock` は `RLock` なので同一スレッドの `acquire` は保持中でも
      成功する → `_LockProbingNotifier` が別スレッドから試す。
    ②**メソッドを直接呼ぶと変異が生存する** — 呼び出し元 (commit-post) を
      lock 内へ移す変異は直呼びテストを一切通らない。`run_once()` を通すこと。"""
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    loop.settings = _settings_with_threshold(loop.settings, 1)
    loop.executor.settings = loop.settings
    notifier = _LockProbingNotifier(loop._core_lock)
    loop.notifier = notifier
    _intent(conn, "open", "rejected", "risk_gate")
    loop.run_once()
    assert notifier.sent, "通知が出ていない (前提条件)"
    assert notifier.lock_was_held is False


# ⚠️ ここに旧 test_gate_alert_notifier_is_never_called_on_scheduler_thread を
# 置いてはならない — **vacuous (どんな実装でも PASS する) だった**。
# `threading.Thread(target=loop._notify_gate_reject_streak)` でメソッドを直接
# 別スレッドから呼ぶため、notifier 内の `threading.get_ident()` は決してテスト
# 実行スレッドの ident と一致せず、`pytest.fail` ガードは構造的に発火不能
# だった。しかも殺すべき変異は「**呼び出し元**を scheduler へ移す」ことで
# あり、実際の呼び出し元を一切通らないこのテストでは変異が生存する。
#
# 正しい pin は「**scheduler tick 経路を実際に走らせて notifier が呼ばれない**」
# ことと「**commit-post 経路では呼ばれる**」ことの対を見ることである。

def test_scheduler_tick_never_notifies_gate_reject_streak(tmp_path):
    """呼び出し元の pin (本命)。閾値を超える却下が既にある状態で scheduler の
    tick を実運用と同じ経路で回し、**通知が 1 件も出ない**ことを見る。
    `_notify_gate_reject_streak` を maintenance / tick へ移す変異はここで死ぬ
    (着手前検証で `service.py:800` へ lock 外・lock 内の 2 通り注入し、
    どちらも KILLED・狙ったテストだけが red であることを実測)。"""
    app = _app_with_threshold(tmp_path, threshold=1)
    _intent(app.conn_core, "open", "rejected", "risk_gate")
    _scheduler_tick_once(app)          # 実配線 (service.py) をそのまま使う
    assert app.notifier.sent == []


def test_scheduler_tick_actually_runs_the_tick_body(tmp_path):
    """(着手前検証 2026-08-15 で追加) positive control。上の否定側テストが
    「tick が何もしていないから緑」になっていないことを示す。この 1 本が
    無いと、否定側は vacuous になっても誰も気づけない。"""
    app = _app_with_threshold(tmp_path, threshold=1)
    seen = []
    real_tick = app.scheduler.tick
    app.scheduler.tick = lambda now: (seen.append(now), real_tick(now))[1]
    _scheduler_tick_once(app)
    assert seen == [app.clock.now()]


def test_commit_post_notifies_gate_reject_streak(tmp_path):
    """対になる肯定側の pin。commit-post 経路では実際に通知が出ることを見る
    (上のテストだけだと「どこからも呼ばない」実装でも green になるため)。"""
    conn, loop, runner, tp = _completed_hold_loop(tmp_path, threshold=1)
    _intent(conn, "open", "rejected", "risk_gate")
    loop.run_once()
    assert len(loop.notifier.sent) == 1


@pytest.mark.parametrize("stage", ["evaluate", "notify", "state_update"])
def test_commit_post_alert_exception_never_changes_finalized_mission_to_failed(
        tmp_path, stage, monkeypatch):
    conn, loop, runner, tp = _completed_hold_loop(tmp_path, threshold=1)
    _intent(conn, "open", "rejected", "risk_gate")
    _inject_alert_failure(loop, stage, monkeypatch)
    assert loop.run_once() == {"result": "hold", "order_id": None, "reasons": []}
    assert conn.execute("SELECT status FROM missions ORDER BY id DESC LIMIT 1").fetchone()[0] == "completed"
