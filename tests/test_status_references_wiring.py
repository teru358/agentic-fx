"""status の `references` が実 build_app の status_provider から出る。"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml

from agentic_fx.core.contracts import FixedClock
from agentic_fx.ops import client
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import build_app
from tests.store.test_rag import FakeEmbedding
from tests.test_service_app import _init

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _app(root: Path):
    return build_app(root, runner=FakeRunner([]), clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())


def _watch(root: Path, symbols) -> None:
    path = root / "config" / "settings.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    feed = raw["datafeed"]
    feed["primary"] = "mt5"
    feed["yfinance"] = {"enabled": False}
    feed["mt5"] = {"enabled": True, "bridge_url": "http://localhost:8812"}
    feed["watch_symbols"] = list(symbols)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")


def test_status_has_no_references_when_nothing_is_watched(tmp_path):
    _init(tmp_path)
    app = _app(tmp_path)
    try:
        assert "references" not in app.ops._status_provider()
    finally:
        app.close()


def test_a_watched_symbol_without_a_strategy_has_no_row_but_the_status_still_builds(tmp_path):
    _init(tmp_path)
    _watch(tmp_path, ["JP225"])
    app = _app(tmp_path)
    try:
        status = app.ops._status_provider()
        assert "references" not in status  # no deployed strategy reads JP225
    finally:
        app.close()


def test_status_provider_does_not_take_the_core_lock(tmp_path):
    _init(tmp_path)
    _watch(tmp_path, ["JP225"])
    app = _app(tmp_path)
    try:
        result = {}
        with app.core_lock:
            worker = threading.Thread(
                target=lambda: result.setdefault("status", app.ops._status_provider()))
            worker.start()
            worker.join(timeout=10)
            assert not worker.is_alive()
        assert "status" in result
    finally:
        app.close()


def test_status_rows_flow_through_to_the_formatted_line(tmp_path):
    _init(tmp_path)
    _watch(tmp_path, ["JP225"])
    app = _app(tmp_path)
    try:
        from types import SimpleNamespace
        from agentic_fx.plugin.loader import IndicatorRef
        from agentic_fx.datafeed.requirements import build_registry

        strategy = SimpleNamespace(
            name="ref", kind="strategy", timeframe="1h", pairs=(app.settings.pairs[0],),
            max_bars=50, indicators=(IndicatorRef("jp", "ema", {}, None, symbol="JP225"),))
        app.ingest.registry = build_registry(app.settings, [strategy])
        status = app.ops._status_provider()
        assert status["references"] == [
            {"symbol": "JP225", "interval": "1h", "source": "mt5-live",
             "state": "missing", "age_seconds": None}]
        assert "references: JP225/1h/mt5-live=missing" in client.format_status(status)
    finally:
        app.close()


def test_the_signal_producer_reads_the_ingest_fetch_state_snapshot(tmp_path, monkeypatch):
    from types import MappingProxyType

    import agentic_fx.service as service_mod

    _init(tmp_path)
    app = _app(tmp_path)
    try:
        seen = {}
        with monkeypatch.context() as patch:
            patch.setattr(service_mod, "_run_signal_maintenance",
                          lambda **kwargs: seen.setdefault("producer", kwargs["signal_producer"]))
            app.scheduler.state_fn = lambda: "ready"
            app.scheduler.on_signal_maintenance(NOW)
        producer = seen["producer"]
        app.ingest.reference_fetch_state = MappingProxyType({("JP225", "1h"): "fetch_failed"})
        assert producer.reference_state(("JP225", "1h")) == "fetch_failed"
        assert producer.reference_state(("US500", "1h")) is None
    finally:
        app.close()
