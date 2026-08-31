"""ImproveSupervisor.tick の B-7 配線 pin (プラン10 Task11g Step6b、裁定1)。

逸脱 (実測): プランは「既存ファイル、着手時に実ファイル名・フィクスチャ規約
を確認」と指示するが、`tests/core/test_improve_supervisor.py` は本 task の
着手時点で非実在だった (`ImproveSupervisor` の既存テストは
`tests/core/test_improve_wave_slot_protocol.py` にある) — 新規作成し、その
ファイルの fixture 規約 (`_FixedClock`/`_fake_settings`/`_conn_for_test` シーム)
を踏襲する。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentic_fx.core.improve_supervisor import ImproveSupervisor
from agentic_fx.store import db as db_mod

NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)


class _FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _fake_settings(parallel: int) -> Any:
    class _Cadence:
        improve = "weekly"
        improve_at = "Sat 03:00"

    class _Improve:
        def __init__(self, p: int) -> None:
            self.parallel = p

    class _FakeSettings:
        def __init__(self) -> None:
            self.schedule = _Cadence()
            self.improve = _Improve(parallel)
            self.display_timezone = "UTC"

    return _FakeSettings()


def test_improve_supervisor_tick_calls_process_expired_approvals(monkeypatch, tmp_path):
    """B-7: tick() が process_expired_approvals を呼ぶことの配線 pin
    (M9 の killer)。capacity=0 で spawn まで進めないようにし、期限切れ
    処理呼び出しだけを観測する。"""
    from agentic_fx.plugin import switch
    calls = []
    monkeypatch.setattr(
        switch, "process_expired_approvals",
        lambda conn, *, plugins_root, now, activity=None: calls.append((plugins_root, now)))

    db_path = tmp_path / "agentic.db"
    conn = db_mod.connect(db_path)
    db_mod.init_db(conn)
    sup = ImproveSupervisor(capacity=0, root=tmp_path,
                            settings=_fake_settings(parallel=1),
                            clock=_FixedClock(NOW),
                            db_path=db_path,
                            stop_event=threading.Event())
    sup._conn_for_test = conn  # テストシーム

    sup.tick(NOW)

    assert len(calls) == 1
    assert calls[0] == (tmp_path / "plugins", NOW)


def test_improve_supervisor_tick_threads_activity_into_process_expired_approvals(
        tmp_path):
    """E4 裁定 (2026-08-25): `process_expired_approvals` の name 欠落
    payload に対する activity ERROR 記録は、`ImproveSupervisor.tick()`
    経由の呼び出し (service.py 起動時 reconcile とは独立に毎 tick 実行
    される経路) でも書けなければならない。`ImproveSupervisor` に
    `activity` を渡して構築し、name の無い plugin payload が期限到来した
    状態で `tick()` を呼び、activity ログに ERROR が実際に書かれることを
    実物 (spy なし) で確認する。"""
    from agentic_fx.activity import ActivityLog
    from agentic_fx.store import approvals as approvals_store

    db_path = tmp_path / "agentic.db"
    conn = db_mod.connect(db_path)
    db_mod.init_db(conn)
    approvals_store.create(
        conn, "plugin", {"candidate_origin": "staging"}, NOW,
        expires_at=NOW - timedelta(minutes=1))  # name が無い契約違反 payload

    activity = ActivityLog(tmp_path / "logs" / "activity.log")
    sup = ImproveSupervisor(capacity=0, root=tmp_path,
                            settings=_fake_settings(parallel=1),
                            clock=_FixedClock(NOW),
                            db_path=db_path,
                            stop_event=threading.Event(),
                            activity=activity)
    sup._conn_for_test = conn  # テストシーム

    sup.tick(NOW)

    log_text = (tmp_path / "logs" / "activity.log").read_text()
    assert "expire_skipped_payload_missing_name" in log_text


def test_improve_supervisor_tick_skips_process_expired_approvals_when_stopped(
        monkeypatch, tmp_path):
    """`_stop_event` が既に set されていれば tick() は何もしない (期限切れ
    処理も含め早期 return する既存契約の維持を確認)。"""
    from agentic_fx.plugin import switch
    calls = []
    monkeypatch.setattr(
        switch, "process_expired_approvals",
        lambda conn, *, plugins_root, now, activity=None: calls.append((plugins_root, now)))

    db_path = tmp_path / "agentic.db"
    conn = db_mod.connect(db_path)
    db_mod.init_db(conn)
    stop_event = threading.Event()
    stop_event.set()
    sup = ImproveSupervisor(capacity=0, root=tmp_path,
                            settings=_fake_settings(parallel=1),
                            clock=_FixedClock(NOW),
                            db_path=db_path,
                            stop_event=stop_event)
    sup._conn_for_test = conn

    sup.tick(NOW)

    assert calls == []


class _CommitFailureRunner:
    def __init__(self, result, *, call_on_ready=False, stop_event=None):
        self.result = result
        self.on_ready = None
        self.call_on_ready = call_on_ready
        self.stop_event = stop_event

    def run(self, mission):
        if self.call_on_ready:
            self.on_ready({"type": "ready", "ok": True})
        if self.stop_event is not None:
            self.stop_event.set()
        return self.result


class _CommitFailureLoop:
    def __init__(self, ctx, runner):
        self.ctx = ctx
        self.runner = runner
        self.compensations = []

    def prepare(self, *, slot_key, now, on_ready=None):
        self.runner.on_ready = on_ready
        return "mission", self.ctx, self.runner

    def commit(self, **kwargs):
        raise RuntimeError("boom")

    def compensate_commit_failure(self, **kwargs):
        self.compensations.append(kwargs)


def _commit_failure_supervisor(tmp_path, *, stop_event=None):
    stop_event = stop_event or threading.Event()
    return ImproveSupervisor(
        capacity=1, root=tmp_path, settings=_fake_settings(parallel=1),
        clock=_FixedClock(NOW), db_path=tmp_path / "unused.db",
        stop_event=stop_event)


def test_submit_manual_compensates_commit_exception_and_reraises(tmp_path):
    ctx = SimpleNamespace(mission_id=101)
    loop = _CommitFailureLoop(
        ctx, _CommitFailureRunner(type("Result", (), {"status": "completed"})()))
    sup = _commit_failure_supervisor(tmp_path)
    sup._improve_loop = loop

    with pytest.raises(RuntimeError, match="boom"):
        sup.submit_manual()

    assert len(loop.compensations) == 1
    assert loop.compensations[0]["ctx"] is ctx
    assert loop.compensations[0]["slot_terminalize"] is True


def test_launch_slot_running_path_compensates_commit_exception_and_reraises(
        tmp_path, monkeypatch):
    ctx = SimpleNamespace(mission_id=102)
    runner = _CommitFailureRunner(
        type("Result", (), {"status": "failed"})(), call_on_ready=True)
    loop = _CommitFailureLoop(ctx, runner)
    sup = _commit_failure_supervisor(tmp_path)
    sup._improve_loop = loop
    monkeypatch.setattr(
        "agentic_fx.core.improve_supervisor.improve_waves.mark_running",
        lambda *args, **kwargs: True)
    sup._conn_for_test = object()

    with pytest.raises(RuntimeError, match="boom"):
        sup._launch_slot("2026-W36", 0)

    assert len(loop.compensations) == 1
    assert loop.compensations[0]["ctx"] is ctx
    assert loop.compensations[0]["slot_terminalize"] is True


def test_launch_slot_pre_ready_path_compensates_commit_exception_and_reraises(
        tmp_path, monkeypatch):
    stop_event = threading.Event()
    ctx = SimpleNamespace(mission_id=103)
    runner = _CommitFailureRunner(
        type("Result", (), {"status": "failed"})(), stop_event=stop_event)
    loop = _CommitFailureLoop(ctx, runner)
    sup = _commit_failure_supervisor(tmp_path, stop_event=stop_event)
    sup._improve_loop = loop
    monkeypatch.setattr(sup, "_handle_pre_ready_failure", lambda *args: True)

    with pytest.raises(RuntimeError, match="boom"):
        sup._launch_slot("2026-W36", 0)

    assert len(loop.compensations) == 1
    assert loop.compensations[0]["ctx"] is ctx
    assert loop.compensations[0]["slot_terminalize"] is False
