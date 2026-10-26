"""build_app が改善通知の発行を配線していること(スイッチの受け渡しと起動時の順序)。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml

from agentic_fx import service
from agentic_fx.core.contracts import FixedClock
from agentic_fx.ops.service import OpsService
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import build_app
from agentic_fx.store.db import connect
from tests.ops._improve_seed import seed
from tests.store.test_rag import FakeEmbedding
from tests.test_service_app import _init

NOW = datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc)


def _set_improve_events(root, value) -> None:
    path = root / "config" / "settings.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.setdefault("notify", {})["improve_events"] = value
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")


def _app(root):
    return build_app(root, runner=FakeRunner([]), clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())


def _events(conn):
    return [(r["ref"], json.loads(r["fields_json"])) for r in conn.execute(
        "SELECT ref,fields_json FROM ops_events WHERE code='improve_run_changed' "
        "ORDER BY id").fetchall()]


def _seed_unannounced(root, *, age=timedelta(hours=1)):
    conn = connect(root / "data" / "agentic.db")
    try:
        ended = NOW - age
        return seed(SimpleNamespace(conn=conn), started=ended - timedelta(minutes=5),
                    ended=ended)
    finally:
        conn.close()


@pytest.mark.parametrize("value", [True, False])
def test_supervisor_receives_the_switch(tmp_path, value):
    _init(tmp_path)
    _set_improve_events(tmp_path, value)
    app = _app(tmp_path)
    try:
        assert app.improve_supervisor._notices._enabled is value
    finally:
        app.close()


def test_default_settings_enable_the_switch(tmp_path):
    _init(tmp_path)
    app = _app(tmp_path)
    try:
        assert app.improve_supervisor._notices._enabled is True
        assert app.ops._improve_reports_dir == tmp_path / "data" / "improve_reports"
    finally:
        app.close()


def test_a_mission_driven_through_the_built_supervisor_emits_when_on(tmp_path):
    _init(tmp_path)
    app = _app(tmp_path)
    try:
        _run_observation(app)
        assert [f["state"] for _, f in _events(app.conn_core)] == ["started", "finished"]
    finally:
        app.close()


def test_a_mission_driven_through_the_built_supervisor_emits_nothing_when_off(tmp_path):
    _init(tmp_path)
    _set_improve_events(tmp_path, False)
    app = _app(tmp_path)
    try:
        mission_id = _run_observation(app)
        assert _events(app.conn_core) == []
        assert app.conn_core.execute(
            "SELECT count(*) FROM improve_run_notices WHERE mission_id=?",
            (mission_id,)).fetchone()[0] == 1
    finally:
        app.close()


def _run_observation(app):
    from unittest.mock import patch

    from tests.loops.test_improve_e2e import FakeImproveWorkerRunner
    output = {"discoveries": [], "selected": {"backlog_id": None, "idea": "an idea"},
              "artifact": {"type": "observation", "reason": "nothing"},
              "selection_rationale": "r"}
    result = MissionResult(status="completed", output=output, transcript=[], tool_calls=1)
    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               lambda **kw: FakeImproveWorkerRunner(result=result, **kw)):
        return app.improve_supervisor.submit_manual()


def test_outbox_runs_during_build_when_on_and_not_when_off(tmp_path):
    _init(tmp_path)
    _seed_unannounced(tmp_path)
    app = _app(tmp_path)
    try:
        assert len(_events(app.conn_core)) == 1
    finally:
        app.close()
    # 2 回目の起動で再通知されない
    app = _app(tmp_path)
    try:
        assert len(_events(app.conn_core)) == 1
    finally:
        app.close()


def test_outbox_is_skipped_when_off_and_the_snapshot_stays(tmp_path):
    _init(tmp_path)
    _set_improve_events(tmp_path, False)
    _seed_unannounced(tmp_path)
    app = _app(tmp_path)
    try:
        assert _events(app.conn_core) == []
        assert app.conn_core.execute(
            "SELECT count(*) FROM improve_run_notices").fetchone()[0] == 1
    finally:
        app.close()


def test_outbox_failure_does_not_fail_build_app(tmp_path, monkeypatch):
    _init(tmp_path)
    _seed_unannounced(tmp_path)

    def boom(self):
        raise RuntimeError("outbox down")

    with monkeypatch.context() as patch:
        patch.setattr(OpsService, "_emit_pending_improve_notices", boom)
        app = _app(tmp_path)
    try:
        assert _events(app.conn_core) == []
    finally:
        app.close()


def test_outbox_runs_right_after_the_ops_service_is_built_and_before_everything_else(
        tmp_path, monkeypatch):
    _init(tmp_path)
    order: list[str] = []
    real_build = service._build_ops_service
    real_recover = service._recover_ops
    real_emit = OpsService.emit_pending_improve_notices

    def build(*args, **kwargs):
        order.append("build_ops_service")
        return real_build(*args, **kwargs)

    def recover(*args, **kwargs):
        order.append("recover_ops")
        return real_recover(*args, **kwargs)

    def emit(self, *, enabled):
        order.append(f"emit_pending:{enabled}")
        return real_emit(self, enabled=enabled)

    def use(self, ops):
        order.append("use_ops_service")

    from agentic_fx.commands import Commands
    with monkeypatch.context() as patch:
        patch.setattr(service, "_build_ops_service", build)
        patch.setattr(service, "_recover_ops", recover)
        patch.setattr(OpsService, "emit_pending_improve_notices", emit)
        patch.setattr(Commands, "use_ops_service", use)
        app = _app(tmp_path)
    app.close()
    assert order == ["build_ops_service", "emit_pending:True", "use_ops_service",
                     "recover_ops"]


def test_build_app_calls_the_outbox_exactly_once():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(service.__file__).read_text(encoding="utf-8"))
    build_app_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == "build_app")
    calls = [c for c in ast.walk(build_app_fn) if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Attribute)
             and c.func.attr == "emit_pending_improve_notices"]
    assert len(calls) == 1
