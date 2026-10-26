"""実際の ImproveLoop + ImproveSupervisor で、開始・終了 event と秘密/数値の流れを確かめる。"""
from __future__ import annotations

import json
import logging
import threading
from unittest.mock import patch

import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.improve_supervisor import ImproveSupervisor
from agentic_fx.loops.improve_loop import ImprovePrepareFailed
from agentic_fx.ops.contracts import Principal
from agentic_fx.runners.base import MissionResult
from agentic_fx.store import backlog as backlog_store
from agentic_fx.store import improve_waves
from agentic_fx.store import missions as missions_store

from tests.loops.test_improve_candidate_ledger import _loop, _strategy
from tests.loops.test_improve_e2e import (
    NOW,
    FakeImproveWorkerRunner,
    _fake_holdout_metrics,
    _fake_in_sample_metrics,
    _plugin_artifact,
    _write_staging_plugin,
    improve_env,  # noqa: F401
)

SECRET = "https://leak.example/path?apikey=SECRET9"
NUMBER = "7.7777"


def _supervisor(app, root, loop, *, emit=True):
    sup = ImproveSupervisor(
        capacity=1, root=root, settings=app.settings, clock=FixedClock(NOW),
        db_path=root / "data" / "agentic.db", stop_event=threading.Event(),
        activity=app.activity, emit_events=emit)
    sup._improve_loop = loop
    return sup


def _events(conn):
    return [(r["ref"], json.loads(r["fields_json"])) for r in conn.execute(
        "SELECT ref,fields_json FROM ops_events WHERE code='improve_run_changed' "
        "ORDER BY id").fetchall()]


class _StagingRunner(FakeImproveWorkerRunner):
    """worker が staging に書くはずの候補を、run の中で代わりに置く。"""

    def __init__(self, *, files, **kwargs):
        super().__init__(**kwargs)
        self._files = files

    def run(self, mission):
        if self._files is not None:
            _write_staging_plugin(self.kwargs["run_context"].staging_dir, *self._files)
        return super().run(mission)


def _drive(sup, result, files=None, *, in_sample=None, holdout_fn=None):
    patches = [patch("agentic_fx.runners.worker_runner.WorkerRunner",
                     lambda **kw: _StagingRunner(files=files, result=result, **kw))]
    if in_sample is not None:
        patches.append(patch("agentic_fx.loops.improve_loop.holdout.run_in_sample",
                             in_sample))
    if holdout_fn is not None:
        patches.append(patch("agentic_fx.loops.improve_loop.holdout.run_holdout_gate",
                             holdout_fn))
    for p in patches:
        p.start()
    try:
        return sup.submit_manual()
    finally:
        for p in reversed(patches):
            p.stop()


def _raising(exc):
    def build(**kw):
        raise exc
    return build


# ---- prepare の失敗 ----

@pytest.mark.parametrize("slot", [None, ("2026-W34", 0)])
def test_prepare_failure_after_tx0_announces_finished_only(improve_env, slot):
    app, root = improve_env
    conn = app.conn_core
    sup = _supervisor(app, root, _loop(app, root))
    if slot is not None:
        backlog_store.add(conn, "a pending idea", "user", NOW)
        improve_waves.create_wave_and_slots(
            conn, period_key=slot[0], now=NOW, expected=1, commit=True)
    boom = OSError("workspace broke")
    with patch("agentic_fx.runners.worker_runner.WorkerRunner", _raising(boom)):
        with pytest.raises(OSError) as raised:
            sup.submit_manual() if slot is None else sup._launch_slot(*slot)
    assert raised.value is boom
    mission_id = conn.execute("SELECT id FROM missions").fetchone()[0]
    assert _events(conn) == [(f"improve:{mission_id}", {
        "state": "finished", "trigger": "manual" if slot is None else "scheduled",
        "result_code": "failed", "reason_code": "prepare_failed", "tool_calls": None})]


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_base_exception_in_prepare_has_a_snapshot_but_no_event_until_next_start(
        improve_env, exc_type):
    app, root = improve_env
    conn = app.conn_core
    sup = _supervisor(app, root, _loop(app, root))
    with patch("agentic_fx.runners.worker_runner.WorkerRunner", _raising(exc_type())):
        with pytest.raises(exc_type) as raised:
            sup.submit_manual()
    assert not isinstance(raised.value, ImprovePrepareFailed)
    assert _events(conn) == []
    assert [tuple(r) for r in conn.execute(
        "SELECT result_code,reason_code FROM improve_run_notices").fetchall()] == [
        ("failed", "prepare_failed")]
    assert app.ops.emit_pending_improve_notices(enabled=True) == 1
    assert app.ops.emit_pending_improve_notices(enabled=True) == 0


def test_prepare_failure_whose_compensation_failed_emits_nothing_and_logs_not_terminal(
        improve_env, caplog):
    app, root = improve_env
    conn = app.conn_core
    sup = _supervisor(app, root, _loop(app, root))

    def failing(*a, **k):
        raise OSError("compensation broke")

    with patch("agentic_fx.runners.worker_runner.WorkerRunner",
               _raising(RuntimeError("x"))), \
            patch.object(missions_store, "finish_improve_mission", failing), \
            caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError):
            sup.submit_manual()
    mission_id = conn.execute("SELECT id FROM missions").fetchone()[0]
    assert _events(conn) == []
    skipped = [r.getMessage() for r in caplog.records
               if r.getMessage().startswith("improve_notice_skipped")]
    assert skipped == [f"improve_notice_skipped reason=not_terminal mission={mission_id}"]


# ---- 実 mission の終端 ----

def test_real_manual_mission_announces_start_and_finish_with_snapshot_values(
        improve_env):
    app, root = improve_env
    conn = app.conn_core
    sup = _supervisor(app, root, _loop(app, root))
    result = MissionResult(status="completed", output=_plugin_artifact("cand"),
                           transcript=[], tool_calls=5)
    from tests.loops.test_improve_candidate_ledger import _indicator
    mission_id = _drive(sup, result, files=_indicator())
    assert _events(conn) == [
        (f"improve:{mission_id}", {"state": "started", "trigger": "manual",
                                   "result_code": None, "reason_code": None,
                                   "tool_calls": None}),
        (f"improve:{mission_id}", {"state": "finished", "trigger": "manual",
                                   "result_code": "submitted", "reason_code": None,
                                   "tool_calls": 5})]


def test_events_are_off_when_the_switch_is_off_but_the_snapshot_is_written(improve_env):
    app, root = improve_env
    conn = app.conn_core
    sup = _supervisor(app, root, _loop(app, root), emit=False)
    result = MissionResult(status="completed", output=_plugin_artifact("cand"),
                           transcript=[], tool_calls=5)
    from tests.loops.test_improve_candidate_ledger import _indicator
    mission_id = _drive(sup, result, files=_indicator())
    assert _events(conn) == []
    assert conn.execute("SELECT result_code FROM improve_run_notices WHERE mission_id=?",
                        (mission_id,)).fetchone()[0] == "submitted"
    assert app.ops.emit_pending_improve_notices(enabled=False) == 0
    assert _events(conn) == []


def test_failed_finished_emit_is_logged_once_and_the_tick_sweep_announces_it_once(
        improve_env, caplog):
    import sqlite3
    from types import SimpleNamespace
    from agentic_fx import service as service_mod
    from agentic_fx.ops.events import EventStore
    app, root = improve_env
    conn = app.conn_core
    sup = _supervisor(app, root, _loop(app, root))
    real = EventStore.emit

    def flaky(self, code, ref, fields):
        if fields["state"] == "finished":
            raise sqlite3.OperationalError("database is locked")
        return real(self, code, ref, fields)

    result = MissionResult(status="completed", output=_plugin_artifact("cand"),
                           transcript=[], tool_calls=5)
    from tests.loops.test_improve_candidate_ledger import _indicator
    with patch.object(EventStore, "emit", flaky), caplog.at_level(logging.WARNING):
        mission_id = _drive(sup, result, files=_indicator())
    assert [r.getMessage() for r in caplog.records
            if "emit_failed" in r.getMessage()] == [
        f"improve_notice_emit_failed mission={mission_id} state=finished"]
    assert [f["state"] for _, f in _events(conn)] == ["started"]
    tick_app = SimpleNamespace(
        ops=app.ops, settings=SimpleNamespace(notify=SimpleNamespace(improve_events=True)))
    service_mod._sweep_improve_notices(tick_app)
    assert [f["state"] for _, f in _events(conn)] == ["started", "finished"]
    service_mod._sweep_improve_notices(tick_app)
    service_mod._sweep_improve_notices(tick_app)
    assert [f["state"] for _, f in _events(conn)] == ["started", "finished"]
    off = SimpleNamespace(
        ops=app.ops, settings=SimpleNamespace(notify=SimpleNamespace(improve_events=False)))
    service_mod._sweep_improve_notices(off)
    assert len(_events(conn)) == 2


# ---- canary ----

def _sinks(app, root, caplog):
    conn = app.conn_core
    activity = (root / "logs" / "activity.log")
    tables = {}
    for name in ("improvement_backlog", "ops_events", "improve_run_notices",
                 "ops_requests", "ops_idempotency"):
        tables[name] = repr([tuple(r) for r in conn.execute(
            f"SELECT * FROM {name}").fetchall()])
    tables["activity"] = activity.read_text() if activity.exists() else ""
    tables["technical_log"] = caplog.text
    return tables


def test_secret_and_number_canaries_stay_out_of_every_sink(improve_env, caplog):
    app, root = improve_env
    conn = app.conn_core
    sup = _supervisor(app, root, _loop(app, root))

    # (1) holdout の数値 canary + 秘密 canary を agent 出力に入れた、床未達の終端
    artifact = _plugin_artifact("cand", kind="strategy")
    artifact["selection_rationale"] = f"because {SECRET}"
    floor_result = MissionResult(status="completed", output=artifact, transcript=[],
                                 tool_calls=8)
    with caplog.at_level(logging.DEBUG):
        floor_mission = _drive(
            sup, floor_result, files=_strategy(),
            in_sample=_fake_in_sample_metrics(40, pf=1.5, avg_r=0.1),
            holdout_fn=_fake_holdout_metrics(pf=float(NUMBER), avg_r=-0.1))
    row = conn.execute("SELECT result_code,reason_code FROM improve_run_notices "
                       "WHERE mission_id=?", (floor_mission,)).fetchone()
    assert tuple(row) == ("gate_failed", "unprofitable")

    # (2) agent の観察文に秘密 canary を入れた終端
    observation = {"discoveries": [], "selected": {"backlog_id": None, "idea": "an idea"},
                   "artifact": {"type": "observation", "reason": f"saw {SECRET} here"},
                   "selection_rationale": "r"}
    with caplog.at_level(logging.DEBUG):
        obs_mission = _drive(sup, MissionResult(status="completed", output=observation,
                                                transcript=[], tool_calls=2))

    # GET の応答には床未達の数値が出る(これが唯一の経路)。観察文は出るが URL は出ない。
    with caplog.at_level(logging.DEBUG):
        floor_view = app.ops.improve_run(Principal.OPERATOR, floor_mission)
        obs_view = app.ops.improve_run(Principal.OPERATOR, obs_mission)
    assert floor_view["floor"]["stages"][0]["pairs"][0]["pf"] == float(NUMBER)
    assert obs_view["snapshot"]["observation_text"]
    assert "SECRET9" not in json.dumps(obs_view) and "SECRET9" not in json.dumps(floor_view)
    assert NUMBER not in json.dumps(obs_view)

    # 応答を取った後でも、どの sink にも出ていない(GET が記録を残さない)。
    sinks = _sinks(app, root, caplog)
    for name, text in sinks.items():
        assert "SECRET9" not in text and "leak.example" not in text, name
        assert NUMBER not in text, name
    assert [f["state"] for _, f in _events(conn)] == [
        "started", "finished", "started", "finished"]
