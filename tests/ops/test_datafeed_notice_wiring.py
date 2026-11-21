"""daemon の配線: 組み立てた OpsService が通知の生成を有効にし、tick の回収点から回る。"""
from __future__ import annotations

from unittest.mock import MagicMock

from agentic_fx import service as service_mod

from .test_service_api_wiring import _app
from tests.test_service_app import _init


def test_the_daemon_builds_the_ops_service_with_the_datafeed_notices_enabled(tmp_path):
    _init(tmp_path)
    app = _app(tmp_path)
    assert app.ops is not None and app.ops._datafeed_notices is True


def test_the_per_tick_sweep_reaches_the_datafeed_sweep(tmp_path):
    _init(tmp_path)
    app = _app(tmp_path)
    app.ops.emit_pending_notices = MagicMock()
    service_mod._sweep_improve_notices(app)
    app.ops.emit_pending_notices.assert_called_once()
