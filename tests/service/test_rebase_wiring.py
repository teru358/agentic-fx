"""build_app が HWM 付け直しの依存 (core lock と PaperBroker) を OpsService へ渡すこと。
依存が無くても daemon (OpsService) は起動し、付け直しだけが使えない。"""
from __future__ import annotations

from datetime import datetime, timezone

from agentic_fx import service
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.ops.contracts import Principal
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import build_app
from tests.store.test_rag import FakeEmbedding
from tests.test_service_app import _init

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def _app(root):
    return build_app(root, runner=FakeRunner([]), clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())


def test_ops_service_shares_the_app_core_lock_and_the_runtime_broker(tmp_path):
    _init(tmp_path)
    app = _app(tmp_path)
    try:
        wiring = app.ops._rebase
        assert wiring is not None
        assert wiring.core_lock is app.core_lock
        # commit-core / MTM / 振り返り / econ の書き手と同じ 1 個の lock を共有する。
        assert app.trade_loop._core_lock is wiring.core_lock
        assert app.reflection._core_lock is wiring.core_lock
        assert app.econ_worker._core_lock is wiring.core_lock
        assert wiring.broker is app.broker and type(wiring.broker) is PaperBroker
        view = app.ops.rebase_preview(Principal.APPROVER, 0)
        assert view["reason"] != "rebase_unavailable"
    finally:
        app.close()


def test_daemon_builds_without_the_rebase_dependencies(tmp_path, monkeypatch):
    _init(tmp_path)
    real = service._build_ops_service

    def without(*args, **kwargs):
        kwargs.pop("core_lock", None)
        kwargs.pop("broker", None)
        return real(*args, **kwargs)

    monkeypatch.setattr(service, "_build_ops_service", without)
    app = _app(tmp_path)
    try:
        assert app.ops._rebase is None
        assert app.ops.rebase_preview(Principal.APPROVER, 0)["reason"] == "rebase_unavailable"
        assert app.ops.status(Principal.APPROVER) is not None
    finally:
        app.close()
