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
