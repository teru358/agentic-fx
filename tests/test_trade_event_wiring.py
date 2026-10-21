"""取引 event の配線 (AC-10、AC-10b、AC-15b、AC-26)。"""
from __future__ import annotations

import ast
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from agentic_fx import service
from agentic_fx.core import transitions
from agentic_fx.core.contracts import FixedClock, Mode
from agentic_fx.core.contracts import OrderStatus as S
from agentic_fx.ops import client
from agentic_fx.ops.trade_event_sink import COUNTERS, TradeEventEmitter, TradeEventSink
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import build_app
from agentic_fx.store import orders
from agentic_fx.store.db import connect
from tests.store.test_rag import FakeEmbedding
from tests.test_service_app import _init

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SRC = Path(__file__).resolve().parents[1] / "src" / "agentic_fx"


def _set_notify(root: Path, value) -> None:
    path = root / "config" / "settings.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["notify"] = value
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")


def _app(root: Path):
    return build_app(root, runner=FakeRunner([]), clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())


def _observe_calls() -> list[tuple[str, int]]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "observe"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "transitions"):
                found.append((str(path.relative_to(SRC)), node.lineno))
    return found


def test_ac10_observe_is_called_from_exactly_one_place_in_service():
    assert [path for path, _ in _observe_calls()] == ["service.py"]
    assert len(_observe_calls()) == 1
    # transitions 内の定義以外に別名での登録 (from ... import observe) を作らない
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "agentic_fx.core.transitions":
                assert "observe" not in {a.name for a in node.names}, path


def test_ac10_backtest_connections_never_have_an_observer(tmp_path, monkeypatch):
    from tests.backtest.test_runner import (
        DATASET_1M, OPEN, SETTINGS, WED, _conn, _seed_history, run_replay)

    looked_up = []
    real_get = transitions._REGISTRY.get

    def spy(conn):
        found = real_get(conn)
        looked_up.append(found)
        return found

    # service が登録済みの別接続があっても、バックテストの :memory: 接続は対象外
    other = connect(tmp_path / "svc.db")
    calls = []
    token = transitions.observe(other, lambda *a: calls.append(a))
    monkeypatch.setattr(transitions._REGISTRY, "get", spy)
    hist = _conn(tmp_path)
    _seed_history(hist)
    fired = []

    def source(bar):
        if not fired:
            fired.append(bar.ts)
            return dict(OPEN)
        return None

    try:
        res = run_replay(SETTINGS, symbol="USDJPY", dataset=DATASET_1M, start=WED,
                         end=WED + timedelta(hours=2), intent_source=source,
                         eval_timeframe="1h", history_conn=hist)
    finally:
        transitions.unobserve(token)
        other.close()
    assert [o for o in res.orders if o["status"] == "closed"]
    assert looked_up and all(found is None for found in looked_up)
    assert calls == []


def test_ac26_notify_true_registers_one_observer_and_close_releases_it(tmp_path):
    _init(tmp_path)
    app = _app(tmp_path)
    try:
        assert isinstance(app.trade_event_sink, TradeEventSink)
        assert app.trade_event_sink.worker_alive()
        with pytest.raises(transitions.ObserverError):
            transitions.observe(app.conn_core, lambda *a: None)
    finally:
        app.close()
    assert not app.trade_event_sink.worker_alive()
    # 解除済み: 同じ接続は (閉じられていても) 再登録の拒否対象ではない
    probe = connect(tmp_path / "data" / "agentic.db")
    token = transitions.observe(probe, lambda *a: None)
    transitions.unobserve(token)
    probe.close()


def test_ac26_notify_false_starts_neither_observer_nor_sink(tmp_path):
    _init(tmp_path)
    _set_notify(tmp_path, {"trade_events": False})
    app = _app(tmp_path)
    try:
        assert app.trade_event_sink is None and app.trade_event_token is None
        token = transitions.observe(app.conn_core, lambda *a: None)  # 登録できる = 未登録
        transitions.unobserve(token)
        oid = orders.insert(app.conn_core, pair="USDJPY", direction="long", entry_type="limit",
                            horizon="day", status=S.SUBMITTING, now=NOW, quantity=0.1,
                            requested_price=148.2, stop_loss=147.8)
        transitions.transition(app.conn_core, oid, S.SUBMITTED, NOW)
        transitions.transition(app.conn_core, oid, S.PENDING_FILL, NOW)
        count = app.conn_core.execute("SELECT COUNT(*) FROM ops_events").fetchone()[0]
        assert count == 0
        assert "trade_events" not in app.ops._status_provider()
    finally:
        app.close()


def test_ac26_notify_section_omitted_defaults_to_enabled(tmp_path):
    _init(tmp_path)
    path = tmp_path / "config" / "settings.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.pop("notify", None)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    app = _app(tmp_path)
    try:
        assert app.trade_event_sink is not None
    finally:
        app.close()


@pytest.mark.parametrize("mode", [Mode.LEARNING, Mode.TRADING])
def test_ac10b_state_mode_is_read_once_at_startup(tmp_path, mode):
    _init(tmp_path)
    from agentic_fx.store.state import StateStore
    StateStore(tmp_path / "data" / "state" / "app_state.json").update(mode=mode)
    app = _app(tmp_path)
    try:
        oid = orders.insert(app.conn_core, pair="USDJPY", direction="long", entry_type="limit",
                            horizon="day", status=S.SUBMITTING, now=NOW, quantity=0.1,
                            requested_price=148.2, stop_loss=147.8)
        # 起動後に state を書き換えても observer は起動時の値のまま
        other = Mode.TRADING if mode is Mode.LEARNING else Mode.LEARNING
        app.state.update(mode=other)
        transitions.transition(app.conn_core, oid, S.SUBMITTED, NOW)
        transitions.transition(app.conn_core, oid, S.PENDING_FILL, NOW)
        app.trade_event_sink.stop()
        row = connect(tmp_path / "data" / "agentic.db").execute(
            "SELECT fields_json FROM ops_events WHERE code='order_state_changed'").fetchone()
        import json
        assert json.loads(row[0])["state_mode"] == mode.value
    finally:
        app.close()


def test_ac10b_unreadable_state_gives_none(monkeypatch):
    class Broken:
        def load(self):
            raise OSError("state unreadable")

    assert service._read_state_mode(Broken()) is None


def test_ac26_build_app_fails_when_registration_fails(tmp_path, monkeypatch):
    _init(tmp_path)

    def boom(*a, **k):
        raise transitions.ObserverError("already registered")

    monkeypatch.setattr(service.transitions, "observe", boom)
    started = []
    real_start = TradeEventSink.start
    monkeypatch.setattr(TradeEventSink, "start", lambda self: (started.append(self),
                                                               real_start(self))[1])
    with pytest.raises(transitions.ObserverError):
        _app(tmp_path)
    assert started and not started[0].worker_alive()  # 登録に失敗したら書き込み thread も残らない


def test_ac26_build_app_failure_after_registration_releases_observer_and_sink(tmp_path, monkeypatch):
    _init(tmp_path)
    captured = {}
    real_start = service._start_trade_events

    def spy(*a, **k):
        sink, token = real_start(*a, **k)
        captured["sink"], captured["token"] = sink, token
        return sink, token

    monkeypatch.setattr(service, "_start_trade_events", spy)
    monkeypatch.setattr(service, "_build_ops_service",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ops build failed")))
    with pytest.raises(RuntimeError):
        _app(tmp_path)
    assert not captured["sink"].worker_alive()
    probe = connect(tmp_path / "data" / "agentic.db")
    transitions.unobserve(transitions.observe(probe, lambda *a: None))
    probe.close()


def test_close_unobserves_before_stopping_the_sink(tmp_path, monkeypatch):
    _init(tmp_path)
    app = _app(tmp_path)
    order = []
    monkeypatch.setattr(transitions, "unobserve", lambda token: order.append("unobserve"))
    sink = app.trade_event_sink
    real_stop = sink.stop
    monkeypatch.setattr(sink, "stop", lambda: (order.append("stop"), real_stop())[1])
    app.close()
    assert order[:2] == ["unobserve", "stop"]


def test_ac15b_status_carries_the_seven_counters_only(tmp_path):
    _init(tmp_path)
    app = _app(tmp_path)
    try:
        app.trade_event_sink.record_drop("queue_drop", 3)
        status = app.ops._status_provider()
        assert status["trade_events"] == {**dict.fromkeys(COUNTERS, 0), "queue_drop": 3}
    finally:
        app.close()


def test_ac15b_format_status_shows_counts_and_nothing_else():
    counters = {**dict.fromkeys(COUNTERS, 0), "queue_drop": 3, "db_busy_drop": 12}
    text = client.format_status({"trade_events": counters})
    line = next(line for line in text.splitlines() if line.startswith("取引 event"))
    assert line == ("取引 event の欠落: queue_drop=3 observer_error=0 db_busy_drop=12 "
                    "db_error_drop=0 invalid_value_drop=0 worker_death=0 shutdown_drop=0")
    assert "取引 event" not in client.format_status({})
    hostile = client.format_status({"trade_events": {"queue_drop": "\x1b[31mred", "x": "y"}})
    assert "\x1b" not in hostile and "queue_drop=-" in hostile


def test_start_trade_events_uses_the_configured_account_currency(tmp_path):
    from types import SimpleNamespace
    (tmp_path / "data").mkdir()
    conn = connect(tmp_path / "data" / "agentic.db")
    from agentic_fx.store.db import init_db
    init_db(conn)
    state = SimpleNamespace(load=lambda: SimpleNamespace(mode=Mode.LEARNING))
    sink, token = service._start_trade_events(
        tmp_path, conn, SimpleNamespace(account_currency="USD"), state, FixedClock(NOW))
    try:
        emitter = transitions._REGISTRY.get(conn)
        assert isinstance(emitter, TradeEventEmitter)
        assert emitter._ccy == "USD"
    finally:
        transitions.unobserve(token)
        sink.stop()
        conn.close()


def test_close_without_trade_events_does_not_try_to_release_an_observer(tmp_path, monkeypatch):
    _init(tmp_path)
    _set_notify(tmp_path, {"trade_events": False})
    app = _app(tmp_path)
    released = []
    monkeypatch.setattr(transitions, "unobserve", lambda token: released.append(token))
    app.close()
    assert released == []


def test_build_failure_releases_the_observer_on_the_core_connection(tmp_path, monkeypatch):
    _init(tmp_path)
    captured = {}
    real_start = service._start_trade_events

    def spy(root, conn_core, *a, **k):
        captured["conn"] = conn_core
        return real_start(root, conn_core, *a, **k)

    monkeypatch.setattr(service, "_start_trade_events", spy)
    monkeypatch.setattr(service, "_build_ops_service",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ops build failed")))
    with pytest.raises(RuntimeError):
        _app(tmp_path)
    assert transitions._REGISTRY.get(captured["conn"]) is None
