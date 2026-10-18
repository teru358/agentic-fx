"""Signal producer の name-lock 再検証を守る回帰テスト。"""
from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.plugin import switch
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.signal_producer import (
    SignalProducer, _SourceLockTimeout, _SourceNotLive,
)
from agentic_fx.store.db import connect, init_db


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _conn(tmp_path):
    conn = connect(tmp_path / "signal.db")
    init_db(conn)
    return conn


def _meta() -> PluginMeta:
    return PluginMeta(
        name="strategy", kind="strategy", path=Path("/not-used"), params={},
        timeframe="1h", pairs=("USDJPY",), max_bars=2,
        content_hash="a" * 64,
    )


def test_insert_runs_only_while_name_lock_is_held(tmp_path, monkeypatch):
    """`_plugin_locks` を外す変異は、INSERT 時点の相互排他を失わせる。"""
    conn = _conn(tmp_path)
    producer = SignalProducer()
    held = False

    @contextmanager
    def lock(_root, names, **_kwargs):
        nonlocal held
        assert names == {"strategy"}
        held = True
        try:
            yield
        finally:
            held = False

    monkeypatch.setattr(switch, "_plugin_locks", lock)
    monkeypatch.setattr(
        "agentic_fx.plugin.signal_producer.source_state",
        lambda *_args, **_kw: None,
    )

    assert producer._insert_while_live(
        conn, _meta(), None, tmp_path / "plugins", SETTINGS,
        lambda: 1 if held else (_ for _ in ()).throw(AssertionError("unlocked")),
    ) == 1
    assert not held


def test_revalidation_blocks_insert_after_lock_acquisition(tmp_path, monkeypatch):
    """評価後に disable 相当の状態へ変われば signal を保存しない。"""
    conn = _conn(tmp_path)
    producer = SignalProducer()
    monkeypatch.setattr(
        "agentic_fx.plugin.signal_producer.source_state",
        lambda *_args, **_kw: "strategy_not_live",
    )

    called = False

    def insert():
        nonlocal called
        called = True
        return 1

    with pytest.raises(_SourceNotLive) as caught:
        producer._insert_while_live(
            conn, _meta(), None, tmp_path / "plugins", SETTINGS, insert)
    assert caught.value.reason == "strategy_not_live"
    assert not called


def test_busy_name_lock_skips_insert_without_converting_to_success(
        tmp_path, monkeypatch):
    """lock timeout は producer の cursor を進める成功ではない。"""
    conn = _conn(tmp_path)
    producer = SignalProducer()

    @contextmanager
    def busy(*_args, **_kwargs):
        raise switch.PluginBusyError("held elsewhere")
        yield

    monkeypatch.setattr(switch, "_plugin_locks", busy)

    with pytest.raises(_SourceLockTimeout):
        producer._insert_while_live(
            conn, _meta(), None, tmp_path / "plugins", SETTINGS,
            lambda: (_ for _ in ()).throw(AssertionError("must not insert")),
        )


def test_source_state_activity_is_once_per_unchanged_reason():
    """同じ lock timeout を tick ごとに activity へ洪水化させない。"""
    producer = SignalProducer()
    events = []

    class Activity:
        def write(self, _category, event, summary, ref_id=None):
            events.append((event, summary, ref_id))

    meta = _meta()
    producer._report_source_state(meta, "USDJPY", "lock_timeout", Activity())
    producer._report_source_state(meta, "USDJPY", "lock_timeout", Activity())
    producer._report_source_state(meta, "USDJPY", "strategy_not_live", Activity())

    assert events == [
        ("plugin_skipped_lock_timeout", "name=strategy reason=lock_timeout", "strategy"),
        ("plugin_skipped_not_live", "name=strategy reason=strategy_not_live", "strategy"),
    ]


class _Activity:
    def __init__(self):
        self.events = []

    def write(self, _category, event, summary, ref_id=None):
        self.events.append((event, summary))


def _evaluate(producer, conn, meta, now, plugins_root, sandbox, activity):
    from tests.plugin.test_signal_producer import SOURCE, _resolved_by_identity
    return producer.evaluate_due_plugins(
        conn, plugins=[meta], now=now, source=SOURCE, sandbox_run=sandbox,
        settings=SETTINGS, resolved_by_identity=_resolved_by_identity(meta),
        activity=activity, plugins_root=plugins_root)


def test_retired_plugin_is_skipped_before_the_worker_runs(tmp_path):
    """live に無い plugin は評価 (worker 起動) の前に skip し、通知は 1 回。"""
    from tests.plugin.test_signal_producer import H, _FakeSandbox, _seed_flat
    from datetime import timedelta
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    root = tmp_path / "plugins"
    root.mkdir()
    sandbox = _FakeSandbox()
    activity = _Activity()
    producer = SignalProducer()
    meta = _meta()

    for seconds in (31, 45):
        _evaluate(producer, conn, meta, H + timedelta(hours=1, seconds=seconds),
                  root, sandbox, activity)

    assert sandbox.calls == []
    assert activity.events == [
        ("plugin_skipped_not_live", "name=strategy reason=strategy_not_live")]


def test_lock_timeout_notice_is_renewed_after_a_successful_insert(
        tmp_path, monkeypatch):
    from tests.plugin.test_signal_producer import H, _FakeSandbox, _seed_flat
    from datetime import timedelta
    conn = _conn(tmp_path)
    _seed_flat(conn, H - timedelta(hours=3), 6 * 60 + 1)
    root = tmp_path / "plugins"
    root.mkdir()
    state = {"busy": True}

    @contextmanager
    def lock(*_args, **_kwargs):
        if state["busy"]:
            raise switch.PluginBusyError("held")
        yield

    monkeypatch.setattr(switch, "_plugin_locks", lock)
    monkeypatch.setattr(
        "agentic_fx.plugin.signal_producer.live_mismatch", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "agentic_fx.plugin.signal_producer.source_state", lambda *_a, **_k: None)
    sandbox = _FakeSandbox()
    activity = _Activity()
    producer = SignalProducer()
    meta = dataclasses.replace(_meta(), kind="signal")
    first = H + timedelta(hours=1, seconds=31)

    _evaluate(producer, conn, meta, first, root, sandbox, activity)
    _evaluate(producer, conn, meta, first, root, sandbox, activity)
    assert len(activity.events) == 1
    state["busy"] = False
    _evaluate(producer, conn, meta, first, root, sandbox, activity)
    state["busy"] = True
    _evaluate(producer, conn, meta, first + timedelta(hours=1), root, sandbox,
              activity)

    assert [e[0] for e in activity.events] == [
        "plugin_skipped_lock_timeout", "plugin_skipped_lock_timeout"]
