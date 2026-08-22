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
from agentic_fx.service import build_app, run_init, run_service

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


def test_scheduler_on_improve_tick_is_none_at_task9(tmp_path):
    """Task 9 の時点では Scheduler.on_improve_tick は None のままであること。
    活性化配線 (実値の関数を渡す) は Task 12 の担当 (統合裁定 R-i9)。"""
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        assert app.scheduler.on_improve_tick is None
    finally:
        app.close()


def test_commands_improve_supervisor_is_none_at_task9(tmp_path):
    """Task 9 の時点では Commands.improve_supervisor は None のままであること。
    活性化配線は Task 12 の担当 (統合裁定 R-i9)。"""
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    try:
        assert app.commands.improve_supervisor is None
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
