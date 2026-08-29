"""service.build_app が ImproveSupervisor を配線することの pin、および
run_service の shutdown/join シーケンスの検証 (プラン10 Task9-6/9-8)。

起動時回収 (Task 8 の `recover_interrupted` 拡張) の SQL 本体・
`missions.recover_interrupted` との同一 tx 検証は Task 8 のテストが担う。
ここでは build_app が ImproveSupervisor を構築して App.improve_supervisor に
格納すること、run_service の shutdown/join シーケンスに改善レーンの
ライフサイクル呼び出しが並ぶこと、および Scheduler.on_improve_tick /
Commands.improve_supervisor への実値配線は無いこと (Task 12 の担当) を
検証する。"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.improve_supervisor import ImproveSupervisor
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import (build_app, run_init, run_service,
                                _scheduler_tick_once)

NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)


def _init(tmp_path):
    """テスト用の初期化 (test_service_app.py の _init に倣う)。"""
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml.example").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        run_init(tmp_path)


def test_build_app_wires_improve_supervisor(tmp_path):
    """build_app が App.improve_supervisor を ImproveSupervisor インスタンスで
    初期化すること (9.6 Step 3)。"""
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        assert isinstance(app.improve_supervisor, ImproveSupervisor)
        assert app.improve_supervisor._capacity == \
            app.settings.improve.parallel
    finally:
        app.close()


def test_build_app_improve_supervisor_shares_stop_event(tmp_path):
    """build_app が improve_supervisor に stop_event を渡すこと (9.6 Step 3)。"""
    _init(tmp_path)
    stop_event = threading.Event()
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    stop_event=stop_event)
    try:
        assert app.improve_supervisor._stop_event is stop_event
    finally:
        app.close()


def test_scheduler_on_improve_tick_is_wired_after_task12(tmp_path):
    """Task 12 の活性化配線後、Scheduler.on_improve_tick は
    improve_supervisor.tick に束縛される (統合裁定 R-i9)。"""
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        assert app.scheduler.on_improve_tick.__self__ is app.improve_supervisor
    finally:
        app.close()


def test_commands_improve_supervisor_is_wired_after_task12(tmp_path):
    """Task 12 の活性化配線後、Commands.improve_supervisor は
    app.improve_supervisor と同一インスタンスになる (統合裁定 R-i9)。"""
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        assert app.commands.improve_supervisor is app.improve_supervisor
    finally:
        app.close()


def test_run_service_calls_improve_supervisor_shutdown_and_join(tmp_path):
    """run_service の shutdown シーケンスで improve_supervisor.shutdown() /
    .join() が supervisor.shutdown() / .join() と並んで呼ばれることを
    検証する (9.8 Step 3.5-7)。

    _stop_event を事前に set した Event を注入し、run_service が
    immediately に finally ブロック (shutdown 処理) へ到達するようにする。
    improve_supervisor をモック化して shutdown/join の呼び出しを
    検証する。"""
    _init(tmp_path)
    stop_event = threading.Event()
    stop_event.set()  # immediately trigger shutdown

    with patch("agentic_fx.service.build_app") as mock_build_app, \
         patch("agentic_fx.service.build_splash") as mock_splash, \
         patch("agentic_fx.service.Policy"):
        # Mock app with mock improve_supervisor and other required attributes
        mock_app = MagicMock()
        mock_app.improve_supervisor = MagicMock()
        mock_app.supervisor = MagicMock()
        mock_app.supervisor.shutdown = MagicMock()
        mock_app.supervisor.join = MagicMock()
        mock_app.supervisor.is_alive = MagicMock(return_value=False)
        mock_app.close = MagicMock(return_value=[])
        mock_app.activity.write = MagicMock()
        mock_app.fatal_reason = None

        mock_build_app.return_value = mock_app
        mock_splash.return_value = "splash"

        # Run service with pre-set stop event
        exit_code = run_service(tmp_path, _stop_event=stop_event)

        # Verify shutdown and join were called on improve_supervisor
        mock_app.improve_supervisor.shutdown.assert_called_once()
        mock_app.improve_supervisor.join.assert_called_once()

        # Verify supervisor was also shut down (sanity check)
        mock_app.supervisor.shutdown.assert_called_once()

        # L-F23 是正 (verified-local-round1.md §1【4】): shutdown() は
        # join() より前に呼ばれる (順序そのものは仕様 — 検収 B5 が join の
        # 位置を明示指定している)。上の assert_called_once() を 2 つ並べる
        # だけでは順序を見ていなかった (assert_called_once は呼び出し回数
        # だけを見る、順序非依存)。
        names = [c[0] for c in mock_app.improve_supervisor.mock_calls]
        assert names.index("shutdown") < names.index("join")

        # Exit code should be 0 (graceful shutdown)
        assert exit_code == 0


def test_scheduler_tick_returns_empty_list_when_on_improve_tick_is_none(tmp_path):
    """`on_improve_tick` が None のとき `Scheduler.tick()` の戻り値は常に
    空リスト (Scheduler 自体の契約 — Task 12 配線後も保たれる)。"""
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        app.scheduler.on_improve_tick = None
        pending = app.scheduler.tick(NOW)
        assert pending == []
    finally:
        app.close()


def test_on_improve_tick_fires_outside_core_lock(tmp_path):
    """検収 B2: `on_improve_tick` は core_lock 保持下では**判定のみ**され、
    実際の発火は `_scheduler_tick_once` が `with app.core_lock:` を抜けた
    後に行われる。`ImproveSupervisor.tick` は sqlite write + thread spawn
    を伴うため core_lock 保持下では実行できない — hook 内で
    `app.core_lock.acquire(blocking=False)` が True (= lock が空いている
    = lock 外での発火) を確認する。"""
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        fired = []

        def fake_on_improve_tick(now):
            acquired = app.core_lock.acquire(blocking=False)
            if acquired:
                app.core_lock.release()
            fired.append((now, acquired))

        app.scheduler.on_improve_tick = fake_on_improve_tick
        _scheduler_tick_once(app)

        assert len(fired) == 1
        now_seen, acquired = fired[0]
        assert acquired is True, (
            "on_improve_tick fired while app.core_lock was still held "
            "(must fire after _scheduler_tick_once releases core_lock)")
    finally:
        app.close()


def test_build_app_reconciles_report_outbox_at_startup(tmp_path):
    """F-1 是正 (検収 task12、§7.1-6): build_app の起動シーケンスが
    `ImproveLoop.reconcile_report_outbox` を呼ぶこと。「起動時 reconcile が
    published ⇔ 最終存在に収束させ孤児を消す」の production 配線が
    存在することの pin (`reconcile_report_outbox` を消す変異、または呼び
    出しを外す変異で red になるべき)。

    plugin switch の reconcile/sweep/expire と同じ並びに置く — その 3 本の
    呼び出し確認テスト (`test_service_startup_calls_reconcile_sweep_expire_
    then_approved_plugins_in_order`, tests/test_service_app.py) と対になる。
    """
    from agentic_fx.loops.improve_loop import ImproveLoop

    _init(tmp_path)
    with patch.object(ImproveLoop, "reconcile_report_outbox") as m_reconcile:
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
        try:
            m_reconcile.assert_called_once()
            args, kwargs = m_reconcile.call_args
            # L-F22 是正 (verified-local-round1.md §1【6】): 第 1 引数
            # (位置引数) は conn_core (Commands は conn_shell 束縛の broker
            # を持つ — シェルスレッドから conn_core を触らない設計意図の
            # 裏返し)。旧 assert は `_, kwargs = ...` で位置引数を丸ごと
            # 捨てており、conn_shell へ差し替える変異が無防備だった。
            assert args and args[0] is app.conn_core
            assert kwargs["reports_dir"] == tmp_path / "data" / "improve_reports"
            assert kwargs["now"] == NOW
        finally:
            app.close()


def test_build_app_startup_survives_report_outbox_reconcile_failure(tmp_path):
    """F-1 是正: `reconcile_report_outbox` が例外を出しても build_app は
    完走する (§5.3 と同じ規約 — 改善レーンの report 整合だけが成立せず
    取引は動く)。

    F-S11 是正 (段0 診断4): 旧 assert は `app is not None` しか見ておらず、
    `service.py:997` の except 節 (`activity.write(Category.IMPROVE,
    "improve_report_reconcile_failed", ...)` → `pass`) を落とす変異が
    生存していた — 「起動時に reconcile が黙って失敗する」ことそのものは
    通っても、失敗が記録されたか (診断の帰属) を見ていなかった
    (メモリ §6.13 と同型)。`app.activity.tail` で記録を確認する。"""
    from agentic_fx.loops.improve_loop import ImproveLoop

    _init(tmp_path)
    with patch.object(ImproveLoop, "reconcile_report_outbox",
                      side_effect=RuntimeError("db locked")):
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    assert app is not None
    tail = app.activity.tail(50)
    assert any("improve_report_reconcile_failed" in line for line in tail), (
        "reconcile 失敗が activity へ記録されていない (F-S11 の穴): "
        f"{tail!r}")
    app.close()
